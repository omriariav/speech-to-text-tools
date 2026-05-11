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
WORKER_LOCK="$QUEUE_DIR/.worker.lock"

mkdir -p "$QUEUE_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WORKER] $1" | tee -a "$LOG_FILE"
}

# macOS Notification Center toast. Failures here must never kill the
# worker, so 2>/dev/null and a guard against osascript missing.
notify() {
    [ -x "$(command -v osascript)" ] || return 0
    local title="$1"
    local message="$2"
    osascript -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\"" 2>/dev/null || true
}

# Atomic lock acquisition. If a stale lock points to a dead pid, clear
# and retry once. Two concurrent workers race here; the loser exits
# before touching anything expensive.
acquire_lock() {
    if mkdir "$WORKER_LOCK" 2>/dev/null; then
        return 0
    fi
    local stale_pid
    stale_pid="$(cat "$WORKER_LOCK/pid" 2>/dev/null || echo "")"
    if [ -n "$stale_pid" ] && kill -0 "$stale_pid" 2>/dev/null; then
        return 1
    fi
    log "Clearing stale lock (pid $stale_pid)"
    rm -rf "$WORKER_LOCK"
    mkdir "$WORKER_LOCK" 2>/dev/null
}

if ! acquire_lock; then
    # Another worker is alive. Quiet exit — the existing worker will
    # process whatever this caller just enqueued.
    exit 0
fi

echo "$$" > "$WORKER_LOCK/pid"
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
    while IFS='=' read -r key value; do
        case "$key" in
            INPUT_PATH) INPUT_PATH="$value" ;;
            BASE_NAME)  BASE_NAME="$value" ;;
        esac
    done < "$JOB"

    DEPTH="$(queue_depth)"
    log "Processing $(basename "$JOB") (queue depth: $DEPTH)"
    log "  INPUT_PATH=$INPUT_PATH"
    log "  BASE_NAME=$BASE_NAME"
    notify "Transcription started" "$(basename "$INPUT_PATH") (queue: $DEPTH)"

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

    # Run pipeline. Don't let a single failure kill the worker — log and
    # move on so the rest of the queue still drains.
    if "$SCRIPT_DIR/transcribe_one.sh" "$INPUT_PATH" "$BASE_NAME"; then
        log "Job succeeded: $BASE_NAME"
        notify "Transcription done" "$(basename "$INPUT_PATH")"
    else
        EXIT_CODE=$?
        log "Job FAILED (exit $EXIT_CODE): $BASE_NAME — see log above for details"
        notify "Transcription FAILED" "$(basename "$INPUT_PATH") — check log"
    fi

    rm -f "$JOB"
done
