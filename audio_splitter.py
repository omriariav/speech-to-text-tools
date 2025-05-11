#!/usr/bin/env python3
"""
Split large audio files (.m4a, .opus) into smaller segments.

This script takes audio files and splits them into smaller chunks
of a specified duration. Useful for processing very long recordings
with tools that work better on shorter segments.

Usage:
    # Split all files in a folder:
    python audio_splitter.py /path/to/audio_folder \
        --output /path/to/output_folder \
        --segment-length 600 \
        --format m4a
        
    # Split a specific file:
    python audio_splitter.py /path/to/audio_folder \
        --file recording.m4a \
        --output /path/to/output_folder
"""
import os
import argparse
import subprocess
from tqdm import tqdm
import time
import math
import pdb

def get_duration(file_path):
    """Get duration of an audio file in seconds using ffprobe."""
    cmd = [
        "ffprobe", 
        "-v", "error", 
        "-show_entries", "format=duration", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def split_audio_file(file_path, output_dir, segment_length, output_format):
    """Split an audio file into segments of specified length."""
    # Create output folder if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Get the file basename without extension
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    
    # Get the file duration
    duration = get_duration(file_path)
    
    # Calculate number of segments
    num_segments = math.ceil(duration / segment_length)
    
    print(f"Splitting {base_name} ({duration:.1f}s) into {num_segments} segments...")
    
    # Process each segment with a progress bar
    for i in tqdm(range(num_segments), desc=f"Splitting {base_name}", unit="segment"):
        start_time = i * segment_length
        
        # Create output filename
        output_filename = f"{base_name}_part{i+1:03d}.{output_format}"
        output_path = os.path.join(output_dir, output_filename)
        
        # ffmpeg command to extract the segment
        cmd = [
            "ffmpeg",
            "-i", file_path,
            "-ss", str(start_time),
            "-t", str(segment_length),
            "-c", "copy",  # Copy codec without re-encoding for speed
            "-y",  # Overwrite output files without asking
            output_path
        ]
        
        # Run ffmpeg
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print(f"✅ {base_name} split into {num_segments} parts in {output_dir}")
    return num_segments

def main():
    parser = argparse.ArgumentParser(description="Split audio files into smaller segments")
    parser.add_argument(
        "input_folder",
        help="Path to the folder containing audio files"
    )
    parser.add_argument(
        "--file", "-f",
        help="Specific audio file to split (if omitted, all .m4a and .opus files in the folder will be processed)"
    )
    parser.add_argument(
        "--output", "-o",
        default="split_audio",
        help="Path to the output folder (default: ./split_audio)"
    )
    parser.add_argument(
        "--segment-length", "-s",
        type=int,
        default=600,
        help="Length of each segment in seconds (default: 600)"
    )
    parser.add_argument(
        "--format",
        choices=["m4a", "opus", "mp3", "wav"],
        default="m4a",
        help="Output format (default: m4a)"
    )
    args = parser.parse_args()
    
    # Ensure the input folder exists
    if not os.path.isdir(args.input_folder):
        print(f"Error: Input folder '{args.input_folder}' not found.")
        return
    
    # Get audio files from the input folder
    audio_files = []
    
    # If a specific file is provided, only process that file
    if args.file:
        # Check if the file parameter is already a complete path
        if os.path.isfile(args.file):
            file_path = args.file
        else:
            # Otherwise, join it with the input folder
            file_path = os.path.join(args.input_folder, args.file)
        
        #import pdb; pdb.set_trace()  # Breakpoint for debugging
        
        if os.path.isfile(file_path):
            if file_path.lower().endswith((".m4a", ".opus")):
                audio_files.append(file_path)
            else:
                print(f"Error: File '{file_path}' is not a supported audio format (.m4a or .opus).")
                return
        else:
            print(f"Error: File '{file_path}' not found.")
            return
    else:
        # Process all files in the directory
        for filename in os.listdir(args.input_folder):
            if filename.lower().endswith((".m4a", ".opus")):
                audio_files.append(os.path.join(args.input_folder, filename))
    
    if not audio_files:
        print(f"No .m4a or .opus files found in {args.input_folder}")
        return
    
    print(f"Found {len(audio_files)} audio files to split.")
    
    # Create output folder if it doesn't exist
    os.makedirs(args.output, exist_ok=True)
    
    # Process each audio file
    total_segments = 0
    start_time = time.time()
    
    for file_path in audio_files:
        segments = split_audio_file(
            file_path, 
            args.output, 
            args.segment_length, 
            args.format
        )
        total_segments += segments
    
    # Print summary
    elapsed = time.time() - start_time
    print(f"\nSummary:")
    print(f"- Processed {len(audio_files)} files")
    print(f"- Created {total_segments} segments")
    print(f"- Total time: {elapsed:.1f} seconds")
    print(f"- Output folder: {os.path.abspath(args.output)}")

if __name__ == "__main__":
    main() 