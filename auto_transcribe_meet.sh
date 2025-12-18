#!/bin/bash
#
# Auto-transcribe Google Meet recordings with timestamped filenames
# Converts video -> M4A and transcribes in both English and Hebrew
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
WHISPER_MODEL="medium"

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
EN_TRANSCRIPT="${OUTPUT_DIR}/${BASE_NAME}-en.txt"
HE_TRANSCRIPT="${OUTPUT_DIR}/${BASE_NAME}-he.txt"

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

# Step 2: Transcribe in English (if not already exists)
if [ -f "$EN_TRANSCRIPT" ]; then
    log "English transcript already exists: $EN_TRANSCRIPT"
else
    log "Transcribing in English..."
    python3 "$TOOLS_DIR/transcribe.py" \
        "$M4A_FILE" \
        --model "$WHISPER_MODEL" \
        --lang en

    # Rename the output to our format
    TEMP_EN="${OUTPUT_DIR}/${BASE_NAME}.txt"
    if [ -f "$TEMP_EN" ]; then
        mv "$TEMP_EN" "$EN_TRANSCRIPT"
        log "English transcript saved: $EN_TRANSCRIPT"
    else
        log "ERROR: English transcription failed"
        exit 1
    fi
fi

# Step 3: Transcribe in Hebrew (if not already exists)
if [ -f "$HE_TRANSCRIPT" ]; then
    log "Hebrew transcript already exists: $HE_TRANSCRIPT"
else
    log "Transcribing in Hebrew..."
    python3 "$TOOLS_DIR/transcribe.py" \
        "$M4A_FILE" \
        --model "$WHISPER_MODEL" \
        --lang he

    # Rename the output to our format
    TEMP_HE="${OUTPUT_DIR}/${BASE_NAME}.txt"
    if [ -f "$TEMP_HE" ]; then
        mv "$TEMP_HE" "$HE_TRANSCRIPT"
        log "Hebrew transcript saved: $HE_TRANSCRIPT"
    else
        log "ERROR: Hebrew transcription failed"
        exit 1
    fi
fi

log "=========================================="
log "Auto-transcription completed successfully!"
log "Output files:"
log "  - Audio: $M4A_FILE"
log "  - English: $EN_TRANSCRIPT"
log "  - Hebrew: $HE_TRANSCRIPT"
log "=========================================="

# Deactivate virtual environment
deactivate
