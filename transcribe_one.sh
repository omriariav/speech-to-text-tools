#!/bin/bash
#
# Process a single transcription job. Called by auto_transcribe_worker.sh
# (and usable standalone for manual runs).
#
# Reads:
#   INPUT_PATH — absolute path to the source audio/video
#   BASE_NAME  — timestamped+sanitized output base (computed at enqueue time
#                so crash recovery re-runs land on the same output paths)
#
# Both can be passed as args or env vars. CLI form:
#   ./transcribe_one.sh /abs/path/to/file.mp4 2026-05-11-21-14-meeting
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

for var in TOOLS_DIR VENV_DIR OUTPUT_DIR LOG_FILE; do
    if [ -z "${!var}" ]; then
        echo "ERROR: $var is not set in .env"
        exit 1
    fi
done

INPUT_PATH="${1:-${INPUT_PATH:-}}"
BASE_NAME="${2:-${BASE_NAME:-}}"

if [ -z "$INPUT_PATH" ] || [ -z "$BASE_NAME" ]; then
    echo "Usage: $0 <input-path> <base-name>" >&2
    exit 1
fi

if [ ! -f "$INPUT_PATH" ]; then
    echo "ERROR: File not found: $INPUT_PATH" >&2
    exit 1
fi

# Engine selection — empty/unset triggers transcribe.py's auto-detect.
ENGINE_FLAG=()
if [ -n "${TRANSCRIPTION_ENGINE:-}" ]; then
    ENGINE_FLAG=(--engine "$TRANSCRIPTION_ENGINE")
fi

TRANSCRIPT_LANGS="${TRANSCRIPT_LANGS:-he}"
case "$TRANSCRIPT_LANGS" in
    he)   DO_HE=true;  DO_EN=false ;;
    en)   DO_HE=false; DO_EN=true  ;;
    both) DO_HE=true;  DO_EN=true  ;;
    *)
        echo "ERROR: TRANSCRIPT_LANGS must be 'he', 'en', or 'both' (got: '$TRANSCRIPT_LANGS')" >&2
        exit 1
        ;;
esac

mkdir -p "$OUTPUT_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

INPUT_DIR=$(dirname "$INPUT_PATH")
FILENAME=$(basename "$INPUT_PATH")
ORIGINAL_FILENAME="${FILENAME%.*}"

M4A_FILE="${OUTPUT_DIR}/${BASE_NAME}.m4a"
FAST_HE="${OUTPUT_DIR}/${BASE_NAME}-he.txt"
FAST_EN="${OUTPUT_DIR}/${BASE_NAME}-en.txt"
DIARIZED_HE="${OUTPUT_DIR}/${BASE_NAME}-he-diarized.txt"
DIARIZED_EN="${OUTPUT_DIR}/${BASE_NAME}-en-diarized.txt"

log "=========================================="
log "Starting transcription job"
log "Input: $INPUT_PATH"
log "Base name: $BASE_NAME"
log "Output directory: $OUTPUT_DIR"

log "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

export PATH="/opt/homebrew/bin:$PATH"

if [ -n "$TRANSCRIPTION_ENGINE" ]; then
    log "Engine: $TRANSCRIPTION_ENGINE"
else
    log "Engine: auto-detect (transcribe.py picks best available)"
fi

# Step 1: Get M4A audio file (convert if needed)
if [ -f "$M4A_FILE" ]; then
    log "Audio file already exists: $M4A_FILE"
elif [[ "$INPUT_PATH" =~ \.[mM]4[aA]$ ]]; then
    log "Input is already M4A, copying to output..."
    cp "$INPUT_PATH" "$M4A_FILE"
    log "Audio copied: $M4A_FILE"
else
    log "Converting video to M4A..."
    if ! python3 "$TOOLS_DIR/video_converter.py" \
        "$INPUT_PATH" \
        --output "$OUTPUT_DIR" \
        --format m4a \
        --bitrate 192k >/dev/null 2>>"$LOG_FILE"; then
        log "ERROR: video_converter.py exited non-zero — see stderr above in this log"
        exit 1
    fi

    TEMP_M4A="${OUTPUT_DIR}/${ORIGINAL_FILENAME}.m4a"
    if [ -f "$TEMP_M4A" ]; then
        mv "$TEMP_M4A" "$M4A_FILE"
        log "Audio saved: $M4A_FILE"
    else
        log "ERROR: video_converter.py succeeded but expected output not found"
        log "Expected file: $TEMP_M4A"
        exit 1
    fi
fi

# Step 2: Fast transcription (Whisper only, no diarization)
if [ "$ENABLE_FAST" = true ]; then
    log "--- Fast transcription (model: $FAST_MODEL, langs: $TRANSCRIPT_LANGS) ---"

    if [ "$DO_HE" = true ]; then
        if [ -f "$FAST_HE" ]; then
            log "Fast Hebrew transcript already exists: $FAST_HE"
        else
            log "Fast transcribing in Hebrew..."
            python3 "$TOOLS_DIR/transcribe.py" \
                "$M4A_FILE" \
                --model "$FAST_MODEL" \
                "${ENGINE_FLAG[@]}" \
                --lang he \
                --output "$FAST_HE" >/dev/null 2>>"$LOG_FILE"
            log "Fast Hebrew transcript saved: $FAST_HE"
        fi
    fi

    if [ "$DO_EN" = true ]; then
        if [ -f "$FAST_EN" ]; then
            log "Fast English transcript already exists: $FAST_EN"
        else
            log "Fast transcribing in English..."
            python3 "$TOOLS_DIR/transcribe.py" \
                "$M4A_FILE" \
                --model "$FAST_MODEL" \
                "${ENGINE_FLAG[@]}" \
                --lang en \
                --output "$FAST_EN" >/dev/null 2>>"$LOG_FILE"
            log "Fast English transcript saved: $FAST_EN"
        fi
    fi
fi

# Step 3: Diarized transcription (Whisper + Pyannote speaker identification)
if [ "$ENABLE_DIARIZATION" = true ]; then
    log "--- Diarized transcription (model: $DIARIZE_MODEL, langs: $TRANSCRIPT_LANGS) ---"

    if [ "$DO_HE" = true ]; then
        if [ -f "$DIARIZED_HE" ]; then
            log "Diarized Hebrew transcript already exists: $DIARIZED_HE"
        else
            log "Diarized transcribing in Hebrew..."
            python3 "$TOOLS_DIR/transcribe.py" \
                "$M4A_FILE" \
                --model "$DIARIZE_MODEL" \
                "${ENGINE_FLAG[@]}" \
                --lang he \
                --diarize \
                --output "$DIARIZED_HE" >/dev/null 2>>"$LOG_FILE"
            log "Diarized Hebrew transcript saved: $DIARIZED_HE"
        fi
    fi

    if [ "$DO_EN" = true ]; then
        if [ -f "$DIARIZED_EN" ]; then
            log "Diarized English transcript already exists: $DIARIZED_EN"
        else
            log "Diarized transcribing in English..."
            python3 "$TOOLS_DIR/transcribe.py" \
                "$M4A_FILE" \
                --model "$DIARIZE_MODEL" \
                "${ENGINE_FLAG[@]}" \
                --lang en \
                --diarize \
                --output "$DIARIZED_EN" >/dev/null 2>>"$LOG_FILE"
            log "Diarized English transcript saved: $DIARIZED_EN"
        fi
    fi
fi

log "Job completed: $BASE_NAME"
log "  Fast transcription: $([ "$ENABLE_FAST" = true ] && echo "ENABLED ($FAST_MODEL)" || echo "DISABLED")"
log "  Diarization: $([ "$ENABLE_DIARIZATION" = true ] && echo "ENABLED ($DIARIZE_MODEL)" || echo "DISABLED")"
log "  Languages: $TRANSCRIPT_LANGS"
log "=========================================="

deactivate
