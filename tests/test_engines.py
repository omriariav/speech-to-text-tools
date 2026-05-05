"""Tests for the pluggable engine adapter in transcribe.py.

Covers engine dispatch, model-name mapping, output normalization, and
auto-detect logic. No real model inference — each backend's underlying
library is mocked so tests run in milliseconds without GPU/weights.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Allow importing transcribe.py as a module from the repo root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import transcribe  # noqa: E402


class TestDetectDefaultEngine(unittest.TestCase):
    def setUp(self):
        # detect_default_engine imports lazily; clear any prior cached imports
        for mod in ("mlx_whisper", "faster_whisper"):
            sys.modules.pop(mod, None)

    @patch("transcribe.platform")
    def test_apple_silicon_with_mlx_picks_mlx(self, mock_platform):
        mock_platform.system.return_value = "Darwin"
        mock_platform.machine.return_value = "arm64"
        sys.modules["mlx_whisper"] = MagicMock()
        try:
            self.assertEqual(transcribe.detect_default_engine(), "mlx-whisper")
        finally:
            sys.modules.pop("mlx_whisper", None)

    @patch("transcribe.platform")
    def test_apple_silicon_without_mlx_falls_back_to_faster(self, mock_platform):
        mock_platform.system.return_value = "Darwin"
        mock_platform.machine.return_value = "arm64"
        # Setting sys.modules[X] = None actively blocks `import X` (raises
        # ImportError); merely popping the key would let a real installed
        # package re-import successfully and break this test on dev
        # machines that actually have mlx-whisper.
        with patch.dict(sys.modules, {"mlx_whisper": None, "faster_whisper": MagicMock()}):
            self.assertEqual(transcribe.detect_default_engine(), "faster-whisper")

    @patch("transcribe.platform")
    def test_intel_mac_skips_mlx(self, mock_platform):
        mock_platform.system.return_value = "Darwin"
        mock_platform.machine.return_value = "x86_64"
        # mlx_whisper present in sys.modules should be ignored on non-arm
        sys.modules["mlx_whisper"] = MagicMock()
        sys.modules["faster_whisper"] = MagicMock()
        try:
            self.assertEqual(transcribe.detect_default_engine(), "faster-whisper")
        finally:
            sys.modules.pop("mlx_whisper", None)
            sys.modules.pop("faster_whisper", None)

    @patch("transcribe.platform")
    def test_no_optional_engines_falls_back_to_openai(self, mock_platform):
        mock_platform.system.return_value = "Linux"
        mock_platform.machine.return_value = "x86_64"
        sys.modules.pop("mlx_whisper", None)
        sys.modules.pop("faster_whisper", None)
        # Force ImportError for both optional engines
        with patch.dict(sys.modules, {"mlx_whisper": None, "faster_whisper": None}):
            self.assertEqual(transcribe.detect_default_engine(), "openai-whisper")

    @patch("transcribe.platform")
    def test_broken_faster_whisper_native_lib_falls_through(self, mock_platform):
        # If faster-whisper is installed but its native CTranslate2 lib
        # raises a non-ImportError at import time, detection should still
        # gracefully fall through to openai-whisper rather than propagate.
        mock_platform.system.return_value = "Linux"
        mock_platform.machine.return_value = "x86_64"
        broken_loader = MagicMock()
        broken_loader.__spec__ = None  # presence triggers import path
        # Simulate a runtime exception during import of faster_whisper
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "faster_whisper":
                raise RuntimeError("CTranslate2 native lib mismatch")
            return real_import(name, *args, **kwargs)

        sys.modules.pop("mlx_whisper", None)
        sys.modules.pop("faster_whisper", None)
        with patch.object(builtins, "__import__", side_effect=fake_import):
            self.assertEqual(transcribe.detect_default_engine(), "openai-whisper")


class TestTranscribeAudioDispatch(unittest.TestCase):
    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            transcribe.transcribe_audio("bogus-engine", "/tmp/x.m4a", "large", "he")

    def test_dispatches_to_openai(self):
        with patch.object(transcribe, "_transcribe_openai") as m:
            m.return_value = {"segments": [], "text": ""}
            transcribe.transcribe_audio("openai-whisper", "/tmp/a.m4a", "medium", "he", True)
            m.assert_called_once_with("/tmp/a.m4a", "medium", "he", True)

    def test_dispatches_to_mlx(self):
        with patch.object(transcribe, "_transcribe_mlx") as m:
            m.return_value = {"segments": [], "text": ""}
            transcribe.transcribe_audio("mlx-whisper", "/tmp/a.m4a", "large", "en")
            m.assert_called_once()

    def test_dispatches_to_faster(self):
        with patch.object(transcribe, "_transcribe_faster") as m:
            m.return_value = {"segments": [], "text": ""}
            transcribe.transcribe_audio("faster-whisper", "/tmp/a.m4a", "large", "en")
            m.assert_called_once()


class TestOpenAIAdapter(unittest.TestCase):
    def setUp(self):
        transcribe._MODEL_CACHE.clear()

    def test_returns_normalized_shape(self):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = {
            "segments": [{"start": 0, "end": 1, "text": "hi"}],
            "text": "hi",
        }
        fake_whisper = MagicMock()
        fake_whisper.load_model.return_value = fake_model
        with patch.dict(sys.modules, {"whisper": fake_whisper}):
            result = transcribe._transcribe_openai("/tmp/a.m4a", "medium", "he", False)
        self.assertEqual(result["segments"], [{"start": 0, "end": 1, "text": "hi"}])
        self.assertEqual(result["text"], "hi")
        fake_whisper.load_model.assert_called_once_with("medium", device="cpu")

    def test_large_maps_to_large_v3(self):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = {"segments": [], "text": ""}
        fake_whisper = MagicMock()
        fake_whisper.load_model.return_value = fake_model
        with patch.dict(sys.modules, {"whisper": fake_whisper}):
            transcribe._transcribe_openai("/tmp/a.m4a", "large", "he", False)
        fake_whisper.load_model.assert_called_once_with("large-v3", device="cpu")

    def test_model_is_cached(self):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = {"segments": [], "text": ""}
        fake_whisper = MagicMock()
        fake_whisper.load_model.return_value = fake_model
        with patch.dict(sys.modules, {"whisper": fake_whisper}):
            transcribe._transcribe_openai("/tmp/a.m4a", "medium", "he", False)
            transcribe._transcribe_openai("/tmp/b.m4a", "medium", "he", False)
        # load_model should be called once due to module-level cache
        self.assertEqual(fake_whisper.load_model.call_count, 1)


class TestMLXAdapter(unittest.TestCase):
    def test_size_maps_to_hf_repo(self):
        fake_mlx = MagicMock()
        fake_mlx.transcribe.return_value = {
            "segments": [{"start": 0, "end": 1, "text": "hi"}],
            "text": "hi",
        }
        with patch.dict(sys.modules, {"mlx_whisper": fake_mlx}):
            transcribe._transcribe_mlx("/tmp/a.m4a", "large", "he", False)
        fake_mlx.transcribe.assert_called_once()
        kwargs = fake_mlx.transcribe.call_args.kwargs
        self.assertEqual(kwargs["path_or_hf_repo"], "mlx-community/whisper-large-v3-mlx")
        self.assertEqual(kwargs["language"], "he")
        self.assertFalse(kwargs["word_timestamps"])

    def test_unknown_size_raises(self):
        fake_mlx = MagicMock()
        with patch.dict(sys.modules, {"mlx_whisper": fake_mlx}):
            with self.assertRaises(ValueError):
                transcribe._transcribe_mlx("/tmp/a.m4a", "huge", "he", False)

    def test_missing_package_gives_actionable_error(self):
        with patch.dict(sys.modules, {"mlx_whisper": None}):
            with self.assertRaises(RuntimeError) as ctx:
                transcribe._transcribe_mlx("/tmp/a.m4a", "large", "he", False)
        self.assertIn("mlx-whisper", str(ctx.exception))


class TestFasterWhisperAdapter(unittest.TestCase):
    def setUp(self):
        transcribe._MODEL_CACHE.clear()

    def _make_fake_module(self, segments):
        """Build a fake faster_whisper module whose WhisperModel yields given segments."""
        fake_model = MagicMock()
        fake_model.transcribe.return_value = (iter(segments), SimpleNamespace(language="he"))
        fake_module = MagicMock()
        fake_module.WhisperModel.return_value = fake_model
        return fake_module, fake_model

    def test_normalizes_segment_objects_to_dicts(self):
        segs = [
            SimpleNamespace(start=0.0, end=1.5, text="hello", words=None),
            SimpleNamespace(start=1.5, end=3.0, text=" world", words=None),
        ]
        fake_module, _ = self._make_fake_module(segs)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            result = transcribe._transcribe_faster("/tmp/a.m4a", "large", "en", False)
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][0], {"start": 0.0, "end": 1.5, "text": "hello"})
        self.assertEqual(result["text"], "hello world")

    def test_text_strips_leading_space_from_first_segment(self):
        # faster-whisper segments commonly start with a leading space; the
        # joined `text` field should be clean to match openai/mlx behavior.
        segs = [
            SimpleNamespace(start=0.0, end=1.0, text=" hello", words=None),
            SimpleNamespace(start=1.0, end=2.0, text=" world", words=None),
        ]
        fake_module, _ = self._make_fake_module(segs)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            result = transcribe._transcribe_faster("/tmp/a.m4a", "large", "en", False)
        self.assertEqual(result["text"], "hello world")
        self.assertFalse(result["text"].startswith(" "))

    def test_compute_type_is_auto_for_portability(self):
        # Hard-coded int8 would break on x86_64 CPUs without AVX2.
        # "auto" lets CTranslate2 pick the best available quantization.
        fake_module, _ = self._make_fake_module([])
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            transcribe._transcribe_faster("/tmp/a.m4a", "large", "he", False)
        kwargs = fake_module.WhisperModel.call_args.kwargs
        self.assertEqual(kwargs.get("compute_type"), "auto")

    def test_word_timestamps_pass_through(self):
        words = [SimpleNamespace(start=0.0, end=0.5, word="hi")]
        segs = [SimpleNamespace(start=0.0, end=0.5, text="hi", words=words)]
        fake_module, fake_model = self._make_fake_module(segs)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            result = transcribe._transcribe_faster("/tmp/a.m4a", "medium", "en", True)
        self.assertIn("words", result["segments"][0])
        self.assertEqual(result["segments"][0]["words"][0]["word"], "hi")
        # Verify word_timestamps flag propagated to the underlying call
        kwargs = fake_model.transcribe.call_args.kwargs
        self.assertTrue(kwargs["word_timestamps"])

    def test_large_maps_to_large_v3(self):
        fake_module, _ = self._make_fake_module([])
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            transcribe._transcribe_faster("/tmp/a.m4a", "large", "he", False)
        args, _ = fake_module.WhisperModel.call_args
        self.assertEqual(args[0], "large-v3")

    def test_missing_package_gives_actionable_error(self):
        with patch.dict(sys.modules, {"faster_whisper": None}):
            with self.assertRaises(RuntimeError) as ctx:
                transcribe._transcribe_faster("/tmp/a.m4a", "large", "he", False)
        self.assertIn("faster-whisper", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
