#!/bin/bash
#
# Enqueue a transcription job. Designed for macOS Folder Actions.
#
# Folder Actions can dump N files at once; running them in parallel loads
# multiple MLX whisper models simultaneously and triggers OOM on
# memory-constrained machines. This script writes a job descriptor to a
# queue directory and (if no worker is running) spawns a single
# auto_transcribe_worker.sh that drains the queue serially.
#
# The output base name is computed here, at enqueue time, and stored in
# the job file. That way a crashed-and-re-run job lands on the same output
# paths — the per-output `-f && skip` checks in transcribe_one.sh make
# re-runs idempotent.
#
# Usage: auto_transcribe_meet.sh /path/to/video_or_audio
#

set -e
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "ERROR: .env file not found. Copy .env.example to .env and configure it."
    exit 1
fi

for var in TOOLS_DIR OUTPUT_DIR LOG_FILE; do
    if [ -z "${!var}" ]; then
        echo "ERROR: $var is not set in .env"
        exit 1
    fi
done

QUEUE_DIR="${QUEUE_DIR:-$OUTPUT_DIR/.queue}"
mkdir -p "$QUEUE_DIR" "$OUTPUT_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [QUEUE] $1" | tee -a "$LOG_FILE"
}

sanitize_filename() {
    echo "$1" | tr ' /:' '---'
}

if [ $# -eq 0 ]; then
    log "ERROR: No file provided"
    exit 1
fi

INPUT_FILE="$1"

if [ ! -f "$INPUT_FILE" ]; then
    log "ERROR: File not found: $INPUT_FILE"
    exit 1
fi

# Resolve to absolute path so re-runs and dedupe scans match reliably.
INPUT_ABS="$(cd "$(dirname "$INPUT_FILE")" && pwd)/$(basename "$INPUT_FILE")"

# Short enqueue mutex — protects dedupe-scan + write window so two
# Folder Action invocations for the same path can't both pass the dedupe
# check. Held for milliseconds.
ENQUEUE_LOCK="$QUEUE_DIR/.enqueue.lock"
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if mkdir "$ENQUEUE_LOCK" 2>/dev/null; then
        trap 'rmdir "$ENQUEUE_LOCK" 2>/dev/null || true' EXIT
        break
    fi
    sleep 0.1
done

# Dedupe: if the exact same absolute path is already pending, skip.
for job in "$QUEUE_DIR"/*.job; do
    [ -e "$job" ] || continue
    if grep -qxF "INPUT_PATH=$INPUT_ABS" "$job" 2>/dev/null; then
        log "Duplicate skipped (already queued): $INPUT_ABS"
        exit 0
    fi
done

# Compute output base name now, freeze it in the job file.
TIMESTAMP=$(date '+%Y-%m-%d-%H-%M')
FILENAME=$(basename "$INPUT_ABS")
ORIGINAL_FILENAME="${FILENAME%.*}"
SANITIZED_NAME=$(sanitize_filename "$ORIGINAL_FILENAME")
BASE_NAME="${TIMESTAMP}-${SANITIZED_NAME}"

# Atomic job write: temp file + rename. Nanosecond timestamp + pid for
# uniqueness; `date +%N` is non-standard but works on Darwin 25.
JOB_ID="$(date +%s%N)-$$"
TMP_JOB="$QUEUE_DIR/.tmp.$JOB_ID"
FINAL_JOB="$QUEUE_DIR/${JOB_ID}.job"

{
    echo "INPUT_PATH=$INPUT_ABS"
    echo "BASE_NAME=$BASE_NAME"
} > "$TMP_JOB"
mv "$TMP_JOB" "$FINAL_JOB"

log "Enqueued: $INPUT_ABS (base: $BASE_NAME, job: $(basename "$FINAL_JOB"))"

# Release enqueue lock before spawning worker — worker takes its own lock.
rmdir "$ENQUEUE_LOCK" 2>/dev/null || true
trap - EXIT

# Try to spawn a worker. The worker itself races for the worker lock
# (`mkdir $QUEUE_DIR/.worker.lock`); losers exit cheaply before loading
# anything heavy, so a double-spawn from concurrent enqueues is safe.
WORKER_LOCK="$QUEUE_DIR/.worker.lock"
if [ -d "$WORKER_LOCK" ]; then
    # Check liveness for stale lock recovery.
    WORKER_PID="$(cat "$WORKER_LOCK/pid" 2>/dev/null || echo "")"
    if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
        log "Worker already running (pid $WORKER_PID), job will be picked up."
        exit 0
    fi
    log "Stale worker lock detected (pid $WORKER_PID), clearing."
    rm -rf "$WORKER_LOCK"
fi

# nohup + disown so the worker survives Automator/Folder Action teardown.
# Stdout goes to /dev/null because the worker's log() already writes to
# $LOG_FILE — redirecting here would double-write every line. Stderr is
# captured separately so unexpected failures still surface.
log "Spawning worker..."
nohup "$SCRIPT_DIR/auto_transcribe_worker.sh" </dev/null >/dev/null 2>>"$LOG_FILE" &
disown $! 2>/dev/null || true
log "Worker spawned (pid $!)."
