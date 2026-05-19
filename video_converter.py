#!/usr/bin/env python3
"""
Convert video files to audio-only formats.

This script extracts audio tracks from video files (mp4, mov, avi, mkv, etc.)
and converts them to audio-only formats (m4a, mp3, opus, wav). Useful for
extracting audio from video recordings for transcription or archival purposes.

Usage:
    # Convert a single video to m4a:
    python video_converter.py /path/to/video.mp4

    # Convert all videos in a folder:
    python video_converter.py /path/to/videos_folder \
        --format m4a \
        --bitrate 192k

    # Convert to mp3 with custom output directory:
    python video_converter.py /path/to/video.mp4 \
        --format mp3 \
        --output /path/to/audio_folder \
        --bitrate 320k

    # Force re-conversion of existing files:
    python video_converter.py /path/to/videos_folder --force
"""
import os
import sys
import argparse
import subprocess
from tqdm import tqdm
import time

# Supported video file extensions
VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg')


def looks_like_media(path):
    """ffprobe content-detection fallback.

    Some sources (Google Drive Meet "– Recording" files in particular)
    arrive with no extension, so extension matching silently rejects
    them. Ask ffprobe what's actually in the container instead. We
    accept any file whose header decodes to at least one audio or
    video stream — that's all we need to be able to extract audio
    via ffmpeg downstream.

    Surfaces ffprobe's stderr / timeout to our stderr on failure so
    callers (transcribe_one.sh redirects this script's stderr to the
    log file) can diagnose why a file was rejected instead of seeing
    only "not a recognizable media file".
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type",
                "-of", "default=nw=1:nk=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        print("ffprobe binary not found on PATH", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"ffprobe timed out after 15s on {path!r} (cloud file not yet materialized?)", file=sys.stderr)
        return False
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        print(f"ffprobe rejected {path!r}: rc={result.returncode} stderr={stderr!r}", file=sys.stderr)
        return False
    streams = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return bool(streams & {"audio", "video"})

def get_audio_codec(output_format):
    """Get the appropriate audio codec for the output format."""
    codec_map = {
        'm4a': 'aac',
        'mp3': 'libmp3lame',
        'opus': 'libopus',
        'wav': 'pcm_s16le'
    }
    return codec_map.get(output_format, 'aac')

def convert_video_to_audio(video_path, output_dir, output_format, bitrate, force=False):
    """
    Convert a video file to audio-only format.

    Args:
        video_path: Path to the input video file
        output_dir: Directory where the audio file will be saved
        output_format: Output audio format (m4a, mp3, opus, wav)
        bitrate: Audio bitrate (e.g., '192k', '320k')
        force: Force re-conversion even if output file exists

    Returns:
        True if conversion was successful, False otherwise
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Get the base filename without extension
    base_name = os.path.splitext(os.path.basename(video_path))[0]

    # Create output filename
    output_filename = f"{base_name}.{output_format}"
    output_path = os.path.join(output_dir, output_filename)

    # Check if output already exists
    if os.path.exists(output_path) and not force:
        print(f"⏭️  Skipping {base_name} (audio already exists at {output_path})")
        return True

    # Get audio codec
    audio_codec = get_audio_codec(output_format)

    # Build ffmpeg command
    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-vn",  # No video
        "-acodec", audio_codec,
        "-y",  # Overwrite output files without asking
    ]

    # Add bitrate for formats that support it (not WAV)
    if output_format != 'wav':
        cmd.extend(["-b:a", bitrate])

    cmd.append(output_path)

    # Run ffmpeg
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode == 0:
            # Get file size for reporting
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"✅ {base_name} → {output_filename} ({size_mb:.1f} MB)")
            return True
        else:
            print(f"❌ Error converting {base_name}: {result.stderr}")
            return False

    except FileNotFoundError:
        print("❌ Error: ffmpeg not found. Please install ffmpeg first.")
        print("   macOS: brew install ffmpeg")
        print("   Ubuntu/Debian: apt-get install ffmpeg")
        return False
    except Exception as e:
        print(f"❌ Error converting {base_name}: {str(e)}")
        return False

def get_video_files(path, specific_file=None):
    """
    Get list of video files to process.

    Args:
        path: Path to file or directory
        specific_file: Optional specific filename to process

    Returns:
        List of video file paths
    """
    video_files = []

    # If path is a file
    if os.path.isfile(path):
        if path.lower().endswith(VIDEO_EXTENSIONS) or looks_like_media(path):
            video_files.append(path)
        else:
            print(f"Error: {path} is not a recognizable media file")
            print(f"Known extensions: {', '.join(VIDEO_EXTENSIONS)}; ffprobe also rejected the contents.")
        return video_files

    # If path is a directory
    if os.path.isdir(path):
        if specific_file:
            # Process specific file in directory
            file_path = os.path.join(path, specific_file)
            if os.path.isfile(file_path):
                if file_path.lower().endswith(VIDEO_EXTENSIONS) or looks_like_media(file_path):
                    video_files.append(file_path)
                else:
                    print(f"Error: {specific_file} is not a recognizable media file")
            else:
                print(f"Error: File '{file_path}' not found")
        else:
            # Process all video files in directory. Directory mode stays
            # extension-based to avoid ffprobe-ing every random file in
            # the folder; the extensionless-recording case only matters
            # for the Folder Action path, which always passes a single file.
            for filename in sorted(os.listdir(path)):
                if filename.lower().endswith(VIDEO_EXTENSIONS):
                    video_files.append(os.path.join(path, filename))

        return video_files

    print(f"Error: Path '{path}' not found")
    return []

def main():
    parser = argparse.ArgumentParser(
        description="Convert video files to audio-only formats",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s video.mp4                          # Convert single video to m4a
  %(prog)s videos_folder/                     # Convert all videos in folder
  %(prog)s video.mp4 --format mp3             # Convert to mp3 format
  %(prog)s videos/ --output audio/            # Save to custom directory
  %(prog)s video.mp4 --bitrate 320k           # Higher quality audio
  %(prog)s videos/ --force                    # Re-convert existing files
        """
    )

    parser.add_argument(
        "path",
        help="Path to a video file or folder containing video files"
    )
    parser.add_argument(
        "--file", "-f",
        help="Specific video file to convert (if path is a folder)"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output directory (default: same as input)"
    )
    parser.add_argument(
        "--format",
        choices=["m4a", "mp3", "opus", "wav"],
        default="m4a",
        help="Output audio format (default: m4a)"
    )
    parser.add_argument(
        "--bitrate", "-b",
        default="192k",
        help="Audio bitrate (default: 192k). Examples: 128k, 192k, 320k"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-conversion even if output file exists"
    )

    args = parser.parse_args()

    # Get list of video files to process
    video_files = get_video_files(args.path, args.file)

    if not video_files:
        print(f"No video files found to process")
        # Exit non-zero so callers (transcribe_one.sh) can distinguish
        # "ran but nothing matched" from "successfully converted N files".
        sys.exit(1)

    # Determine output directory
    if args.output:
        output_dir = args.output
    else:
        # Use same directory as input
        if os.path.isfile(args.path):
            output_dir = os.path.dirname(args.path) or "."
        else:
            output_dir = args.path

    print(f"Found {len(video_files)} video file(s) to convert")
    print(f"Output format: {args.format}")
    print(f"Output directory: {os.path.abspath(output_dir)}")
    print(f"Bitrate: {args.bitrate}\n")

    # Process each video file
    successful = 0
    failed = 0
    skipped = 0
    start_time = time.time()

    for video_path in tqdm(video_files, desc="Converting videos", unit="file"):
        result = convert_video_to_audio(
            video_path,
            output_dir,
            args.format,
            args.bitrate,
            args.force
        )

        if result:
            # Check if it was actually converted or skipped
            base_name = os.path.splitext(os.path.basename(video_path))[0]
            output_path = os.path.join(output_dir, f"{base_name}.{args.format}")

            # Simple heuristic: if file existed before and we didn't force, it was skipped
            if not args.force and "Skipping" in str(result):
                skipped += 1
            else:
                successful += 1
        else:
            failed += 1

    # Print summary
    elapsed = time.time() - start_time
    print(f"\n{'='*50}")
    print(f"Summary:")
    print(f"  Total files: {len(video_files)}")
    print(f"  ✅ Converted: {successful}")
    if skipped > 0:
        print(f"  ⏭️  Skipped: {len(video_files) - successful - failed}")
    if failed > 0:
        print(f"  ❌ Failed: {failed}")
    print(f"  ⏱️  Total time: {elapsed:.1f} seconds")
    print(f"  📁 Output directory: {os.path.abspath(output_dir)}")
    print(f"{'='*50}")

    # Surface conversion failures to callers (transcribe_one.sh treats
    # a non-zero exit as "give up on this job"). Without this, a failed
    # ffmpeg run was indistinguishable from success at the shell level.
    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
