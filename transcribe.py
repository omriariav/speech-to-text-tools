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
# Suppress known deprecation warnings from dependencies. pyannote.audio
# calls into torchaudio internals that PyTorch is migrating to TorchCodec;
# the warnings are emitted on every audio file pyannote processes (dozens
# per diarization run) and are not actionable from this repo's side.
import warnings
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
warnings.filterwarnings("ignore", message="Module 'speechbrain.pretrained' was deprecated")
# torchaudio → TorchCodec migration noise (PyTorch issue #3902)
warnings.filterwarnings("ignore", category=UserWarning, module=r"torchaudio\..*")
warnings.filterwarnings("ignore", category=UserWarning, module=r"pyannote\.audio\..*")
warnings.filterwarnings("ignore", message=r".*torchaudio.*deprecated.*")
warnings.filterwarnings("ignore", message=r".*TorchAudio.*maintenance phase.*")
warnings.filterwarnings("ignore", message=r".*load_with_torchcodec.*")
warnings.filterwarnings("ignore", message=r".*AudioMetaData has been deprecated.*")

import os
import argparse
import platform
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

# ---------------------------------------------------------------------------
# Pluggable transcription engines
# ---------------------------------------------------------------------------
# Three backends produce the same {"segments": [...], "text": ...} shape so the
# rest of this module (diarization alignment, output formatting) is engine-
# agnostic. Pick via --engine or TRANSCRIPTION_ENGINE env var.

ENGINES = ("mlx-whisper", "faster-whisper", "openai-whisper")

_MLX_REPOS = {
    "tiny":           "mlx-community/whisper-tiny-mlx",
    "base":           "mlx-community/whisper-base-mlx",
    "small":          "mlx-community/whisper-small-mlx",
    "medium":         "mlx-community/whisper-medium-mlx",
    "large":          "mlx-community/whisper-large-v3-mlx",
    "large-v3":       "mlx-community/whisper-large-v3-mlx",
    # Memory-friendly large-v3 variants. Use these on machines where
    # plain `large` triggers OOM (M-series with <32GB free, or systems
    # under load). Quality is near-identical for most content.
    "large-q4":       "mlx-community/whisper-large-v3-mlx-4bit",   # ~1GB on disk, ~2GB peak
    "large-turbo":    "mlx-community/whisper-large-v3-turbo",      # distilled, ~3GB, much faster
    "large-turbo-q4": "mlx-community/whisper-large-v3-turbo-q4",   # distilled + quantized, ~800MB
}

_FASTER_NAMES = {
    "tiny": "tiny", "base": "base", "small": "small", "medium": "medium",
    "large": "large-v3", "large-v3": "large-v3",
}

_OPENAI_NAMES = {
    "tiny": "tiny", "base": "base", "small": "small", "medium": "medium",
    "large": "large-v3", "large-v3": "large-v3",
}

# Module-level cache: fine for one-shot CLI use (the auto_transcribe_meet.sh
# pattern of one subprocess per pass). Callers importing this as a library
# and switching models frequently across many files should be aware there
# is no eviction policy here.
_MODEL_CACHE = {}

# Model name aliases that only exist in the MLX repo set. Passing these
# to faster-whisper or openai-whisper would surface as opaque "model not
# found" errors from the underlying library — guard them at the dispatch
# boundary with a clear message.
_MLX_ONLY_MODELS = {"large-q4", "large-turbo", "large-turbo-q4"}


def _require_mlx_only(engine: str, model_name: str) -> None:
    if model_name in _MLX_ONLY_MODELS and engine != "mlx-whisper":
        raise ValueError(
            f"Model {model_name!r} is MLX-only and not available for "
            f"engine {engine!r}. Use --engine mlx-whisper, or pick a "
            f"non-quantized model name (large, large-v3, medium, ...)."
        )


def resolve_hf_token(env_token: str, cached_path: str = "~/.cache/huggingface/token"):
    """Pick the right HuggingFace token source for gated model access.

    Resolution order:
      1. `env_token` if non-empty (typically `os.environ["HF_TOKEN"]`)
      2. Cached `huggingface-cli login` at `cached_path` — return None so
         `huggingface_hub` falls back to its own cached credential.
      3. Raise FileNotFoundError if neither is available.

    Returns:
        (token_or_None, source_label). `token=None` means "let
        huggingface_hub handle it via cached login."
    """
    if env_token:
        return env_token, "env"
    if os.path.exists(os.path.expanduser(cached_path)):
        return None, "cached-cli-login"
    raise FileNotFoundError(
        "No HF_TOKEN env var and no cached huggingface-cli login. "
        "Either set HF_TOKEN in .env or run `huggingface-cli login`."
    )


def detect_default_engine() -> str:
    """Pick the fastest available engine on this machine.

    Catches broad Exception (not just ImportError) because optional
    backends can fail at import time with native-library errors
    (CTranslate2 ABI mismatch, missing AVX2, etc.) — those should
    fall through to the next candidate, not abort detection.
    """
    is_mac_arm = platform.system() == "Darwin" and platform.machine() == "arm64"
    if is_mac_arm:
        try:
            import mlx_whisper  # noqa: F401
            return "mlx-whisper"
        except Exception:
            pass
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except Exception:
        pass
    return "openai-whisper"


def transcribe_audio(engine: str, audio_path: str, model_name: str,
                     language: str, word_timestamps: bool = False) -> dict:
    """Engine-agnostic transcription. Returns {'segments': [...], 'text': str}."""
    _require_mlx_only(engine, model_name)
    if engine == "openai-whisper":
        return _transcribe_openai(audio_path, model_name, language, word_timestamps)
    if engine == "mlx-whisper":
        return _transcribe_mlx(audio_path, model_name, language, word_timestamps)
    if engine == "faster-whisper":
        return _transcribe_faster(audio_path, model_name, language, word_timestamps)
    raise ValueError(f"Unknown engine: {engine!r}. Valid: {', '.join(ENGINES)}")


def detect_language(engine: str, audio_path: str, model_name: str,
                    sample_seconds: int = 30) -> str:
    """Detect spoken language from the first `sample_seconds` of audio.

    Returns an ISO-639-1 code (e.g. 'he', 'en'). Uses ffmpeg to slice a short
    16 kHz mono WAV so detection runs in seconds regardless of meeting length.
    """
    import subprocess
    import tempfile

    _require_mlx_only(engine, model_name)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        clip_path = tmp.name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", audio_path,
                "-t", str(sample_seconds),
                "-ac", "1", "-ar", "16000",
                clip_path,
            ],
            check=True,
        )

        if engine == "mlx-whisper":
            import mlx_whisper
            repo = _MLX_REPOS.get(model_name)
            if not repo:
                raise ValueError(f"Unknown MLX model: {model_name!r}")
            result = mlx_whisper.transcribe(clip_path, path_or_hf_repo=repo, language=None)
            return result.get("language", "") or ""
        if engine == "faster-whisper":
            from faster_whisper import WhisperModel
            name = _FASTER_NAMES.get(model_name, model_name)
            key = ("faster", name)
            if key not in _MODEL_CACHE:
                _MODEL_CACHE[key] = WhisperModel(name, device="cpu", compute_type="auto")
            model = _MODEL_CACHE[key]
            _segments, info = model.transcribe(clip_path, language=None)
            return getattr(info, "language", "") or ""
        if engine == "openai-whisper":
            import whisper
            name = _OPENAI_NAMES.get(model_name, model_name)
            key = ("openai", name)
            if key not in _MODEL_CACHE:
                _MODEL_CACHE[key] = whisper.load_model(name, device="cpu")
            model = _MODEL_CACHE[key]
            result = model.transcribe(clip_path, language=None, fp16=False)
            return result.get("language", "") or ""
        raise ValueError(f"Unknown engine: {engine!r}. Valid: {', '.join(ENGINES)}")
    finally:
        try:
            os.unlink(clip_path)
        except OSError:
            pass


def _transcribe_openai(audio_path, model_name, language, word_timestamps):
    import whisper
    name = _OPENAI_NAMES.get(model_name, model_name)
    key = ("openai", name)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = whisper.load_model(name, device="cpu")
    model = _MODEL_CACHE[key]
    result = model.transcribe(
        audio_path, language=language, fp16=False,
        word_timestamps=word_timestamps,
    )
    return {"segments": result.get("segments", []), "text": result.get("text", "")}


def _transcribe_mlx(audio_path, model_name, language, word_timestamps):
    try:
        import mlx_whisper
    except ImportError as e:
        raise RuntimeError(
            "mlx-whisper is not installed. On Apple Silicon: pip install mlx-whisper\n"
            "Or pick a different engine via --engine faster-whisper / openai-whisper."
        ) from e
    repo = _MLX_REPOS.get(model_name)
    if not repo:
        raise ValueError(f"Unknown MLX model: {model_name!r}. Valid: {', '.join(_MLX_REPOS)}")
    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=repo,
        language=language,
        word_timestamps=word_timestamps,
    )
    return {"segments": result.get("segments", []), "text": result.get("text", "")}


def _transcribe_faster(audio_path, model_name, language, word_timestamps):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper"
        ) from e
    name = _FASTER_NAMES.get(model_name, model_name)
    key = ("faster", name)
    if key not in _MODEL_CACHE:
        # compute_type="auto" lets CTranslate2 pick the best available
        # quantization for the host CPU (int8 needs AVX2 on x86_64; on
        # Apple Silicon int8 works via NEON). Hard-coding int8 would
        # break on older x86 chips.
        _MODEL_CACHE[key] = WhisperModel(name, device="cpu", compute_type="auto")
    model = _MODEL_CACHE[key]
    segments_iter, _info = model.transcribe(
        audio_path, language=language, word_timestamps=word_timestamps,
    )
    segments = []
    text_parts = []
    for seg in segments_iter:
        seg_dict = {"start": seg.start, "end": seg.end, "text": seg.text}
        if word_timestamps and getattr(seg, "words", None):
            seg_dict["words"] = [
                {"start": w.start, "end": w.end, "word": w.word} for w in seg.words
            ]
        segments.append(seg_dict)
        text_parts.append(seg.text)
    # faster-whisper segments often start with a leading space; strip the
    # joined result so result["text"] matches openai/mlx behavior.
    return {"segments": segments, "text": "".join(text_parts).strip()}


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

    # Fix for PyTorch 2.6+ compatibility with pyannote models: pyannote's
    # weights were saved with an older torch and need weights_only=False
    # to load. We monkeypatch torch.load only for the duration of the
    # Pipeline.from_pretrained call, then restore — leaving the patch in
    # place would force weights_only=False on every torch.load call in
    # the process for the rest of its lifetime, which is a security
    # regression (arbitrary pickle execution allowed for any caller).
    _original_torch_load = torch.load

    def _patched_torch_load(*args, **kwargs):
        kwargs['weights_only'] = False
        return _original_torch_load(*args, **kwargs)

    from pyannote.audio import Pipeline

    # Trailing `or None` normalizes the empty-string case: when .env contains
    # HF_TOKEN="" and the shell exports it, os.environ.get returns "" (truthy
    # to Python only as a string presence test, but falsy in `or`-chains).
    # Without this, pyannote receives use_auth_token="" and tries to auth
    # with an empty token instead of falling back to the cached CLI login.
    token = auth_token or os.environ.get("HF_TOKEN") or None

    torch.load = _patched_torch_load
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
    finally:
        torch.load = _original_torch_load


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
                           output_path: str = None, engine: str = None):
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
    
    engine = engine or detect_default_engine()
    print(f"Using engine: {engine} (model: {model_name})")

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
        # Transcribe via pluggable engine
        result = transcribe_audio(
            engine=engine,
            audio_path=file_path,
            model_name=model_name,
            language=language,
            word_timestamps=diarize,
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
                      diarize: bool = False, include_timestamps: bool = False, hf_token: str = None,
                      engine: str = None):
    # Check if we should only unify existing files
    if unify and not model_name:
        unify_transcripts(folder, unify)
        return

    engine = engine or detect_default_engine()
    print(f"Using engine: {engine} (model: {model_name})")

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
            # Transcribe via pluggable engine
            result = transcribe_audio(
                engine=engine,
                audio_path=file_path,
                model_name=model_name,
                language=language,
                word_timestamps=diarize,
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
    """Shows an animated progress indicator while Whisper is processing.

    Skipped entirely when stdout isn't a TTY (e.g. Automator-triggered
    runs piped through tee to a logfile). Otherwise the spinner ticks
    every 100ms and writes thousands of lines into the log per run.
    """
    if not sys.stdout.isatty():
        # Just block until done; no animation, no log noise.
        stop_event.wait()
        return

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
        choices=["tiny", "base", "small", "medium", "large", "large-v3",
                 "large-q4", "large-turbo", "large-turbo-q4"],
        help=("Whisper model to use (required for transcription, omit to only "
              "unify existing transcripts). The large-q4 / large-turbo / "
              "large-turbo-q4 variants are MLX-only and trade some quality "
              "for much lower memory.")
    )
    parser.add_argument(
        "--engine", "-e",
        # `or None` so an empty TRANSCRIPTION_ENGINE="" from the shell is
        # treated the same as "unset" (falls through to detect_default_engine).
        default=os.environ.get("TRANSCRIPTION_ENGINE") or None,
        choices=list(ENGINES),
        help=("Transcription engine. Default: auto-detect (mlx-whisper on Apple "
              "Silicon if installed, else faster-whisper, else openai-whisper). "
              "Override via TRANSCRIPTION_ENGINE env var.")
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
    parser.add_argument(
        "--detect-language",
        action="store_true",
        help=("Detect the spoken language from the first ~30s of audio, print "
              "the ISO code (e.g. 'he', 'en') to stdout, and exit. Useful as a "
              "pre-flight gate before running a full transcription.")
    )
    parser.add_argument(
        "--detect-seconds",
        type=int,
        default=30,
        help="Audio sample length (seconds) for --detect-language. Default: 30."
    )
    args = parser.parse_args()

    # argparse only validates --engine choices for command-line values, not
    # for `default=` (which we wired to TRANSCRIPTION_ENGINE env var).
    # Validate explicitly so a typo in .env fails fast with a clear message.
    if args.engine and args.engine not in ENGINES:
        parser.error(
            f"Invalid TRANSCRIPTION_ENGINE {args.engine!r}. "
            f"Valid: {', '.join(ENGINES)}"
        )

    # Short-circuit: language detection mode. Runs on first --detect-seconds
    # of audio, prints the ISO code, and exits. Used by transcribe_one.sh as
    # a gate before kicking off the (much more expensive) full pipeline.
    if args.detect_language:
        if not os.path.isfile(args.path):
            print(f"Error: '{args.path}' is not a file.", file=sys.stderr)
            sys.exit(1)
        engine = args.engine or detect_default_engine()
        model = args.model or "large-turbo-q4" if engine == "mlx-whisper" else (args.model or "large")
        lang = detect_language(engine, args.path, model, sample_seconds=args.detect_seconds)
        if not lang:
            print("Error: language detection returned no result.", file=sys.stderr)
            sys.exit(2)
        print(lang)
        sys.exit(0)

    # Determine if path is a file or directory
    is_file = os.path.isfile(args.path)
    is_dir = os.path.isdir(args.path)
    
    if not is_file and not is_dir:
        print(f"Error: '{args.path}' is neither a valid file nor a directory.")
        sys.exit(1)

    # Default model to medium if not specified
    if not args.model and not args.unify:
        if is_file:
            args.model = "large"  # Default for single file
        else:
            print("Please specify either --model to transcribe or --unify to combine existing transcripts.")
            sys.exit(1)

    # Process based on whether it's a file or directory
    if is_file:
        if args.unify:
            print("Warning: --unify option is only applicable to directories, ignoring.")

        # Default to medium model for single files if not specified
        args.model = args.model or "large"

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
            output_path=args.output,
            engine=args.engine,
        )
    else:  # Directory
        # Directory mode: if --model omitted, args.model is None and the
        # earlier guard already required either --model or --unify, so by
        # this point None means "unify-only" — keep it None for that case.

        # Process the directory
        transcribe_folder(
            folder=args.path,
            model_name=args.model,
            language=args.lang,
            print_to_screen=args.print,
            unify=args.unify,
            diarize=args.diarize,
            include_timestamps=args.timestamps,
            hf_token=getattr(args, 'hf_token', None),
            engine=args.engine,
        )
