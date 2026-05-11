#!/bin/bash
#
# Show transcription queue status: worker state, pending jobs, currently
# processing job, recent log tail. Read-only — does not touch the queue.
#
# Usage: ./view.sh [--tail N]
#

set -e

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
WORKER_LOCK="$QUEUE_DIR/.worker.lock"
TAIL_LINES=15

while [ $# -gt 0 ]; do
    case "$1" in
        --tail) TAIL_LINES="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

echo "── Transcription queue status ────────────────────────────"
echo "Queue dir: $QUEUE_DIR"
echo

# Worker status
if [ -d "$WORKER_LOCK" ]; then
    WORKER_PID="$(cat "$WORKER_LOCK/pid" 2>/dev/null || echo "?")"
    if [ "$WORKER_PID" != "?" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
        WORKER_ETIME="$(ps -p "$WORKER_PID" -o etime= 2>/dev/null | tr -d ' ' || true)"
        echo "Worker:    RUNNING (pid $WORKER_PID, up $WORKER_ETIME)"
    else
        echo "Worker:    STALE LOCK (pid $WORKER_PID — process not alive)"
    fi
else
    echo "Worker:    idle (no lock)"
fi

# Currently transcribing — look for a python child of the worker.
# pgrep returns non-zero when no match exists; we suppress that so a
# between-jobs state (worker alive, no python running) doesn't kill
# this script under `set -e`.
if [ -d "$WORKER_LOCK" ] && [ -n "${WORKER_PID:-}" ] && [ "$WORKER_PID" != "?" ]; then
    MID_PID="$(pgrep -P "$WORKER_PID" 2>/dev/null | head -1 || true)"
    if [ -n "$MID_PID" ]; then
        PY_PID="$(pgrep -P "$MID_PID" 2>/dev/null | head -1 || true)"
    else
        PY_PID=""
    fi
    if [ -n "$PY_PID" ]; then
        # `ps` exits non-zero if pid disappears between pgrep and here;
        # tolerate that so view.sh keeps producing a snapshot.
        PY_CMD="$(ps -p "$PY_PID" -o command= 2>/dev/null || true)"
        PY_CPU="$(ps -p "$PY_PID" -o %cpu=    2>/dev/null | tr -d ' ' || true)"
        PY_MEM="$(ps -p "$PY_PID" -o %mem=    2>/dev/null | tr -d ' ' || true)"
        # Pull out the --model and --lang flags for at-a-glance signal.
        MODEL="$(echo "$PY_CMD" | sed -n 's/.*--model \([^ ]*\).*/\1/p')"
        LANG="$(echo "$PY_CMD" | sed -n 's/.*--lang \([^ ]*\).*/\1/p')"
        DIARIZE=""
        echo "$PY_CMD" | grep -q -- "--diarize" && DIARIZE=" (diarized)"
        echo "Active:    pid $PY_PID  cpu ${PY_CPU}%  mem ${PY_MEM}%  model=$MODEL  lang=$LANG${DIARIZE}"
    else
        echo "Active:    none (worker between jobs)"
    fi
fi

# Queue contents
echo
echo "Pending jobs:"
JOBS=()
for j in "$QUEUE_DIR"/*.job; do
    [ -e "$j" ] || continue
    JOBS+=("$j")
done

if [ "${#JOBS[@]}" -eq 0 ]; then
    echo "  (queue empty)"
else
    # Sort by filename (nanosecond timestamp prefix = FIFO order).
    IFS=$'\n' SORTED=($(printf '%s\n' "${JOBS[@]}" | sort))
    unset IFS
    i=1
    for j in "${SORTED[@]}"; do
        INPUT_PATH="$(grep '^INPUT_PATH=' "$j" 2>/dev/null | cut -d= -f2-)"
        BASE_NAME="$(grep '^BASE_NAME='  "$j" 2>/dev/null | cut -d= -f2-)"
        printf "  %d. %s\n     base: %s\n" "$i" "$INPUT_PATH" "$BASE_NAME"
        i=$((i + 1))
    done
fi

# Log tail
if [ -f "$LOG_FILE" ]; then
    echo
    echo "── Last $TAIL_LINES log lines ──"
    tail -n "$TAIL_LINES" "$LOG_FILE"
fi
