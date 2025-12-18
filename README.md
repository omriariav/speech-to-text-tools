# Audio Transcription Tools

A collection of Python scripts for transcribing and processing audio files using OpenAI's Whisper speech recognition model.

## Available Tools

### 1. video_converter.py

Convert video files (`.mp4`, `.mov`, `.avi`, `.mkv`, etc.) to audio-only formats. Extract audio tracks from videos for transcription or archival purposes.

### 2. transcribe.py

Transcribe audio files (`.m4a` and `.opus` formats) using OpenAI's Whisper. Can process either individual files or entire directories.

### 3. audio_splitter.py

Split large audio files into smaller segments of specified length. Useful for breaking up long recordings.

## Requirements

- Python 3.7+
- OpenAI Whisper
- FFmpeg (for video_converter.py and audio_splitter.py)
- tqdm (for progress bars)

## Setup

1. Create a virtual environment:
   ```
   python -m venv whisper-env
   ```

2. Activate the environment:
   ```
   # On macOS/Linux
   source whisper-env/bin/activate
   
   # On Windows
   whisper-env\Scripts\activate
   ```

3. Install dependencies:
   ```
   pip install -U openai-whisper tqdm
   ```

4. (For video_converter.py and audio_splitter.py) Install FFmpeg:
   ```
   # macOS
   brew install ffmpeg

   # Ubuntu/Debian
   apt-get install ffmpeg
   ```

## Usage

### Video Conversion (video_converter.py)

#### Convert a single video to audio:
```
python video_converter.py path/to/video.mp4
```

#### Convert all videos in a directory:
```
python video_converter.py path/to/videos_folder
```

#### Convert to mp3 format with custom bitrate:
```
python video_converter.py path/to/video.mp4 --format mp3 --bitrate 320k
```

#### Convert to custom output directory:
```
python video_converter.py path/to/videos_folder --output path/to/audio_output
```

#### Force re-conversion of existing files:
```
python video_converter.py path/to/video.mp4 --force
```

#### Options for video_converter.py:

| Option | Description |
|--------|-------------|
| `path` | Path to a video file or folder containing video files |
| `--file`, `-f` | Specific video file to convert (if path is a folder) |
| `--output`, `-o` | Output directory (default: same as input) |
| `--format` | Output audio format: m4a (default), mp3, opus, wav |
| `--bitrate`, `-b` | Audio bitrate (default: 192k). Examples: 128k, 256k, 320k |
| `--force` | Force re-conversion even if output file exists |

### Transcription (transcribe.py)

#### Transcribe a single audio file:
```
python transcribe.py path/to/audio_file.m4a --model medium --lang en
```

#### Transcribe all audio files in a directory:
```
python transcribe.py path/to/audio_folder --model medium --lang he
```

#### Also print transcripts to the console:
```
python transcribe.py path/to/audio_file.m4a --print
```

#### Create a unified transcript file from all files in a directory:
```
python transcribe.py path/to/audio_folder --model medium --unify asc
```

#### Only unify existing transcription files (no transcription):
```
python transcribe.py path/to/audio_folder --unify
```

#### Options for transcribe.py:

| Option | Description |
|--------|-------------|
| `path` | Path to an audio file or folder containing audio files |
| `--model`, `-m` | Whisper model to use: tiny, base, small, medium (default), large |
| `--lang`, `-l` | Language code (default: he) |
| `--print`, `-p` | Print transcripts to console |
| `--unify`, `-u` | Create a unified transcript file (asc or desc order) |

### Audio Splitting (audio_splitter.py)

#### Split all audio files in a folder:
```
python audio_splitter.py path/to/audio_folder --segment-length 300
```

#### Split a specific audio file:
```
python audio_splitter.py path/to/audio_folder --file recording.m4a
```

#### Options for audio_splitter.py:

| Option | Description |
|--------|-------------|
| `input_folder` | Path to the folder containing audio files |
| `--file`, `-f` | Specific audio file to split |
| `--output`, `-o` | Output folder (default: ./split_audio) |
| `--segment-length`, `-s` | Length of each segment in seconds (default: 600) |
| `--format` | Output format (m4a, opus, mp3, wav) |

## Output

- Converted audio files are saved with the same name as the video files (with new extension)
- Individual transcriptions are saved as text files with the same name as the audio files
- Unified transcripts are saved as `unified_transcript_[order]_[timestamp].txt`
- Split audio files are saved in the specified output directory with sequential numbering

## Tips

1. **Complete workflow**: Convert videos to audio with `video_converter.py`, then transcribe with `transcribe.py` for a complete video-to-text pipeline.
2. For best transcription accuracy, use the larger models (`medium` or `large`), but note they require more processing time and memory.
3. For faster processing with less accuracy, use the `tiny` or `base` models.
4. The scripts automatically check for existing outputs to avoid duplicate work.
5. When processing long audio files, consider splitting them first with `audio_splitter.py`.
6. For high-quality audio extraction from videos, use `--bitrate 320k` with `video_converter.py`.

## License

This project is available under the MIT license. 