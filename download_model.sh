#!/bin/bash
# Pre-download Whisper and Pyannote model weights so the first real
# transcription run doesn't pay the download cost.
#
# Reads engine + model selection from .env. Runs against all configured
# engines/models including diarization (Pyannote).
#
# Usage:
#   ./download_model.sh                    # download configured defaults
#   ./download_model.sh --engine mlx-whisper --model large
#   ./download_model.sh --all              # download every engine x every size
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

if [[ -f ".env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
else
    echo "⚠️  No .env file found — using built-in defaults"
fi

ENGINE="${TRANSCRIPTION_ENGINE:-mlx-whisper}"
FAST_MODEL_NAME="${FAST_MODEL:-large}"
DIARIZE_MODEL_NAME="${DIARIZE_MODEL:-large}"
DOWNLOAD_ALL=false
SKIP_PYANNOTE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --engine) ENGINE="$2"; shift 2 ;;
        --model) FAST_MODEL_NAME="$2"; DIARIZE_MODEL_NAME="$2"; shift 2 ;;
        --all) DOWNLOAD_ALL=true; shift ;;
        --no-pyannote) SKIP_PYANNOTE=true; shift ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/whisper-env}"
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
else
    echo "⚠️  Virtual env not found at $VENV_DIR — using system Python"
fi

if ! python -c "import huggingface_hub" 2>/dev/null; then
    echo "Installing huggingface_hub..."
    pip install -q huggingface_hub
fi

export HF_TOKEN="${HF_TOKEN:-}"
export ENGINE FAST_MODEL_NAME DIARIZE_MODEL_NAME DOWNLOAD_ALL SKIP_PYANNOTE

python <<'PYEOF'
import os
import sys
from huggingface_hub import snapshot_download, hf_hub_download

ENGINE = os.environ["ENGINE"]
FAST = os.environ["FAST_MODEL_NAME"]
DIARIZE = os.environ["DIARIZE_MODEL_NAME"]
ALL = os.environ["DOWNLOAD_ALL"] == "true"
SKIP_PYANNOTE = os.environ["SKIP_PYANNOTE"] == "true"
HF_TOKEN = os.environ.get("HF_TOKEN") or None

MLX_REPOS = {
    "tiny":     "mlx-community/whisper-tiny-mlx",
    "base":     "mlx-community/whisper-base-mlx",
    "small":    "mlx-community/whisper-small-mlx",
    "medium":   "mlx-community/whisper-medium-mlx",
    "large":    "mlx-community/whisper-large-v3-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
}

FASTER_REPOS = {
    "tiny":     "Systran/faster-whisper-tiny",
    "base":     "Systran/faster-whisper-base",
    "small":    "Systran/faster-whisper-small",
    "medium":   "Systran/faster-whisper-medium",
    "large":    "Systran/faster-whisper-large-v3",
    "large-v3": "Systran/faster-whisper-large-v3",
}

OPENAI_NAMES = {
    "tiny":     "tiny",
    "base":     "base",
    "small":    "small",
    "medium":   "medium",
    "large":    "large-v3",
    "large-v3": "large-v3",
}


def fetch_mlx(size):
    repo = MLX_REPOS[size]
    print(f"  → MLX: {repo}")
    snapshot_download(repo_id=repo)


def fetch_faster(size):
    repo = FASTER_REPOS[size]
    print(f"  → faster-whisper: {repo}")
    snapshot_download(repo_id=repo)


def fetch_openai(size):
    name = OPENAI_NAMES[size]
    print(f"  → openai-whisper: {name} (loading triggers download)")
    try:
        # Use the public load_model API rather than whisper._download /
        # whisper._MODELS — those are private and have changed between
        # releases. load_model fetches weights as a side effect.
        import whisper
        whisper.load_model(name, device="cpu")
    except Exception as e:
        print(f"    ⚠️  could not pre-fetch openai-whisper {name}: {e}")
        print(f"       (will download on first transcribe.py run)")


FETCHERS = {
    "mlx-whisper":    fetch_mlx,
    "faster-whisper": fetch_faster,
    "openai-whisper": fetch_openai,
}

targets = []
if ALL:
    for eng in FETCHERS:
        for size in ["tiny", "base", "small", "medium", "large"]:
            targets.append((eng, size))
else:
    if ENGINE not in FETCHERS:
        print(f"❌ Unknown engine: {ENGINE}")
        print(f"   Valid: {', '.join(FETCHERS)}")
        sys.exit(1)
    seen = set()
    for size in (FAST, DIARIZE):
        key = (ENGINE, size)
        if key not in seen:
            targets.append(key)
            seen.add(key)

print(f"📦 Downloading {len(targets)} model(s)...")
for eng, size in targets:
    print(f"\n[{eng} / {size}]")
    FETCHERS[eng](size)

if not SKIP_PYANNOTE:
    print(f"\n[pyannote / speaker-diarization-3.1]")
    if not HF_TOKEN:
        print("  ⚠️  HF_TOKEN not set — skipping Pyannote download.")
        print("     Pyannote models are gated. Set HF_TOKEN in .env or shell,")
        print("     accept the model terms at:")
        print("     https://huggingface.co/pyannote/speaker-diarization-3.1")
    else:
        try:
            snapshot_download(repo_id="pyannote/speaker-diarization-3.1", token=HF_TOKEN)
            snapshot_download(repo_id="pyannote/segmentation-3.0", token=HF_TOKEN)
            print("  ✓ Pyannote models cached")
        except Exception as e:
            print(f"  ❌ Pyannote download failed: {e}")
            print("     Make sure you've accepted model terms on HuggingFace")
            sys.exit(1)

print("\n✅ Done.")
PYEOF
