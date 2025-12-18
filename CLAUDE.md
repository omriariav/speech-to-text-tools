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
Batch transcribe audio files (.m4a, .opus) using Whisper.

```bash
# Single file
python transcribe.py audio.m4a --model medium --lang en

# Directory with unified output
python transcribe.py ./folder --model medium --unify asc

# Unify existing transcripts only (no transcription)
python transcribe.py ./folder --unify desc
```

Models: tiny, base, small, medium (default), large
Default language: Hebrew (he)

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
Automation script for Google Meet recordings. Converts MP4 to M4A and transcribes in both English and Hebrew. Designed for use with macOS Folder Actions.

```bash
./auto_transcribe_meet.sh /path/to/video.mp4
```

## Architecture Notes

- All scripts use argparse for CLI arguments
- Whisper runs on CPU with fp16=False for compatibility
- transcribe.py uses threaded animated progress during transcription
- Existing output files are skipped unless --force is specified
- Transcripts saved as .txt with same base name as input
