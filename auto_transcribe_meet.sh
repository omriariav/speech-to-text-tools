#!/bin/bash
#
# Auto-transcribe Google Meet recordings with timestamped filenames
# Converts video -> M4A and transcribes in both English and Hebrew
# Includes speaker diarization to identify different speakers
# Accepts any video format supported by ffmpeg (with or without extension)
# If input is already M4A, skips conversion and transcribes directly
#
# Usage: auto_transcribe_meet.sh /path/to/video_or_audio
#

set -e  # Exit on error

# Configuration
TOOLS_DIR="/Users/omri.a/Code/speech-to-text-tools"
VENV_DIR="$TOOLS_DIR/whisper-env"
LOG_FILE="/Users/omri.a/Code/omri_plygrnd/meetings_context/auto_transcribe.log"
OUTPUT_DIR="/Users/omri.a/Code/omri_plygrnd/meetings_context"
ENABLE_FAST=true           # Fast Whisper-only transcription (no diarization)
FAST_MODEL="medium"        # Model for fast transcription
ENABLE_DIARIZATION=true    # Slower transcription with speaker identification
DIARIZE_MODEL="medium"     # Model for diarized transcription

# Function to log messages
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Function to sanitize filename (replace spaces with hyphens, remove special chars)
sanitize_filename() {
    echo "$1" | sed 's/ /-/g' | sed 's/[^a-zA-Z0-9._-]//g'
}

# Check if file was provided
if [ $# -eq 0 ]; then
    log "ERROR: No file provided"
    exit 1
fi

INPUT_FILE="$1"

# Check if input file exists
if [ ! -f "$INPUT_FILE" ]; then
    log "ERROR: File not found: $INPUT_FILE"
    exit 1
fi

log "=========================================="
log "Starting auto-transcription"
log "Input: $INPUT_FILE"

# Extract directory and filename (strip any extension)
INPUT_DIR=$(dirname "$INPUT_FILE")
FILENAME=$(basename "$INPUT_FILE")
ORIGINAL_FILENAME="${FILENAME%.*}"
# If no extension was present, ORIGINAL_FILENAME equals FILENAME
[ "$ORIGINAL_FILENAME" = "$FILENAME" ] || true

# Get current timestamp (when processing starts)
TIMESTAMP=$(date '+%Y-%m-%d-%H-%M')
log "Timestamp: $TIMESTAMP"

# Sanitize the original filename
SANITIZED_NAME=$(sanitize_filename "$ORIGINAL_FILENAME")

# Create timestamped base name
BASE_NAME="${TIMESTAMP}-${SANITIZED_NAME}"
log "Base name: $BASE_NAME"

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# Define output files (in meetings_context directory)
M4A_FILE="${OUTPUT_DIR}/${BASE_NAME}.m4a"
FAST_HE="${OUTPUT_DIR}/${BASE_NAME}-he.txt"
FAST_EN="${OUTPUT_DIR}/${BASE_NAME}-en.txt"
DIARIZED_HE="${OUTPUT_DIR}/${BASE_NAME}-he-diarized.txt"
DIARIZED_EN="${OUTPUT_DIR}/${BASE_NAME}-en-diarized.txt"

log "Output directory: $OUTPUT_DIR"

# Activate virtual environment
log "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# Add ffmpeg to PATH (Automator doesn't have Homebrew paths)
export PATH="/opt/homebrew/bin:$PATH"

# Step 1: Get M4A audio file (convert if needed)
if [ -f "$M4A_FILE" ]; then
    log "Audio file already exists: $M4A_FILE"
elif [[ "$INPUT_FILE" =~ \.[mM]4[aA]$ ]]; then
    # Input is already M4A - copy it directly
    log "Input is already M4A, copying to output..."
    cp "$INPUT_FILE" "$M4A_FILE"
    log "Audio copied: $M4A_FILE"
else
    # Convert video to M4A
    log "Converting video to M4A..."
    CONVERSION_OUTPUT=$(python3 "$TOOLS_DIR/video_converter.py" \
        "$INPUT_FILE" \
        --output "$OUTPUT_DIR" \
        --format m4a \
        --bitrate 192k 2>&1)
    CONVERSION_STATUS=$?

    # Log the conversion output
    echo "$CONVERSION_OUTPUT" | tee -a "$LOG_FILE"

    # Rename the output to our timestamped format
    # video_converter.py keeps original filename (with spaces), not sanitized
    TEMP_M4A="${OUTPUT_DIR}/${ORIGINAL_FILENAME}.m4a"
    if [ -f "$TEMP_M4A" ]; then
        mv "$TEMP_M4A" "$M4A_FILE"
        log "Audio saved: $M4A_FILE"
    else
        log "ERROR: Audio conversion failed (exit code: $CONVERSION_STATUS)"
        log "Expected file: $TEMP_M4A"
        exit 1
    fi
fi

# Step 2: Fast transcription (Whisper only, no diarization)
if [ "$ENABLE_FAST" = true ]; then
    log "--- Fast transcription (model: $FAST_MODEL) ---"

    if [ -f "$FAST_HE" ]; then
        log "Fast Hebrew transcript already exists: $FAST_HE"
    else
        log "Fast transcribing in Hebrew..."
        python3 "$TOOLS_DIR/transcribe.py" \
            "$M4A_FILE" \
            --model "$FAST_MODEL" \
            --lang he \
            --output "$FAST_HE"
        log "Fast Hebrew transcript saved: $FAST_HE"
    fi

    if [ -f "$FAST_EN" ]; then
        log "Fast English transcript already exists: $FAST_EN"
    else
        log "Fast transcribing in English..."
        python3 "$TOOLS_DIR/transcribe.py" \
            "$M4A_FILE" \
            --model "$FAST_MODEL" \
            --lang en \
            --output "$FAST_EN"
        log "Fast English transcript saved: $FAST_EN"
    fi
fi

# Step 3: Diarized transcription (Whisper + Pyannote speaker identification)
if [ "$ENABLE_DIARIZATION" = true ]; then
    log "--- Diarized transcription (model: $DIARIZE_MODEL) ---"

    if [ -f "$DIARIZED_HE" ]; then
        log "Diarized Hebrew transcript already exists: $DIARIZED_HE"
    else
        log "Diarized transcribing in Hebrew..."
        python3 "$TOOLS_DIR/transcribe.py" \
            "$M4A_FILE" \
            --model "$DIARIZE_MODEL" \
            --lang he \
            --diarize \
            --output "$DIARIZED_HE"
        log "Diarized Hebrew transcript saved: $DIARIZED_HE"
    fi

    if [ -f "$DIARIZED_EN" ]; then
        log "Diarized English transcript already exists: $DIARIZED_EN"
    else
        log "Diarized transcribing in English..."
        python3 "$TOOLS_DIR/transcribe.py" \
            "$M4A_FILE" \
            --model "$DIARIZE_MODEL" \
            --lang en \
            --diarize \
            --output "$DIARIZED_EN"
        log "Diarized English transcript saved: $DIARIZED_EN"
    fi
fi

log "=========================================="
log "Auto-transcription completed successfully!"
log "  Fast transcription: $([ "$ENABLE_FAST" = true ] && echo "ENABLED ($FAST_MODEL)" || echo "DISABLED")"
log "  Diarization: $([ "$ENABLE_DIARIZATION" = true ] && echo "ENABLED ($DIARIZE_MODEL)" || echo "DISABLED")"
log "Output files:"
log "  - Audio: $M4A_FILE"
[ "$ENABLE_FAST" = true ] && log "  - Hebrew (fast): $FAST_HE"
[ "$ENABLE_FAST" = true ] && log "  - English (fast): $FAST_EN"
[ "$ENABLE_DIARIZATION" = true ] && log "  - Hebrew (diarized): $DIARIZED_HE"
[ "$ENABLE_DIARIZATION" = true ] && log "  - English (diarized): $DIARIZED_EN"
log "=========================================="

# Deactivate virtual environment
deactivate
