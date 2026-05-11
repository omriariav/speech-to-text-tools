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
    # printf, not echo: a filename starting with `-n`/`-e` would be eaten
    # by echo's flag parsing under some bash builds.
    printf '%s' "$1" | tr ' /:' '---'
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
# check. Real critical section is sub-second; we wait up to ~5s, and if
# we still can't get it we check for a stale holder (mkdir-orphan from a
# killed previous enqueuer) and reclaim via atomic rename. Losing an
# enqueue invocation is a data-loss event for Folder Actions (no retry),
# so failing-closed by exit-1 is worse than this auto-recovery.
ENQUEUE_LOCK="$QUEUE_DIR/.enqueue.lock"
ENQUEUE_STALE_AGE=5
ENQUEUE_LOCK_OWNED=false

try_acquire_enqueue_lock() {
    local attempt
    for attempt in $(seq 1 50); do
        if mkdir "$ENQUEUE_LOCK" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

if try_acquire_enqueue_lock; then
    ENQUEUE_LOCK_OWNED=true
else
    # 5s without success — assume a previous holder died mid-section.
    # Atomic rename picks exactly one reclaimer if multiple invocations
    # race here simultaneously.
    lock_birth="$(stat -f %m "$ENQUEUE_LOCK" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    lock_age=$(( now - lock_birth ))
    if [ "$lock_age" -ge "$ENQUEUE_STALE_AGE" ]; then
        dead_name="${ENQUEUE_LOCK}.dead.$$.$(date +%s%N)"
        if mv "$ENQUEUE_LOCK" "$dead_name" 2>/dev/null; then
            rm -rf "$dead_name"
            log "Reclaimed stale enqueue lock (age=${lock_age}s)"
            if try_acquire_enqueue_lock; then
                ENQUEUE_LOCK_OWNED=true
            fi
        else
            # Another invocation just reclaimed it; one more brief wait.
            if try_acquire_enqueue_lock; then
                ENQUEUE_LOCK_OWNED=true
            fi
        fi
    fi
fi

if [ "$ENQUEUE_LOCK_OWNED" != true ]; then
    log "ERROR: could not acquire enqueue mutex; aborting"
    exit 1
fi
trap 'if [ "$ENQUEUE_LOCK_OWNED" = true ]; then rmdir "$ENQUEUE_LOCK" 2>/dev/null || true; fi' EXIT

# Dedupe: if the exact same absolute path is already pending, skip.
for job in "$QUEUE_DIR"/*.job; do
    [ -e "$job" ] || continue
    if grep -qxF "INPUT_PATH=$INPUT_ABS" "$job" 2>/dev/null; then
        log "Duplicate skipped (already queued): $INPUT_ABS"
        exit 0
    fi
done

# Compute output base name now, freeze it in the job file. The
# timestamp uses seconds AND a short content-derived suffix of the
# absolute input path, so two same-named files from different folders
# (or even the same folder in the same second) get distinct BASE_NAMEs.
# Without the suffix, the `-f && skip` checks in transcribe_one.sh
# would silently mask one file's transcripts with another's.
TIMESTAMP=$(date '+%Y-%m-%d-%H-%M-%S')
FILENAME=$(basename "$INPUT_ABS")
ORIGINAL_FILENAME="${FILENAME%.*}"
SANITIZED_NAME=$(sanitize_filename "$ORIGINAL_FILENAME")
PATH_HASH="$(printf '%s' "$INPUT_ABS" | md5 -q 2>/dev/null | head -c 6)"
BASE_NAME="${TIMESTAMP}-${SANITIZED_NAME}-${PATH_HASH}"

# Atomic job write: temp file + rename. Nanosecond timestamp + pid for
# uniqueness; `date +%N` is non-standard but works on Darwin 25.
JOB_ID="$(date +%s%N)-$$"
TMP_JOB="$QUEUE_DIR/.tmp.$JOB_ID"
FINAL_JOB="$QUEUE_DIR/${JOB_ID}.job"

# printf, not echo: avoids the `-n` flag-parsing pitfall.
{
    printf 'INPUT_PATH=%s\n' "$INPUT_ABS"
    printf 'BASE_NAME=%s\n'  "$BASE_NAME"
} > "$TMP_JOB"
mv "$TMP_JOB" "$FINAL_JOB"

log "Enqueued: $INPUT_ABS (base: $BASE_NAME, job: $(basename "$FINAL_JOB"))"

# Release enqueue mutex before spawning worker.
rmdir "$ENQUEUE_LOCK" 2>/dev/null || true
ENQUEUE_LOCK_OWNED=false
trap - EXIT

# Always attempt to spawn a worker. The worker itself is the only thing
# that touches $QUEUE_DIR/.worker.lock: it races for the lock via atomic
# mkdir, and a loser (one started while a sibling is already holding the
# lock) exits cheaply before touching anything heavy. The enqueuer used
# to inspect the lock here for stale-recovery, but that created a TOCTOU
# window where the enqueuer could rm -rf a live worker's lock between
# mkdir and the worker's pid-file write. Letting the worker own all
# locking eliminates that race.
#
# nohup + disown so the worker survives Automator/Folder Action teardown.
# Stdout to /dev/null because the worker's log() already writes to
# $LOG_FILE — redirecting here would double-write every line. Stderr is
# captured so unexpected failures still surface.
nohup "$SCRIPT_DIR/auto_transcribe_worker.sh" </dev/null >/dev/null 2>>"$LOG_FILE" &
disown $! 2>/dev/null || true
log "Worker ensured (spawn pid $!; may exit immediately if a sibling holds the lock)."
