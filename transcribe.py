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
# Suppress known deprecation warnings from dependencies
import warnings
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
warnings.filterwarnings("ignore", message="torchaudio._backend.list_audio_backends has been deprecated")
warnings.filterwarnings("ignore", message="Module 'speechbrain.pretrained' was deprecated")

import os
import argparse
import whisper
from tqdm import tqdm
import time
import sys
import threading
import datetime

# Speaker labels for different languages
SPEAKER_LABELS = {
    "he": "דובר",      # Hebrew
    "en": "Speaker",   # English
    "es": "Orador",    # Spanish
    "fr": "Orateur",   # French
    "de": "Sprecher",  # German
    "ar": "المتحدث",   # Arabic
    "ru": "Спикер",    # Russian
}


def load_diarization_pipeline(auth_token: str = None):
    """
    Load the Pyannote speaker diarization pipeline.

    Args:
        auth_token: HuggingFace token. If None, uses HF_TOKEN env var or cached login.

    Returns:
        Pyannote Pipeline object

    Raises:
        EnvironmentError: If authentication fails
    """
    import torch

    # Fix for PyTorch 2.6+ compatibility with pyannote models
    # The models were saved with older PyTorch and need weights_only=False
    # Monkeypatch torch.load to use weights_only=False for pyannote compatibility
    _original_torch_load = torch.load
    def _patched_torch_load(*args, **kwargs):
        kwargs['weights_only'] = False
        return _original_torch_load(*args, **kwargs)
    torch.load = _patched_torch_load

    from pyannote.audio import Pipeline

    token = auth_token or os.environ.get("HF_TOKEN")

    try:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=token
        )
        # Force CPU execution (matching existing whisper setup)
        pipeline.to(torch.device("cpu"))
        return pipeline
    except Exception as e:
        if "401" in str(e) or "authentication" in str(e).lower() or "gated" in str(e).lower():
            raise EnvironmentError(
                "HuggingFace authentication failed. Please either:\n"
                "1. Set HF_TOKEN environment variable, or\n"
                "2. Run 'huggingface-cli login'\n"
                "See README for detailed setup instructions."
            ) from e
        raise


def align_whisper_with_diarization(whisper_segments, diarization):
    """
    Align Whisper transcript segments with Pyannote diarization results.

    Uses temporal intersection to find the speaker with maximum overlap
    for each Whisper segment.

    Args:
        whisper_segments: List of Whisper segments with 'start', 'end', 'text' keys
        diarization: Pyannote Annotation object with speaker turns

    Returns:
        List of dicts with 'speaker', 'start', 'end', 'text' keys
    """
    aligned_segments = []

    for segment in whisper_segments:
        seg_start = segment['start']
        seg_end = segment['end']
        seg_text = segment['text'].strip()

        if not seg_text:
            continue

        # Find overlapping speaker turns
        speaker_overlaps = {}

        for turn, _, speaker in diarization.itertracks(yield_label=True):
            # Calculate intersection
            overlap_start = max(seg_start, turn.start)
            overlap_end = min(seg_end, turn.end)
            overlap_duration = max(0, overlap_end - overlap_start)

            if overlap_duration > 0:
                speaker_overlaps[speaker] = speaker_overlaps.get(speaker, 0) + overlap_duration

        # Assign speaker with maximum overlap, or UNKNOWN if no overlap
        if speaker_overlaps:
            assigned_speaker = max(speaker_overlaps, key=speaker_overlaps.get)
        else:
            assigned_speaker = "UNKNOWN"

        aligned_segments.append({
            'speaker': assigned_speaker,
            'start': seg_start,
            'end': seg_end,
            'text': seg_text
        })

    return aligned_segments


def merge_consecutive_speaker_segments(aligned_segments):
    """
    Merge consecutive segments from the same speaker for readability.

    Args:
        aligned_segments: List from align_whisper_with_diarization()

    Returns:
        List of merged segments with combined text
    """
    if not aligned_segments:
        return []

    merged = []
    current = aligned_segments[0].copy()

    for segment in aligned_segments[1:]:
        if segment['speaker'] == current['speaker']:
            # Same speaker - merge
            current['end'] = segment['end']
            current['text'] = current['text'] + " " + segment['text']
        else:
            # Different speaker - save current and start new
            merged.append(current)
            current = segment.copy()

    # Don't forget the last segment
    merged.append(current)

    return merged


def get_speaker_label(speaker_id: str, language: str) -> str:
    """
    Get localized speaker label based on language.

    Args:
        speaker_id: Pyannote speaker ID (e.g., "SPEAKER_00")
        language: Language code (e.g., "he", "en")

    Returns:
        Localized speaker label (e.g., "דובר 1" for Hebrew)
    """
    # Extract speaker number from Pyannote ID (SPEAKER_00 -> 0)
    try:
        speaker_num = int(speaker_id.split("_")[-1]) + 1  # 1-indexed for readability
    except (ValueError, IndexError):
        speaker_num = speaker_id  # Fallback to original ID

    label_word = SPEAKER_LABELS.get(language, "Speaker")
    return f"{label_word} {speaker_num}"


def format_diarized_transcript(merged_segments, language: str, include_timestamps: bool = False):
    """
    Format merged segments into labeled transcript text.

    Args:
        merged_segments: List from merge_consecutive_speaker_segments()
        language: Language code for localized speaker labels
        include_timestamps: If True, include [MM:SS-MM:SS] before each line

    Returns:
        Formatted transcript string
    """
    lines = []

    for segment in merged_segments:
        speaker_label = get_speaker_label(segment['speaker'], language)
        text = segment['text']

        if include_timestamps:
            start_mins = int(segment['start'] // 60)
            start_secs = int(segment['start'] % 60)
            end_mins = int(segment['end'] // 60)
            end_secs = int(segment['end'] % 60)
            timestamp = f"[{start_mins:02d}:{start_secs:02d}-{end_mins:02d}:{end_secs:02d}]"
            lines.append(f"{speaker_label}: {timestamp} {text}")
        else:
            lines.append(f"{speaker_label}: {text}")

    return "\n".join(lines)

def transcribe_single_file(file_path: str, model_name: str, language: str, print_to_screen: bool,
                           diarize: bool = False, diarization_pipeline=None, include_timestamps: bool = False,
                           output_path: str = None):
    """Transcribe a single audio file, optionally with speaker diarization."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    # Check file extension
    if not file_path.lower().endswith((".m4a", ".opus")):
        print(f"Warning: File {file_path} is not a supported audio format (.m4a or .opus). Attempting to transcribe anyway.")

    # Get output path
    filename = os.path.basename(file_path)
    base_name = os.path.splitext(filename)[0]
    output_dir = os.path.dirname(file_path)
    if output_path:
        txt_path = output_path
    elif diarize:
        txt_path = os.path.join(output_dir, f"{base_name}_diarized.txt")
    else:
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
            fp16=False,
            word_timestamps=diarize  # Enable detailed timestamps when diarizing
        )

        # Stop the progress animation
        stop_progress.set()
        progress_thread.join()

        # Calculate and display elapsed time
        elapsed = time.time() - start_time
        print(f"✓   Finished transcribing {filename} in {elapsed:.1f} seconds")

        # Process segments - with or without diarization
        segments = result.get("segments", [])

        if diarize and diarization_pipeline is not None:
            # Run diarization
            print(f"    Running speaker diarization on {filename}...")

            # Pyannote requires WAV format - convert if needed
            import subprocess
            import tempfile
            temp_wav = None
            audio_for_diarization = file_path

            if file_path.lower().endswith(('.m4a', '.opus', '.mp3', '.ogg')):
                print(f"    Converting to WAV for diarization...")
                temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                temp_wav.close()
                subprocess.run([
                    'ffmpeg', '-y', '-i', file_path,
                    '-ar', '16000', '-ac', '1',  # 16kHz mono for diarization
                    temp_wav.name
                ], capture_output=True)
                audio_for_diarization = temp_wav.name

            try:
                diarization_result = diarization_pipeline(audio_for_diarization)
            finally:
                # Clean up temp file
                if temp_wav and os.path.exists(temp_wav.name):
                    os.unlink(temp_wav.name)

            # Align Whisper segments with diarization
            aligned = align_whisper_with_diarization(segments, diarization_result)

            # Merge consecutive same-speaker segments
            merged = merge_consecutive_speaker_segments(aligned)

            # Format output with language-appropriate speaker labels
            transcript = format_diarized_transcript(merged, language, include_timestamps)

            print(f"    Speaker diarization complete.")
        else:
            # Original behavior - just join segment text
            lines = [seg["text"].strip() for seg in segments]
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

def transcribe_folder(folder: str, model_name: str, language: str, print_to_screen: bool, unify: str = None,
                      diarize: bool = False, include_timestamps: bool = False, hf_token: str = None):
    # Check if we should only unify existing files
    if unify and not model_name:
        unify_transcripts(folder, unify)
        return

    # Load Whisper model once
    print("Loading Whisper model...")
    model = whisper.load_model(model_name, device="cpu")
    print(f"Model {model_name} loaded successfully.")

    # Load diarization pipeline if requested
    diarization_pipeline = None
    if diarize:
        print("Loading speaker diarization pipeline...")
        print("Note: Speaker diarization on CPU may be slow for long recordings.")
        diarization_pipeline = load_diarization_pipeline(hf_token)
        print("Diarization pipeline loaded successfully.")

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
        if diarize:
            txt_path = os.path.join(folder, f"{base_name}_diarized.txt")
        else:
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
                fp16=False,
                word_timestamps=diarize  # Enable detailed timestamps when diarizing
            )

            # Stop the progress animation
            stop_progress.set()
            progress_thread.join()

            # Calculate and display elapsed time
            elapsed = time.time() - start_time
            tqdm.write(f"✓   Finished transcribing {filename} in {elapsed:.1f} seconds")

            # Process segments - with or without diarization
            segments = result.get("segments", [])

            if diarize and diarization_pipeline is not None:
                # Run diarization
                tqdm.write(f"    Running speaker diarization on {filename}...")

                # Pyannote requires WAV format - convert if needed
                import subprocess
                import tempfile
                temp_wav = None
                audio_for_diarization = file_path

                if file_path.lower().endswith(('.m4a', '.opus', '.mp3', '.ogg')):
                    tqdm.write(f"    Converting to WAV for diarization...")
                    temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                    temp_wav.close()
                    subprocess.run([
                        'ffmpeg', '-y', '-i', file_path,
                        '-ar', '16000', '-ac', '1',  # 16kHz mono for diarization
                        temp_wav.name
                    ], capture_output=True)
                    audio_for_diarization = temp_wav.name

                try:
                    diarization_result = diarization_pipeline(audio_for_diarization)
                finally:
                    # Clean up temp file
                    if temp_wav and os.path.exists(temp_wav.name):
                        os.unlink(temp_wav.name)

                # Align Whisper segments with diarization
                aligned = align_whisper_with_diarization(segments, diarization_result)

                # Merge consecutive same-speaker segments
                merged = merge_consecutive_speaker_segments(aligned)

                # Format output with language-appropriate speaker labels
                transcript = format_diarized_transcript(merged, language, include_timestamps)

                tqdm.write(f"    Speaker diarization complete.")
            else:
                # Original behavior - just join segment text
                lines = [seg["text"].strip() for seg in segments]
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
    parser.add_argument(
        "--diarize", "-d",
        action="store_true",
        help="Enable speaker diarization (requires HuggingFace token, see README)"
    )
    parser.add_argument(
        "--timestamps", "-t",
        action="store_true",
        help="Include timestamps in diarized output (only applies when --diarize is used)"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Explicit output file path (overrides automatic naming)"
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        help="HuggingFace token for accessing Pyannote models (or set HF_TOKEN env var)"
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

        # Load diarization pipeline if needed (for single file)
        diarization_pipeline = None
        if args.diarize:
            print("Loading speaker diarization pipeline...")
            print("Note: Speaker diarization on CPU may be slow for long recordings.")
            diarization_pipeline = load_diarization_pipeline(getattr(args, 'hf_token', None))
            print("Diarization pipeline loaded successfully.")

        # Transcribe the single file
        transcribe_single_file(
            file_path=args.path,
            model_name=args.model,
            language=args.lang,
            print_to_screen=args.print,
            diarize=args.diarize,
            diarization_pipeline=diarization_pipeline,
            include_timestamps=args.timestamps,
            output_path=args.output
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
            unify=args.unify,
            diarize=args.diarize,
            include_timestamps=args.timestamps,
            hf_token=getattr(args, 'hf_token', None)
        )