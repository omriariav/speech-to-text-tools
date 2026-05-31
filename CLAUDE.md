# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Audio transcription toolkit using OpenAI's Whisper for speech-to-text. Converts videos to audio, transcribes audio files, and splits large recordings into segments.

## Setup

```bash
# Create and activate virtual environment
python -m venv whisper-env
source whisper-env/bin/activate  # macOS/Linux

# Install dependencies
pip install -U openai-whisper tqdm

# FFmpeg required for video_converter.py and audio_splitter.py
brew install ffmpeg  # macOS
```

## Core Scripts

### transcribe.py
Batch transcribe audio files (.m4a, .opus) using Whisper. Supports speaker diarization with Pyannote.

```bash
# Single file
python transcribe.py audio.m4a --model medium --lang en

# Explicit output path
python transcribe.py audio.m4a --model large --lang en --output /path/to/output.txt

# Directory with unified output
python transcribe.py ./folder --model medium --unify asc

# Unify existing transcripts only (no transcription)
python transcribe.py ./folder --unify desc

# Speaker diarization (requires HuggingFace token - see README)
python transcribe.py meeting.m4a --model medium --lang en --diarize

# Diarization with explicit output path
python transcribe.py meeting.m4a --model medium --lang en --diarize --output /path/to/meeting-diarized.txt

# Hebrew with localized speaker labels (דובר 1, דובר 2, ...)
python transcribe.py meeting.m4a --model medium --lang he --diarize

# With timestamps
python transcribe.py meeting.m4a --model medium --diarize --timestamps
```

Models: tiny, base, small, medium, large, large-v3 (default for single file: large)
Default language: Hebrew (he)

**Engines** (via `--engine` or `TRANSCRIPTION_ENGINE` env var):
- `mlx-whisper` — Apple Silicon, fastest (requires `pip install mlx-whisper`)
- `faster-whisper` — cross-platform, fast (requires `pip install faster-whisper`)
- `openai-whisper` — original CPU path, always available

When `--engine` is omitted, transcribe.py auto-detects: MLX on Apple Silicon
when installed, else faster-whisper, else openai-whisper. Pre-fetch model
weights with `./download_model.sh` to avoid first-run download latency.

**Diarization output format:**
- English: `Speaker 1: Hello...`
- Hebrew: `דובר 1: שלום...`
- Diarized files saved as `*_diarized.txt`

### video_converter.py
Extract audio from video files (.mp4, .mov, .avi, .mkv, etc.)

```bash
python video_converter.py video.mp4 --format m4a --bitrate 192k
python video_converter.py ./videos --output ./audio --force
```

### audio_splitter.py
Split audio files into segments of specified length.

```bash
python audio_splitter.py ./folder --segment-length 600 --format m4a
python audio_splitter.py ./folder --file recording.m4a --output ./split
```

### auto_transcribe_meet.sh
Entrypoint for macOS Folder Actions. Enqueues a transcription job and returns within ~100ms. A single `auto_transcribe_worker.sh` drains the queue serially so multiple files dropped at once don't load multiple MLX models in parallel and OOM the machine.

```bash
./auto_transcribe_meet.sh /path/to/video.mp4
./auto_transcribe_meet.sh /path/to/audio.m4a
```

**Queue semantics:**
- Job file written to `$QUEUE_DIR` with the computed output base name (frozen at enqueue time so crash re-runs land on the same paths).
- Worker self-spawns if none is alive; FIFO order; waits until each matching Google Drive job is at least `TRANSCRIPTION_START_DELAY_SECONDS` old before processing; exits after `WORKER_IDLE_GRACE_SECONDS` of empty queue.
- Same absolute input path, copy-suffix variant, or same-folder recording-name containment already pending → dedupe, second enqueue is a no-op.
- Manual run bypassing the queue: `./transcribe_one.sh <input> <base-name>`.

**Two-mode flow** (in `transcribe_one.sh`):
1. Convert video → M4A (if needed)
2. **Fast transcription** (Whisper only, `large` model) → `{name}-he.txt`, `{name}-en.txt`
3. **Diarized transcription** (Whisper `medium` + Pyannote) → `{name}-he-diarized.txt`, `{name}-en-diarized.txt`

**Configuration options** (edit script):
- `ENABLE_FAST`: true (default) / false — toggle fast transcription
- `FAST_MODEL`: large (default) — model for fast transcription
- `ENABLE_DIARIZATION`: true (default) / false — toggle speaker identification
- `DIARIZE_MODEL`: medium (default) — model for diarized transcription
- `OUTPUT_DIR`: Where transcripts are saved

**Folder Actions setup:**
- Meet Recordings folder: processes all files
- Downloads folder: filters for .mp4/.m4a only

See AUTOMATOR_SETUP_INSTRUCTIONS.md for setup details.

## Architecture Notes

- All scripts use argparse for CLI arguments
- Pluggable engine layer (mlx-whisper / faster-whisper / openai-whisper) behind
  a unified `transcribe_audio(engine, ...)` dispatcher in transcribe.py
- transcribe.py uses threaded animated progress during transcription
- Existing output files are skipped unless --force is specified
- Transcripts saved as .txt with same base name as input

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Tests cover engine dispatch, model-name mapping, output normalization, and
diarization alignment. They mock the underlying ML libraries — no real model
inference, runs in milliseconds.
