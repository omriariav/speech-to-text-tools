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

# Sentinel exit code used to tell the worker "intentionally skipped, not
# a failure". Any value outside 0/1 works; 75 is unused by anything in
# the pipeline and avoids overlap with shell signal-based codes (128+N).
EXIT_LANGUAGE_SKIP=75

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

# Normalize a language code so the gate is robust to:
#   - casing  (HE / He / he → he)
#   - whitespace
#   - the legacy ISO-639-1 'iw' code that some toolchains still emit
#     where modern Whisper returns 'he'
#   - region subtags (he-IL, en-US → he, en)
# Used on both LANGUAGE_GATE and the detected language so a typo or
# alias in .env doesn't silently skip every recording.
normalize_lang() {
    local code="${1:-}"
    code="$(printf '%s' "$code" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    # Strip both `-` and `_` region delimiters (he-IL, he_IL → he).
    code="${code%%-*}"
    code="${code%%_*}"
    case "$code" in
        iw) code="he" ;;
    esac
    printf '%s' "$code"
}

INPUT_DIR=$(dirname "$INPUT_PATH")
FILENAME=$(basename "$INPUT_PATH")
ORIGINAL_FILENAME="${FILENAME%.*}"

M4A_FILE="${OUTPUT_DIR}/${BASE_NAME}.m4a"
FAST_HE="${OUTPUT_DIR}/${BASE_NAME}-he.txt"
FAST_EN="${OUTPUT_DIR}/${BASE_NAME}-en.txt"
DIARIZED_HE="${OUTPUT_DIR}/${BASE_NAME}-he-diarized.txt"
DIARIZED_EN="${OUTPUT_DIR}/${BASE_NAME}-en-diarized.txt"

# Stable per-input dedupe key for language-gate skips. BASE_NAME has an
# enqueue-time timestamp baked in, so a re-fire from Folder Actions gets a
# fresh BASE_NAME and bypasses any BASE_NAME-keyed marker. Use the full
# md5 of the absolute input path — the 6-char truncation in
# auto_transcribe_meet.sh is fine inside BASE_NAME (namespaced by
# timestamp+name) but as a standalone key here it would collide.
PATH_HASH="$(printf '%s' "$INPUT_PATH" | md5 -q 2>/dev/null)"
SKIPPED_DIR="${OUTPUT_DIR}/.skipped"
SKIPPED_MARKER="${SKIPPED_DIR}/${PATH_HASH}"

log "=========================================="
log "Starting transcription job"
log "Input: $INPUT_PATH"
log "Base name: $BASE_NAME"
log "Output directory: $OUTPUT_DIR"

# Fast path: if a previous run already gated this exact input out under the
# *currently configured* gate, no-op. We deliberately don't honor stale
# markers when LANGUAGE_GATE is unset or has changed — a config change
# should re-run detection, not silently keep skipping. Whitespace-only
# values are treated as unset (after normalize they're empty).
LANGUAGE_GATE_NORM=$(normalize_lang "${LANGUAGE_GATE:-}")
if [ -n "$LANGUAGE_GATE_NORM" ] && [ -f "$SKIPPED_MARKER" ]; then
    MARKER_CONTENTS=$(cat "$SKIPPED_MARKER" 2>/dev/null)
    MARKER_GATE=$(printf '%s\n' "$MARKER_CONTENTS" | sed -n 's/^.*gate=\([^ ]*\).*/\1/p')
    MARKER_PATH=$(printf '%s\n' "$MARKER_CONTENTS" | sed -n 's/^.*path=\(.*\)$/\1/p')
    if [ "$(normalize_lang "$MARKER_GATE")" = "$LANGUAGE_GATE_NORM" ] && [ "$MARKER_PATH" = "$INPUT_PATH" ]; then
        log "Skip marker present (matches current gate), no-op: $MARKER_CONTENTS"
        log "=========================================="
        exit "$EXIT_LANGUAGE_SKIP"
    fi
    log "Skip marker exists but gate/path mismatch — re-evaluating (marker: $MARKER_CONTENTS)"
fi

log "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

export PATH="/opt/homebrew/bin:$PATH"

if [ -n "$TRANSCRIPTION_ENGINE" ]; then
    log "Engine: $TRANSCRIPTION_ENGINE"
else
    log "Engine: auto-detect (transcribe.py picks best available)"
fi

# Step 0.5: materialize cloud-backed placeholders. macOS File Provider
# extensions (Google Drive, iCloud, etc.) expose remote files as on-disk
# stubs whose stat() size reports the real bytes but whose actual data
# isn't local yet. ffprobe/ffmpeg will choke on these. Force a full read
# so the FP streams bytes down synchronously before we touch the file.
#
# We don't filter by path because the cost of `cat > /dev/null` on an
# already-local file is just a sequential disk read (1-2s for hundreds
# of MB on SSD). Cheap insurance vs. the failure mode of silently
# rejecting a real recording.
#
# brctl download is iCloud-only — Google Drive paths return "Path is
# outside of any CloudDocs app library, will never sync". A blocking
# read works for any FP-backed provider.
if [ -f "$M4A_FILE" ]; then
    : # Output already exists — no need to touch the source file.
else
    ON_DISK_BYTES=$(( $(stat -f %b "$INPUT_PATH" 2>/dev/null || echo 0) * 512 ))
    TOTAL_BYTES=$(stat -f %z "$INPUT_PATH" 2>/dev/null || echo 0)
    if [ "$TOTAL_BYTES" -gt 0 ] && [ "$ON_DISK_BYTES" -lt "$TOTAL_BYTES" ]; then
        log "Cloud file partially materialized ($((ON_DISK_BYTES/1024/1024))MB / $((TOTAL_BYTES/1024/1024))MB). Forcing download..."
        MATERIALIZE_START=$(date +%s)
        if ! cat "$INPUT_PATH" > /dev/null 2>>"$LOG_FILE"; then
            log "ERROR: failed to materialize cloud file — File Provider read failed"
            exit 1
        fi
        log "Materialized in $(( $(date +%s) - MATERIALIZE_START ))s"
    fi
fi

# Step 1: Get M4A audio file (convert if needed)
# Track whether *this* run produced the M4A. If it was already present
# (resumed run, manual prep), downstream cleanup paths must not delete it.
M4A_CREATED_THIS_RUN=false
if [ -f "$M4A_FILE" ]; then
    log "Audio file already exists: $M4A_FILE"
elif [[ "$INPUT_PATH" =~ \.[mM]4[aA]$ ]]; then
    log "Input is already M4A, copying to output..."
    cp "$INPUT_PATH" "$M4A_FILE"
    M4A_CREATED_THIS_RUN=true
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
        M4A_CREATED_THIS_RUN=true
        log "Audio saved: $M4A_FILE"
    else
        log "ERROR: video_converter.py succeeded but expected output not found"
        log "Expected file: $TEMP_M4A"
        exit 1
    fi
fi

# Step 1.5: Language gate. When LANGUAGE_GATE is set (e.g. "he"), detect the
# spoken language from the first ~30s and bail out if it doesn't match. Keeps
# the pipeline from burning minutes diarizing English meetings when the user
# only cares about Hebrew (or vice versa). Unset = no gating.
if [ -n "$LANGUAGE_GATE_NORM" ]; then
    # Pick a detection model that we know is downloadable for the chosen
    # engine. Default order: explicit override > FAST_MODEL (when fast is on)
    # > DIARIZE_MODEL (diarize-only configs) > FAST_MODEL as last resort.
    if [ -n "${DETECT_MODEL:-}" ]; then
        DETECT_MODEL_EFFECTIVE="$DETECT_MODEL"
    elif [ "$ENABLE_FAST" = true ]; then
        DETECT_MODEL_EFFECTIVE="$FAST_MODEL"
    elif [ "$ENABLE_DIARIZATION" = true ]; then
        DETECT_MODEL_EFFECTIVE="$DIARIZE_MODEL"
    else
        DETECT_MODEL_EFFECTIVE="$FAST_MODEL"
    fi
    DETECT_SECONDS="${DETECT_SECONDS:-30}"
    log "--- Language detection (model: $DETECT_MODEL_EFFECTIVE, sample: ${DETECT_SECONDS}s, gate: $LANGUAGE_GATE_NORM) ---"
    if DETECTED_LANG=$(python3 "$TOOLS_DIR/transcribe.py" \
        "$M4A_FILE" \
        --model "$DETECT_MODEL_EFFECTIVE" \
        "${ENGINE_FLAG[@]}" \
        --detect-language \
        --detect-seconds "$DETECT_SECONDS" 2>>"$LOG_FILE"); then
        log "Detected language: $DETECTED_LANG"
    else
        log "ERROR: language detection failed — see log above. Skipping job."
        # Only clean up the m4a if we made it this run; an existing one
        # may belong to a resumed/partial prior run we shouldn't clobber.
        if [ "$M4A_CREATED_THIS_RUN" = true ]; then
            rm -f "$M4A_FILE"
        fi
        exit 1
    fi
    DETECTED_NORM=$(normalize_lang "$DETECTED_LANG")
    if [ "$DETECTED_NORM" != "$LANGUAGE_GATE_NORM" ]; then
        log "Language gate: detected '$DETECTED_NORM' != gate '$LANGUAGE_GATE_NORM'. Skipping transcription."
        # Record the skip so future Folder Action re-fires for this same
        # input path no-op instead of re-running detection. Store the
        # normalized forms so the fast-path comparison is order-insensitive
        # to .env edits like case/region tweaks.
        mkdir -p "$SKIPPED_DIR"
        printf 'lang=%s gate=%s date=%s path=%s\n' \
            "$DETECTED_NORM" "$LANGUAGE_GATE_NORM" "$(date '+%Y-%m-%d %H:%M:%S')" "$INPUT_PATH" \
            > "$SKIPPED_MARKER"
        if [ "$M4A_CREATED_THIS_RUN" = true ]; then
            rm -f "$M4A_FILE"
        fi
        log "Job skipped (language mismatch): $BASE_NAME"
        log "=========================================="
        deactivate
        exit "$EXIT_LANGUAGE_SKIP"
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
