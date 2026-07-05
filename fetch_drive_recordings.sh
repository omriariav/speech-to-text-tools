#!/bin/bash
#
# Drive-API poller for Meet recordings.
#
# Lists the configured Google Drive folder via the `gws` CLI, downloads any
# new recordings DIRECTLY over the Drive API, and hands each off to the normal
# enqueue path (auto_transcribe_meet.sh -> queue -> worker -> transcribe_one).
#
# Why this exists: the macOS Folder Action path relies on Google Drive File
# Stream materializing the cloud placeholder locally before ffmpeg reads it.
# That materialization is unreliable — placeholders that never download,
# "Operation not permitted", quarantine attributes — and the old workaround
# was a blind 600s TRANSCRIPTION_START_DELAY gamble. Fetching the bytes over
# the API removes that whole class of failure: the file we hand downstream is
# a real, fully-downloaded local file.
#
# This REPLACES the Folder Action as the trigger for the Meet Recordings
# folder. The Downloads Folder Action stays (manual local m4a/mp4 drops).
#
# Idempotency is keyed on the stable Drive file ID via a ledger directory:
# a recording is downloaded + enqueued exactly once, no matter how often we
# poll. Staged files are pruned once they're done (not referenced by any
# pending job) and older than the retention window.
#
# Designed to be run hourly by a LaunchAgent; safe to run manually any time.
#
# Usage: fetch_drive_recordings.sh
#

set -e
set -o pipefail

notify() {
    [ "${DRIVE_NOTIFY:-1}" = "0" ] && return 0
    [ -x "$(command -v osascript)" ] || return 0

    local title="$1"
    local message="$2"
    osascript -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\"" >/dev/null 2>&1 || true
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
else
    message="ERROR: .env file not found. Copy .env.example to .env and configure it."
    echo "$message" >&2
    notify "Drive poll error" "$message"
    exit 1
fi

for var in OUTPUT_DIR LOG_FILE DRIVE_FOLDER_ID; do
    if [ -z "${!var}" ]; then
        message="ERROR: $var is not set in .env (DRIVE_FOLDER_ID is local-only; never committed)"
        echo "$message" >&2
        notify "Drive poll error" "$message"
        exit 1
    fi
done

# gws lives in the Go bin dir; launchd runs with a minimal PATH, so resolve
# an absolute binary and don't rely on PATH lookup.
GWS_BIN="${GWS_BIN:-$HOME/go/bin/gws}"
if [ ! -x "$GWS_BIN" ]; then
    if command -v gws >/dev/null 2>&1; then
        GWS_BIN="$(command -v gws)"
    else
        message="ERROR: gws binary not found (looked at $GWS_BIN and PATH)"
        echo "$message" >&2
        notify "Drive poll error" "$message"
        exit 1
    fi
fi

if ! command -v jq >/dev/null 2>&1; then
    message="ERROR: jq binary not found"
    echo "$message" >&2
    notify "Drive poll error" "$message"
    exit 1
fi

QUEUE_DIR="${QUEUE_DIR:-$OUTPUT_DIR/.queue}"
STAGING_DIR="${STAGING_DIR:-$OUTPUT_DIR/.staging}"
DRIVE_LEDGER_DIR="${DRIVE_LEDGER_DIR:-$OUTPUT_DIR/.drive_done}"
ENQUEUE_SCRIPT="${ENQUEUE_SCRIPT:-$SCRIPT_DIR/auto_transcribe_meet.sh}"
WORKER_SCRIPT="${WORKER_SCRIPT:-$SCRIPT_DIR/auto_transcribe_worker.sh}"
# launchd kills leftover processes in the job's process group when the poller
# exits. Drain in the foreground so downloaded recordings are not abandoned
# mid-conversion/transcription. Set to 0 only if another worker service drains
# $QUEUE_DIR.
DRIVE_DRAIN_QUEUE="${DRIVE_DRAIN_QUEUE:-1}"
# How long a downloaded-but-finished staged file may linger before pruning.
# Generous: even a 400MB recording transcribes well within this window.
STAGING_RETENTION_SECONDS="${STAGING_RETENTION_SECONDS:-7200}"
# Max recordings to consider per poll (folder is newest-first).
DRIVE_LIST_MAX="${DRIVE_LIST_MAX:-50}"
# DRY_RUN=1 logs what would be downloaded/enqueued without touching anything.
DRY_RUN="${DRY_RUN:-0}"
POLL_LOCK="$STAGING_DIR/.poll.lock"

mkdir -p "$STAGING_DIR" "$DRIVE_LEDGER_DIR" "$QUEUE_DIR" "$OUTPUT_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DRIVE] $1" | tee -a "$LOG_FILE"
}

queue_job_count() {
    local count=0
    for j in "$QUEUE_DIR"/*.job; do
        [ -e "$j" ] && count=$((count + 1))
    done
    echo "$count"
}

# Single-instance guard. Hourly launchd runs may overlap now that the poller
# drains long transcription jobs in the foreground. mkdir is atomic; the pid
# file prevents a later hourly launch from reclaiming a live long-running drain.
POLL_LOCK_OWNED=false
write_poll_lock_pid() {
    printf '%s\n' "$$" > "$POLL_LOCK/pid"
    POLL_LOCK_OWNED=true
}

release_poll_lock() {
    if [ "$POLL_LOCK_OWNED" = true ] && [ "$(cat "$POLL_LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
        rm -rf "$POLL_LOCK" 2>/dev/null || true
    fi
}

if mkdir "$POLL_LOCK" 2>/dev/null; then
    write_poll_lock_pid
else
    holder_pid="$(cat "$POLL_LOCK/pid" 2>/dev/null || true)"
    if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then
        log "Another poller is running (pid $holder_pid); exiting."
        notify "Drive poll skipped" "Another Drive poller is already running."
        exit 0
    fi

    # No pid yet, no pid file, or pid dead. Reclaim only after the age guard
    # so a sibling that just mkdir'd the lock has time to write its pid.
    lock_birth="$(stat -f %m "$POLL_LOCK" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    if [ "$(( now - lock_birth ))" -ge 3600 ]; then
        dead_name="${POLL_LOCK}.dead.$$.$(date +%s%N)"
        if mv "$POLL_LOCK" "$dead_name" 2>/dev/null; then
            rm -rf "$dead_name"
            mkdir "$POLL_LOCK" 2>/dev/null || { log "Another poller holds the lock; exiting."; exit 0; }
            write_poll_lock_pid
            log "Reclaimed stale poll lock."
        else
            log "Another poller holds the lock; exiting."
            exit 0
        fi
    else
        log "Another poller is running; exiting."
        notify "Drive poll skipped" "Another Drive poller is already running."
        exit 0
    fi
fi
trap release_poll_lock EXIT

# Mirror auto_transcribe_meet.sh's filename sanitizer so staged names line up
# with how the enqueuer would have sanitized a Folder-Action drop.
sanitize_filename() {
    printf '%s' "$1" | tr ' /:' '---'
}

# Prune staged files that are finished: not referenced by any pending job and
# older than the retention window. Keeps disk bounded without risking deletion
# of a file mid-transcription.
prune_staging() {
    local now f mtime age
    now="$(date +%s)"
    for f in "$STAGING_DIR"/*.mp4 "$STAGING_DIR"/*.m4a; do
        [ -e "$f" ] || continue
        # Still queued/processing? Leave it alone.
        if grep -rqlF -- "INPUT_PATH=$f" "$QUEUE_DIR" 2>/dev/null; then
            continue
        fi
        mtime="$(stat -f %m "$f" 2>/dev/null || echo "$now")"
        age=$(( now - mtime ))
        if [ "$age" -ge "$STAGING_RETENTION_SECONDS" ]; then
            rm -f "$f"
            log "Pruned finished staged file: $(basename "$f") (age ${age}s)"
        fi
    done
}

prune_staging

# List the folder. JSON is the default; we filter to mp4 recordings. Gemini
# "Notes" exports are native Google Docs (mime application/vnd.google-apps.*)
# and are excluded by the mime filter — so the .gdoc dedupe hazard that bites
# the Folder-Action path simply cannot occur here.
LIST_JSON="$("$GWS_BIN" drive list --folder "$DRIVE_FOLDER_ID" --max "$DRIVE_LIST_MAX" --order "modifiedTime desc" --format json 2>>"$LOG_FILE")" || {
    log "ERROR: gws drive list failed (auth expired? run 'gws auth login'). See log."
    notify "Drive poll error" "Could not list Meet recordings. Check gws auth and the log."
    exit 1
}

NEW_COUNT=0
SKIP_COUNT=0
ERROR_COUNT=0
ENQUEUED_FOR_DRAIN=0
MP4_TSV="$(printf '%s' "$LIST_JSON" | jq -r '.files[] | select(.mime_type == "video/mp4") | [.id, .name] | @tsv')" || {
    log "ERROR: could not parse Drive list JSON. See log."
    notify "Drive poll error" "Could not parse Drive list JSON. Check the log."
    exit 1
}

# id<TAB>name per mp4. Use @tsv so names with spaces/slashes survive intact.
while IFS=$'\t' read -r FILE_ID FILE_NAME; do
    [ -n "$FILE_ID" ] || continue

    LEDGER_MARKER="$DRIVE_LEDGER_DIR/$FILE_ID"
    if [ -e "$LEDGER_MARKER" ]; then
        SKIP_COUNT=$(( SKIP_COUNT + 1 ))
        continue
    fi

    # Build a local staged filename: sanitize, strip any trailing .mp4, then
    # append a single .mp4 (Drive names usually omit the extension).
    STEM="${FILE_NAME%.mp4}"
    SANITIZED="$(sanitize_filename "$STEM")"
    STAGED_PATH="$STAGING_DIR/${SANITIZED}.mp4"

    log "New recording: '$FILE_NAME' ($FILE_ID) -> $(basename "$STAGED_PATH")"

    if [ "$DRY_RUN" = "1" ]; then
        log "DRY_RUN: would download $FILE_ID and enqueue $(basename "$STAGED_PATH")"
        NEW_COUNT=$(( NEW_COUNT + 1 ))
        continue
    fi

    # Download over the API. On failure, do NOT write the ledger marker so the
    # next poll retries; clean up any partial file.
    if ! "$GWS_BIN" drive download "$FILE_ID" --output "$STAGED_PATH" >>"$LOG_FILE" 2>&1; then
        log "ERROR: download failed for $FILE_ID; will retry next poll."
        ERROR_COUNT=$(( ERROR_COUNT + 1 ))
        rm -f "$STAGED_PATH" 2>/dev/null || true
        continue
    fi

    if [ ! -s "$STAGED_PATH" ]; then
        log "ERROR: downloaded file empty for $FILE_ID; will retry next poll."
        ERROR_COUNT=$(( ERROR_COUNT + 1 ))
        rm -f "$STAGED_PATH" 2>/dev/null || true
        continue
    fi

    # Hand off to the normal enqueue path. The staged path is NOT under the
    # GoogleDrive- substring, so the worker's start-delay is auto-skipped.
    # When running from launchd, keep the worker in this process tree until it
    # finishes; otherwise launchd can SIGTERM the background worker as soon as
    # this poller exits.
    set +e
    if [ "$DRIVE_DRAIN_QUEUE" = "0" ]; then
        "$ENQUEUE_SCRIPT" "$STAGED_PATH" >>"$LOG_FILE" 2>&1
    else
        AUTO_TRANSCRIBE_SPAWN_WORKER=0 "$ENQUEUE_SCRIPT" "$STAGED_PATH" >>"$LOG_FILE" 2>&1
    fi
    ENQUEUE_EXIT=$?
    set -e

    if [ "$ENQUEUE_EXIT" -eq 0 ]; then
        : > "$LEDGER_MARKER"
        printf 'name=%s\nstaged=%s\nat=%s\n' "$FILE_NAME" "$STAGED_PATH" "$(date '+%Y-%m-%d %H:%M:%S')" > "$LEDGER_MARKER"
        NEW_COUNT=$(( NEW_COUNT + 1 ))
        ENQUEUED_FOR_DRAIN=$(( ENQUEUED_FOR_DRAIN + 1 ))
        log "Enqueued: $(basename "$STAGED_PATH")"
    else
        log "ERROR: enqueue failed for $(basename "$STAGED_PATH"); will retry next poll."
        ERROR_COUNT=$(( ERROR_COUNT + 1 ))
        rm -f "$STAGED_PATH" 2>/dev/null || true
    fi
done <<< "$MP4_TSV"

PENDING_FOR_DRAIN="$(queue_job_count)"
if [ "$DRY_RUN" != "1" ] && [ "$DRIVE_DRAIN_QUEUE" != "0" ] && { [ "$ENQUEUED_FOR_DRAIN" -gt 0 ] || [ "$PENDING_FOR_DRAIN" -gt 0 ]; }; then
    log "Draining transcription queue in foreground ($PENDING_FOR_DRAIN pending job(s), $ENQUEUED_FOR_DRAIN new recording(s))..."
    if "$WORKER_SCRIPT"; then
        log "Worker drain complete."
    else
        WORKER_EXIT=$?
        log "ERROR: worker drain failed (exit $WORKER_EXIT)."
        notify "Drive poll worker failed" "Worker drain failed with exit $WORKER_EXIT. Check the log."
        exit "$WORKER_EXIT"
    fi
fi

if [ "$ERROR_COUNT" -gt 0 ]; then
    log "Poll complete: $NEW_COUNT new, $SKIP_COUNT already handled, $ERROR_COUNT errors."
    notify "Drive poll completed with errors" "$NEW_COUNT new, $SKIP_COUNT already handled, $ERROR_COUNT errors."
else
    log "Poll complete: $NEW_COUNT new, $SKIP_COUNT already handled."
    notify "Drive poll ran" "$NEW_COUNT new, $SKIP_COUNT already handled."
fi
