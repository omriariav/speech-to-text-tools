#!/usr/bin/env python3
"""
Batch-transcribe audio files in a folder using OpenAI Whisper.

Supports `.m4a` and `.opus` files (any format ffmpeg supports),
inserts line breaks between Whisper segments, and optionally
prints each transcript to the console.

Usage:
    # Transcribe a directory of audio files:
    python transcribe.py /path/to/audio_folder \
        --model medium \
        --lang he \
        --print
        
    # Transcribe a single audio file:
    python transcribe.py /path/to/audio_file.m4a \
        --model medium \
        --lang he
        
    # Only unify existing transcription files (no transcription):
    python transcribe.py /path/to/audio_folder \
        --unify asc
        
    # Unify existing transcription files in descending order:
    python transcribe.py /path/to/audio_folder \
        --unify desc
"""
import os
import argparse
import whisper
from tqdm import tqdm
import time
import sys
import threading
import datetime

def transcribe_single_file(file_path: str, model_name: str, language: str, print_to_screen: bool):
    """Transcribe a single audio file."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
        
    # Check file extension
    if not file_path.lower().endswith((".m4a", ".opus")):
        print(f"Warning: File {file_path} is not a supported audio format (.m4a or .opus). Attempting to transcribe anyway.")
        
    # Get output path
    filename = os.path.basename(file_path)
    base_name = os.path.splitext(filename)[0]
    output_dir = os.path.dirname(file_path)
    txt_path = os.path.join(output_dir, f"{base_name}.txt")
    
    # Check if transcript already exists
    if os.path.exists(txt_path):
        print(f"📄 Transcript already exists at {txt_path}")
        if print_to_screen:
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    transcript = f.read()
                print("\n" + "-" * 40)
                print(transcript)
                print("-" * 40 + "\n")
            except Exception as e:
                print(f"Error reading existing transcript: {str(e)}")
        return txt_path
    
    # Load Whisper model
    print("Loading Whisper model...")
    model = whisper.load_model(model_name, device="cpu")
    print(f"Model {model_name} loaded successfully.")
    
    print(f"⏳  Starting transcription of {filename}")
    
    # Create animated progress bar
    start_time = time.time()
    stop_progress = threading.Event()
    progress_thread = threading.Thread(
        target=show_animated_progress, 
        args=(filename, stop_progress)
    )
    progress_thread.daemon = True
    progress_thread.start()
    
    try:
        # Transcribe with Whisper
        result = model.transcribe(
            file_path,
            language=language,
            fp16=False
        )
        
        # Stop the progress animation
        stop_progress.set()
        progress_thread.join()
        
        # Calculate and display elapsed time
        elapsed = time.time() - start_time
        print(f"✓   Finished transcribing {filename} in {elapsed:.1f} seconds")
        
        # Join segments with line breaks
        lines = [seg["text"].strip() for seg in result.get("segments", [])]
        transcript = "\n".join(lines)

        # Write out .txt file
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"💾  Saved: {txt_path}")
        
        # Optionally echo to console
        if print_to_screen:
            print("\n" + "-" * 40)
            print(transcript)
            print("-" * 40 + "\n")
            
        return txt_path
    except Exception as e:
        # Stop the progress animation if there's an error
        stop_progress.set()
        if progress_thread.is_alive():
            progress_thread.join()
        # Re-raise the exception
        raise e

def unify_transcripts(folder: str, sort_order: str):
    """Unify existing transcript files without re-transcribing."""
    # Find all .txt files that might be transcripts
    txt_files = [f for f in os.listdir(folder) if f.lower().endswith(".txt") and not f.startswith("unified_transcript")]
    
    if not txt_files:
        print(f"No transcript files found in {folder}")
        return
        
    # Sort files by base name
    reverse_sort = (sort_order.lower() == "desc")
    txt_files = sorted(txt_files, key=lambda x: os.path.splitext(x)[0], reverse=reverse_sort)
    sort_direction = "descending" if reverse_sort else "ascending"
    print(f"Found {len(txt_files)} transcript files (sorted in {sort_direction} order).")
    
    # Read all transcript files
    unified_transcripts = []
    for filename in tqdm(txt_files, desc="Reading transcripts", unit="file"):
        file_path = os.path.join(folder, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                transcript = f.read()
                
            # Infer original audio filename
            base_name = os.path.splitext(filename)[0]
            possible_audio_files = [
                f"{base_name}.m4a",
                f"{base_name}.opus"
            ]
            
            # Check if original audio file exists (for display purposes)
            original_file = "Unknown"
            for audio_file in possible_audio_files:
                if os.path.exists(os.path.join(folder, audio_file)):
                    original_file = audio_file
                    break
                    
            unified_transcripts.append({
                "filename": original_file,
                "base_name": base_name,
                "transcript": transcript,
                "duration": 0  # No transcription time available for existing files
            })
            
        except Exception as e:
            print(f"Error reading {filename}: {str(e)}")
    
    if not unified_transcripts:
        print("No valid transcript files found.")
        return
    
    # Create unified transcript file
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    sort_suffix = "_desc" if sort_order.lower() == "desc" else "_asc"
    unified_file_path = os.path.join(folder, f"unified_transcript{sort_suffix}_{timestamp}.txt")
    
    with open(unified_file_path, "w", encoding="utf-8") as f:
        for i, item in enumerate(unified_transcripts, 1):
            f.write(f"\n\n{'=' * 60}\n")
            f.write(f"PART {i}/{len(unified_transcripts)}: {item['filename']}\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(item['transcript'])
            f.write("\n\n")
    
    print(f"\n✅ Unified transcript saved to: {unified_file_path}")
    print(f"   Files sorted in {sort_direction} order by filename.")

def transcribe_folder(folder: str, model_name: str, language: str, print_to_screen: bool, unify: str = None):
    # Check if we should only unify existing files
    if unify and not model_name:
        unify_transcripts(folder, unify)
        return
    
    # Load Whisper model once
    print("Loading Whisper model...")
    model = whisper.load_model(model_name, device="cpu")
    print(f"Model {model_name} loaded successfully.")

    # Ensure the folder exists
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    # Get list of audio files
    audio_files = [f for f in os.listdir(folder) if f.lower().endswith((".m4a", ".opus"))]
    
    if not audio_files:
        print(f"No supported audio files found in {folder}")
        return
        
    # Sort files by base name (without extension) if we're going to unify
    if unify:
        reverse_sort = (unify.lower() == "desc")
        audio_files = sorted(audio_files, key=lambda x: os.path.splitext(x)[0], reverse=reverse_sort)
        sort_direction = "descending" if reverse_sort else "ascending"
        print(f"Found {len(audio_files)} audio files to transcribe (sorted in {sort_direction} order).")
    else:
        print(f"Found {len(audio_files)} audio files to transcribe.")
    
    # Create unified transcription file if requested
    unified_transcripts = []
    
    # Process each supported audio file with progress bar
    for filename in tqdm(audio_files, desc="Processing files", unit="file"):
        file_path = os.path.join(folder, filename)
        base_name = os.path.splitext(filename)[0]
        txt_path = os.path.join(folder, f"{base_name}.txt")
        
        # Check if transcript already exists
        transcript_exists = os.path.exists(txt_path)
        
        if transcript_exists:
            tqdm.write(f"📄 Transcript for {filename} already exists at {txt_path}")
            
            # If we're unifying, read the existing transcript
            if unify:
                try:
                    with open(txt_path, "r", encoding="utf-8") as f:
                        transcript = f.read()
                    
                    unified_transcripts.append({
                        "filename": filename,
                        "base_name": base_name,
                        "transcript": transcript,
                        "duration": 0  # No transcription time available for existing files
                    })
                except Exception as e:
                    tqdm.write(f"⚠️  Error reading existing transcript: {str(e)}")
            
            continue
            
        tqdm.write(f"⏳  Starting transcription of {filename}")
        
        # Create animated progress bar
        start_time = time.time()
        stop_progress = threading.Event()
        progress_thread = threading.Thread(
            target=show_animated_progress, 
            args=(filename, stop_progress)
        )
        progress_thread.daemon = True
        progress_thread.start()
        
        try:
            # Transcribe with Whisper
            result = model.transcribe(
                file_path,
                language=language,
                fp16=False
            )
            
            # Stop the progress animation
            stop_progress.set()
            progress_thread.join()
            
            # Calculate and display elapsed time
            elapsed = time.time() - start_time
            tqdm.write(f"✓   Finished transcribing {filename} in {elapsed:.1f} seconds")
            
            # Join segments with line breaks
            lines = [seg["text"].strip() for seg in result.get("segments", [])]
            transcript = "\n".join(lines)

            # Write out .txt file
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(transcript)
            tqdm.write(f"💾  Saved: {txt_path}")
            
            # Add to unified transcripts if requested
            if unify:
                unified_transcripts.append({
                    "filename": filename,
                    "base_name": base_name,
                    "transcript": transcript,
                    "duration": elapsed
                })

            # Optionally echo to console
            if print_to_screen:
                tqdm.write("\n" + "-" * 40)
                tqdm.write(transcript)
                tqdm.write("-" * 40 + "\n")
        except Exception as e:
            # Stop the progress animation if there's an error
            stop_progress.set()
            if progress_thread.is_alive():
                progress_thread.join()
            # Re-raise the exception
            raise e
    
    # Create unified transcript file if requested
    if unify and unified_transcripts:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        sort_suffix = "_desc" if unify.lower() == "desc" else "_asc"
        unified_file_path = os.path.join(folder, f"unified_transcript{sort_suffix}_{timestamp}.txt")
        
        with open(unified_file_path, "w", encoding="utf-8") as f:
            for i, item in enumerate(unified_transcripts, 1):
                f.write(f"\n\n{'=' * 60}\n")
                duration_info = f" (Transcribed in {item['duration']:.1f} seconds)" if item['duration'] > 0 else ""
                f.write(f"PART {i}/{len(unified_transcripts)}: {item['filename']}{duration_info}\n")
                f.write(f"{'=' * 60}\n\n")
                f.write(item['transcript'])
                f.write("\n\n")
        
        sort_direction = "descending" if unify.lower() == "desc" else "ascending"
        print(f"\n✅ Unified transcript saved to: {unified_file_path}")
        print(f"   Files sorted in {sort_direction} order by filename.")

def show_animated_progress(filename, stop_event):
    """Shows an animated progress indicator while Whisper is processing."""
    spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    i = 0
    start_time = time.time()
    
    while not stop_event.is_set():
        elapsed = time.time() - start_time
        sys.stdout.write(f"\r{spinner[i]} Transcribing {filename}... ({elapsed:.1f}s elapsed)")
        sys.stdout.flush()
        time.sleep(0.1)
        i = (i + 1) % len(spinner)
    
    # Clear the line when done
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Transcribe audio files with Whisper (single file or directory)"
    )
    parser.add_argument(
        "path",
        help="Path to an audio file or a folder containing audio files"
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model to use (required for transcription, omit to only unify existing transcripts)"
    )
    parser.add_argument(
        "--lang", "-l",
        default="he",
        help="Language code for transcription (e.g. 'he', 'en')"
    )
    parser.add_argument(
        "--print", "-p",
        action="store_true",
        help="Also print each transcript to stdout"
    )
    parser.add_argument(
        "--unify", "-u",
        nargs="?",
        const="asc",
        choices=["asc", "desc"],
        help="Create a unified transcript file with all transcriptions, sorted by filename (asc=ascending, desc=descending)"
    )
    args = parser.parse_args()

    # Determine if path is a file or directory
    is_file = os.path.isfile(args.path)
    is_dir = os.path.isdir(args.path)
    
    if not is_file and not is_dir:
        print(f"Error: '{args.path}' is neither a valid file nor a directory.")
        sys.exit(1)

    # Default model to medium if not specified
    if not args.model and not args.unify:
        if is_file:
            args.model = "medium"  # Default for single file
        else:
            print("Please specify either --model to transcribe or --unify to combine existing transcripts.")
            sys.exit(1)

    # Process based on whether it's a file or directory
    if is_file:
        if args.unify:
            print("Warning: --unify option is only applicable to directories, ignoring.")
        
        # Default to medium model for single files if not specified
        args.model = args.model or "medium"
        
        # Transcribe the single file
        transcribe_single_file(
            file_path=args.path,
            model_name=args.model,
            language=args.lang,
            print_to_screen=args.print
        )
    else:  # Directory
        # If only unifying, pass model=None, otherwise ensure it has a default
        if args.model:
            args.model = args.model or "medium"
            
        # Process the directory
        transcribe_folder(
            folder=args.path,
            model_name=args.model,
            language=args.lang,
            print_to_screen=args.print,
            unify=args.unify
        )