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
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
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

QUEUE_DIR="${QUEUE_DIR:-$OUTPUT_DIR/.queue}"
STAGING_DIR="${STAGING_DIR:-$OUTPUT_DIR/.staging}"
DRIVE_LEDGER_DIR="${DRIVE_LEDGER_DIR:-$OUTPUT_DIR/.drive_done}"
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

# Single-instance guard. Hourly launchd runs won't normally overlap, but a
# slow download could; a second poller must not double-fetch. mkdir is atomic.
if ! mkdir "$POLL_LOCK" 2>/dev/null; then
    # Reclaim a stale lock from a killed previous run (older than 1h).
    lock_birth="$(stat -f %m "$POLL_LOCK" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    if [ "$(( now - lock_birth ))" -ge 3600 ]; then
        rm -rf "$POLL_LOCK" 2>/dev/null || true
        mkdir "$POLL_LOCK" 2>/dev/null || { log "Another poller holds the lock; exiting."; exit 0; }
        log "Reclaimed stale poll lock."
    else
        log "Another poller is running; exiting."
        notify "Drive poll skipped" "Another Drive poller is already running."
        exit 0
    fi
fi
trap 'rm -rf "$POLL_LOCK" 2>/dev/null || true' EXIT

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
    # GoogleDrive- substring, so the worker's start-delay is auto-skipped and
    # processing begins immediately. auto_transcribe_meet.sh spawns the worker.
    if "$SCRIPT_DIR/auto_transcribe_meet.sh" "$STAGED_PATH" >>"$LOG_FILE" 2>&1; then
        : > "$LEDGER_MARKER"
        printf 'name=%s\nstaged=%s\nat=%s\n' "$FILE_NAME" "$STAGED_PATH" "$(date '+%Y-%m-%d %H:%M:%S')" > "$LEDGER_MARKER"
        NEW_COUNT=$(( NEW_COUNT + 1 ))
        log "Enqueued: $(basename "$STAGED_PATH")"
    else
        log "ERROR: enqueue failed for $(basename "$STAGED_PATH"); will retry next poll."
        ERROR_COUNT=$(( ERROR_COUNT + 1 ))
        rm -f "$STAGED_PATH" 2>/dev/null || true
    fi
done < <(printf '%s' "$LIST_JSON" | jq -r '.files[] | select(.mime_type == "video/mp4") | [.id, .name] | @tsv')

if [ "$ERROR_COUNT" -gt 0 ]; then
    log "Poll complete: $NEW_COUNT new, $SKIP_COUNT already handled, $ERROR_COUNT errors."
    notify "Drive poll completed with errors" "$NEW_COUNT new, $SKIP_COUNT already handled, $ERROR_COUNT errors."
else
    log "Poll complete: $NEW_COUNT new, $SKIP_COUNT already handled."
    notify "Drive poll ran" "$NEW_COUNT new, $SKIP_COUNT already handled."
fi
