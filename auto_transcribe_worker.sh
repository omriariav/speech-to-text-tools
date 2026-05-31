#!/bin/bash
#
# Queue drainer for the transcription pipeline. One instance at a time,
# enforced via the $QUEUE_DIR/.worker.lock directory (atomic mkdir).
#
# Loop:
#   1. Pick the oldest .job file in $QUEUE_DIR
#   2. Read INPUT_PATH and BASE_NAME from it
#   3. Call transcribe_one.sh
#   4. Delete the job file regardless of success/failure (failure surfaces
#      in $LOG_FILE; re-running a job risks a busy-loop on a poison file).
#   5. When empty: wait WORKER_IDLE_GRACE_SECONDS for late drops, then exit.
#
# Worker is spawned by auto_transcribe_meet.sh via nohup; it does not run
# as a persistent daemon.
#

set -e
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "ERROR: .env file not found." >&2
    exit 1
fi

QUEUE_DIR="${QUEUE_DIR:-$OUTPUT_DIR/.queue}"
WORKER_IDLE_GRACE_SECONDS="${WORKER_IDLE_GRACE_SECONDS:-5}"
TRANSCRIPTION_START_DELAY_SECONDS="${TRANSCRIPTION_START_DELAY_SECONDS:-600}"
WORKER_LOCK="$QUEUE_DIR/.worker.lock"

mkdir -p "$QUEUE_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WORKER] $1" | tee -a "$LOG_FILE"
}

case "$TRANSCRIPTION_START_DELAY_SECONDS" in
    ''|*[!0-9]*)
        log "WARN: invalid TRANSCRIPTION_START_DELAY_SECONDS='$TRANSCRIPTION_START_DELAY_SECONDS'; defaulting to 600"
        TRANSCRIPTION_START_DELAY_SECONDS=600
        ;;
esac

# macOS Notification Center toast. Failures here must never kill the
# worker, so 2>/dev/null and a guard against osascript missing.
notify() {
    [ -x "$(command -v osascript)" ] || return 0
    local title="$1"
    local message="$2"
    osascript -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\"" 2>/dev/null || true
}

# Atomic lock acquisition. Two concurrent workers race here; the loser
# exits cheaply before touching anything expensive.
#
# The pid file is written inside this function, immediately after mkdir
# succeeds. That closes the window where another worker could see the
# lock dir without a pid file and incorrectly treat it as stale.
#
# Stale recovery uses both pid liveness AND a lock-age check: a lock
# whose recorded pid is dead OR whose dir is older than $STALE_LOCK_AGE
# seconds is reclaimable. The age guard protects the moment between
# mkdir and pid-file write in a sibling worker (very small window, but
# real).
STALE_LOCK_AGE=30

acquire_lock() {
    if mkdir "$WORKER_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$WORKER_LOCK/pid"
        return 0
    fi

    local holder_pid lock_birth lock_age now
    holder_pid="$(cat "$WORKER_LOCK/pid" 2>/dev/null || true)"

    if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then
        return 1
    fi

    # No pid yet, or pid dead. Use lock dir mtime to decide whether to
    # reclaim. `stat -f %m` is the BSD/macOS form (Linux is `stat -c %Y`).
    lock_birth="$(stat -f %m "$WORKER_LOCK" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    lock_age=$(( now - lock_birth ))

    if [ "$lock_age" -lt "$STALE_LOCK_AGE" ]; then
        # Probably a sibling worker that just mkdir'd and hasn't yet
        # written its pid. Defer to it; this worker exits cheaply.
        return 1
    fi

    # Stale-recovery is itself racey: two workers can both pass the age
    # check and both try to `rm -rf` + `mkdir`. To pick exactly one
    # winner, atomically rename the stale lock dir to a unique name —
    # only one concurrent `mv` succeeds (POSIX directory rename is
    # atomic within a filesystem). Losers see "source missing" and exit.
    local dead_name
    dead_name="${WORKER_LOCK}.dead.$$.$(date +%s%N)"
    if ! mv "$WORKER_LOCK" "$dead_name" 2>/dev/null; then
        return 1
    fi
    log "Won stale-lock recovery race (was pid='$holder_pid', age=${lock_age}s)"
    rm -rf "$dead_name"
    if mkdir "$WORKER_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$WORKER_LOCK/pid"
        return 0
    fi
    return 1
}

if ! acquire_lock; then
    # Another worker is alive (or is just starting). Quiet exit — that
    # worker will pick up whatever this caller just enqueued.
    exit 0
fi

trap 'rm -rf "$WORKER_LOCK" 2>/dev/null || true' EXIT

log "Started (pid $$)"

queue_depth() {
    local count=0
    for j in "$QUEUE_DIR"/*.job; do
        [ -e "$j" ] && count=$((count + 1))
    done
    echo "$count"
}

# Pick the oldest job by filename (lexicographically; filenames lead with
# a nanosecond timestamp, so lexicographic == FIFO).
oldest_job() {
    local oldest=""
    for j in "$QUEUE_DIR"/*.job; do
        [ -e "$j" ] || continue
        if [ -z "$oldest" ] || [ "$j" \< "$oldest" ]; then
            oldest="$j"
        fi
    done
    echo "$oldest"
}

job_mtime_epoch() {
    stat -f %m "$1" 2>/dev/null || date +%s
}

wait_for_start_delay() {
    local job="$1"
    local enqueued_at="$2"
    local now age remaining

    [ "$TRANSCRIPTION_START_DELAY_SECONDS" -eq 0 ] && return 0

    case "$enqueued_at" in
        ''|*[!0-9]*)
            enqueued_at="$(job_mtime_epoch "$job")"
            ;;
    esac

    now="$(date +%s)"
    age=$(( now - enqueued_at ))
    if [ "$age" -lt 0 ]; then
        age=0
    fi

    remaining=$(( TRANSCRIPTION_START_DELAY_SECONDS - age ))
    if [ "$remaining" -gt 0 ]; then
        log "Waiting ${remaining}s before processing $(basename "$job") (age: ${age}s; start delay: ${TRANSCRIPTION_START_DELAY_SECONDS}s)"
        sleep "$remaining"
    fi
}

while true; do
    JOB="$(oldest_job)"

    if [ -z "$JOB" ]; then
        log "Queue empty, idle grace ${WORKER_IDLE_GRACE_SECONDS}s..."
        sleep "$WORKER_IDLE_GRACE_SECONDS"
        JOB="$(oldest_job)"
        if [ -z "$JOB" ]; then
            log "Idle grace expired, exiting."
            exit 0
        fi
        log "Late drop detected, resuming drain."
    fi

    # Read job; tolerate missing fields by defaulting to empty.
    INPUT_PATH=""
    BASE_NAME=""
    ENQUEUED_AT=""
    while IFS='=' read -r key value; do
        case "$key" in
            INPUT_PATH) INPUT_PATH="$value" ;;
            BASE_NAME)  BASE_NAME="$value" ;;
            ENQUEUED_AT) ENQUEUED_AT="$value" ;;
        esac
    done < "$JOB"

    if [ -z "$INPUT_PATH" ] || [ -z "$BASE_NAME" ]; then
        log "ERROR: malformed job file, discarding: $JOB"
        rm -f "$JOB"
        continue
    fi

    if [ ! -f "$INPUT_PATH" ]; then
        log "ERROR: input file no longer exists, discarding job: $INPUT_PATH"
        rm -f "$JOB"
        continue
    fi

    wait_for_start_delay "$JOB" "$ENQUEUED_AT"

    DEPTH="$(queue_depth)"
    log "Processing $(basename "$JOB") (queue depth: $DEPTH)"
    log "  INPUT_PATH=$INPUT_PATH"
    log "  BASE_NAME=$BASE_NAME"
    notify "Transcription started" "$(basename "$INPUT_PATH") (queue: $DEPTH)"

    # Run pipeline. Don't let a single failure kill the worker — log and
    # move on so the rest of the queue still drains.
    #
    # Exit-code contract with transcribe_one.sh:
    #   0   — full pipeline succeeded, transcripts produced
    #   75  — intentionally skipped (language gate mismatch / skip marker)
    #   *   — failure; details in $LOG_FILE
    set +e
    "$SCRIPT_DIR/transcribe_one.sh" "$INPUT_PATH" "$BASE_NAME"
    EXIT_CODE=$?
    set -e
    case "$EXIT_CODE" in
        0)
            log "Job succeeded: $BASE_NAME"
            notify "Transcription done" "$(basename "$INPUT_PATH")"
            ;;
        75)
            log "Job skipped (language gate): $BASE_NAME"
            # No toast — skips are routine and shouldn't spam Notification Center.
            ;;
        *)
            log "Job FAILED (exit $EXIT_CODE): $BASE_NAME — see log above for details"
            # Google Drive shortcut files (.gdoc, .gsheet, ...) are not media
            # and always fail. Don't spam Notification Center for them.
            case "${INPUT_PATH##*.}" in
                gdoc|gsheet|gslides|gdraw|gform|gmap|gsite|gtable|glink) ;;
                *) notify "Transcription FAILED" "$(basename "$INPUT_PATH") — check log" ;;
            esac
            ;;
    esac

    rm -f "$JOB"
done
