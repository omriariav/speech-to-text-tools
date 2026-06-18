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


class TestMLXOnlyModelGuard(unittest.TestCase):
    """MLX-only model variants must reject non-MLX engines with a clear error
    rather than letting an opaque 'model not found' bubble up from the
    underlying library."""

    def test_large_q4_rejected_for_faster_whisper(self):
        with self.assertRaises(ValueError) as ctx:
            transcribe.transcribe_audio("faster-whisper", "/tmp/a.m4a", "large-q4", "he")
        self.assertIn("MLX-only", str(ctx.exception))
        self.assertIn("mlx-whisper", str(ctx.exception))

    def test_large_turbo_rejected_for_openai_whisper(self):
        with self.assertRaises(ValueError) as ctx:
            transcribe.transcribe_audio("openai-whisper", "/tmp/a.m4a", "large-turbo", "en")
        self.assertIn("MLX-only", str(ctx.exception))

    def test_large_turbo_q4_rejected_for_openai(self):
        with self.assertRaises(ValueError):
            transcribe.transcribe_audio("openai-whisper", "/tmp/a.m4a", "large-turbo-q4", "en")

    def test_mlx_only_models_pass_through_to_mlx(self):
        # Sanity check: the same model names succeed when the engine is mlx
        with patch.object(transcribe, "_transcribe_mlx") as m:
            m.return_value = {"segments": [], "text": ""}
            transcribe.transcribe_audio("mlx-whisper", "/tmp/a.m4a", "large-q4", "he")
            transcribe.transcribe_audio("mlx-whisper", "/tmp/a.m4a", "large-turbo", "he")
            transcribe.transcribe_audio("mlx-whisper", "/tmp/a.m4a", "large-turbo-q4", "he")
        self.assertEqual(m.call_count, 3)

    def test_regular_models_unaffected(self):
        # Non-quantized model names should pass through to all engines
        with patch.object(transcribe, "_transcribe_faster") as m:
            m.return_value = {"segments": [], "text": ""}
            transcribe.transcribe_audio("faster-whisper", "/tmp/a.m4a", "large", "en")
            transcribe.transcribe_audio("faster-whisper", "/tmp/a.m4a", "medium", "en")
        self.assertEqual(m.call_count, 2)


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

    def test_quantized_variants_map_to_correct_repos(self):
        # Memory-friendly variants are MLX-only and route to specific HF repos.
        # Verify each maps to its expected repo so users can rely on them
        # when the full large model OOMs on memory-constrained machines.
        cases = {
            "large-q4":       "mlx-community/whisper-large-v3-mlx-4bit",
            "large-turbo":    "mlx-community/whisper-large-v3-turbo",
            "large-turbo-q4": "mlx-community/whisper-large-v3-turbo-q4",
        }
        for size, expected_repo in cases.items():
            with self.subTest(size=size):
                fake_mlx = MagicMock()
                fake_mlx.transcribe.return_value = {"segments": [], "text": ""}
                with patch.dict(sys.modules, {"mlx_whisper": fake_mlx}):
                    transcribe._transcribe_mlx("/tmp/a.m4a", size, "he", False)
                kwargs = fake_mlx.transcribe.call_args.kwargs
                self.assertEqual(kwargs["path_or_hf_repo"], expected_repo)

    def test_missing_package_gives_actionable_error(self):
        with patch.dict(sys.modules, {"mlx_whisper": None}):
            with self.assertRaises(RuntimeError) as ctx:
                transcribe._transcribe_mlx("/tmp/a.m4a", "large", "he", False)
        self.assertIn("mlx-whisper", str(ctx.exception))


class TestHasSpeech(unittest.TestCase):
    def test_low_no_speech_prob_is_speech(self):
        self.assertTrue(transcribe._has_speech([{"no_speech_prob": 0.1}]))

    def test_all_high_no_speech_prob_is_silence(self):
        # Whisper labels a near-silent clip with high no_speech_prob; the gate
        # must read that as "no speech" rather than trusting the language ID.
        self.assertFalse(transcribe._has_speech(
            [{"no_speech_prob": 0.9}, {"no_speech_prob": 0.95}]))

    def test_no_segments_is_silence(self):
        self.assertFalse(transcribe._has_speech([]))

    def test_mixed_segments_count_as_speech(self):
        self.assertTrue(transcribe._has_speech(
            [{"no_speech_prob": 0.99}, {"no_speech_prob": 0.2}]))

    def test_missing_prob_treated_as_speech(self):
        # Defensive: a segment without the key shouldn't be silently dropped.
        self.assertTrue(transcribe._has_speech([{"start": 0, "end": 1}]))

    def test_object_segments_via_attribute(self):
        # faster-whisper yields objects, not dicts.
        self.assertFalse(transcribe._has_speech(
            [SimpleNamespace(no_speech_prob=0.8)]))
        self.assertTrue(transcribe._has_speech(
            [SimpleNamespace(no_speech_prob=0.3)]))


class TestDetectLanguageNoSpeech(unittest.TestCase):
    def _run(self, segments, language):
        fake_mlx = MagicMock()
        fake_mlx.transcribe.return_value = {"segments": segments, "language": language}
        with patch.dict(sys.modules, {"mlx_whisper": fake_mlx}), \
                patch("subprocess.run") as mock_run, \
                patch("os.unlink"):
            mock_run.return_value.returncode = 0
            return transcribe.detect_language(
                "mlx-whisper", "/tmp/a.m4a", "large-turbo-q4", sample_seconds=30)

    def test_silence_returns_empty_even_with_language(self):
        # The bug we're fixing: a silent intro still carries a language code
        # ('en'), but detect_language must report "" so the gate won't skip.
        self.assertEqual(self._run([{"no_speech_prob": 0.9}], "en"), "")

    def test_speech_returns_detected_language(self):
        self.assertEqual(self._run([{"no_speech_prob": 0.2}], "he"), "he")


class TestDetectLanguageMultiWindow(unittest.TestCase):
    """detect_language sampling several windows with a gate-match language."""

    SPEECH = [{"no_speech_prob": 0.2}]
    SILENCE = [{"no_speech_prob": 0.95}]

    def _run(self, windows, offsets, match_lang):
        # windows: list of (segments, language) returned per offset, in order.
        with patch("transcribe._detect_clip", side_effect=list(windows)) as clip, \
                patch("subprocess.run") as mock_run, \
                patch("os.unlink"):
            mock_run.return_value.returncode = 0
            result = transcribe.detect_language(
                "mlx-whisper", "/tmp/a.m4a", "large-turbo-q4",
                sample_seconds=30, offsets=offsets, match_lang=match_lang)
        return result, clip.call_count

    def test_english_opener_then_hebrew_passes(self):
        # The real-world case: window 0 reads 'en', window 1 is Hebrew. With a
        # 'he' gate the meeting must pass on the later window.
        result, _ = self._run(
            [(self.SPEECH, "en"), (self.SPEECH, "he")],
            offsets=[0, 90], match_lang="he")
        self.assertEqual(result, "he")

    def test_match_short_circuits_remaining_windows(self):
        # Once a window matches the gate, later windows aren't sampled.
        result, calls = self._run(
            [(self.SPEECH, "he"), (self.SPEECH, "en"), (self.SPEECH, "en")],
            offsets=[0, 90, 180], match_lang="he")
        self.assertEqual(result, "he")
        self.assertEqual(calls, 1)

    def test_genuinely_english_meeting_still_reports_en(self):
        # No window is Hebrew → returns the (most common) real code so the gate
        # still skips a truly English meeting.
        result, _ = self._run(
            [(self.SPEECH, "en"), (self.SPEECH, "en"), (self.SPEECH, "en")],
            offsets=[0, 90, 180], match_lang="he")
        self.assertEqual(result, "en")

    def test_all_silence_returns_undetermined(self):
        result, _ = self._run(
            [(self.SILENCE, "en"), (self.SILENCE, "en")],
            offsets=[0, 90], match_lang="he")
        self.assertEqual(result, "")

    def test_silent_windows_skipped_in_vote(self):
        # A silent window's bogus language must not count toward the vote.
        result, _ = self._run(
            [(self.SILENCE, "fr"), (self.SPEECH, "en")],
            offsets=[0, 90], match_lang="he")
        self.assertEqual(result, "en")

    def test_iw_window_matches_he_gate(self):
        # Legacy 'iw' code normalizes to 'he' and satisfies a 'he' gate.
        result, _ = self._run(
            [(self.SPEECH, "iw")], offsets=[0], match_lang="he")
        self.assertEqual(result, "he")

    def test_all_windows_failed_raises(self):
        # Every ffmpeg slice fails (missing binary, corrupt input) → this is a
        # real detection failure, not "silent", so the caller must skip rather
        # than proceed. _detect_clip is never reached.
        with patch("transcribe._detect_clip") as clip, \
                patch("subprocess.run") as mock_run, \
                patch("os.unlink"):
            mock_run.return_value.returncode = 1
            with self.assertRaises(RuntimeError):
                transcribe.detect_language(
                    "mlx-whisper", "/tmp/a.m4a", "large-turbo-q4",
                    sample_seconds=30, offsets=[0, 90, 180], match_lang="he")
        clip.assert_not_called()


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


class TestLoadDiarizationPipelineTokenHandling(unittest.TestCase):
    """Regression test: empty HF_TOKEN env must not be passed verbatim to pyannote.

    When .env has HF_TOKEN="" and `set -a; source .env` exports it, the
    Python child sees os.environ["HF_TOKEN"] = "". That empty string must
    be normalized to None so pyannote falls back to the cached CLI login
    instead of attempting auth with an empty token.
    """

    def _invoke_with_env_token(self, env_value):
        """Call load_diarization_pipeline with mocked deps and return the
        token argument that would have been passed to pyannote."""
        captured = {}

        # Build a fake pyannote.audio.Pipeline whose from_pretrained
        # records the use_auth_token kwarg
        fake_pipeline_class = MagicMock()
        fake_pipeline_instance = MagicMock()

        def fake_from_pretrained(repo, use_auth_token=None, **kwargs):
            captured["token"] = use_auth_token
            return fake_pipeline_instance

        fake_pipeline_class.from_pretrained = fake_from_pretrained

        fake_pyannote_audio = MagicMock()
        fake_pyannote_audio.Pipeline = fake_pipeline_class
        fake_torch = MagicMock()
        fake_torch.device = MagicMock(return_value="cpu")
        fake_torch.load = MagicMock()

        env_patch = {} if env_value is None else {"HF_TOKEN": env_value}
        with patch.dict(os.environ, env_patch, clear=False), \
             patch.dict(sys.modules, {
                 "pyannote": MagicMock(),
                 "pyannote.audio": fake_pyannote_audio,
                 "torch": fake_torch,
             }):
            # If env_value is None, also strip an existing HF_TOKEN from environ
            if env_value is None:
                os.environ.pop("HF_TOKEN", None)
            transcribe.load_diarization_pipeline(auth_token=None)
        return captured["token"]

    def test_empty_env_token_normalized_to_none(self):
        # Empty string from .env must not become use_auth_token=""
        token = self._invoke_with_env_token("")
        self.assertIsNone(token,
            "Empty HF_TOKEN env should be normalized to None so pyannote "
            "falls back to cached huggingface-cli login")

    def test_real_env_token_propagates(self):
        token = self._invoke_with_env_token("hf_realToken123")
        self.assertEqual(token, "hf_realToken123")

    def test_unset_env_token_is_none(self):
        token = self._invoke_with_env_token(None)
        self.assertIsNone(token)

    def test_torch_load_is_restored_after_pipeline_load(self):
        """The weights_only=False monkeypatch must be reverted so it
        doesn't leak to other torch.load callers in the same process."""
        import types
        original_load = MagicMock(name="original_torch_load")
        fake_torch = types.SimpleNamespace(
            load=original_load,
            device=MagicMock(return_value="cpu"),
        )
        fake_pipeline_class = MagicMock()
        fake_pipeline_class.from_pretrained = MagicMock(return_value=MagicMock())
        fake_pyannote_audio = MagicMock()
        fake_pyannote_audio.Pipeline = fake_pipeline_class

        with patch.dict(sys.modules, {
            "pyannote": MagicMock(),
            "pyannote.audio": fake_pyannote_audio,
            "torch": fake_torch,
        }):
            transcribe.load_diarization_pipeline(auth_token="hf_x")

        # After the call, torch.load must point back to the original
        self.assertIs(fake_torch.load, original_load,
            "torch.load was not restored — leaving the patch in place forces "
            "weights_only=False on every subsequent torch.load call in the "
            "process, which is a security regression for library callers.")

    def test_torch_load_restored_even_on_exception(self):
        """Restoration must happen on the error path too (try/finally)."""
        import types
        original_load = MagicMock(name="original_torch_load")
        fake_torch = types.SimpleNamespace(
            load=original_load,
            device=MagicMock(return_value="cpu"),
        )
        fake_pipeline_class = MagicMock()
        fake_pipeline_class.from_pretrained = MagicMock(
            side_effect=RuntimeError("simulated pyannote load failure")
        )
        fake_pyannote_audio = MagicMock()
        fake_pyannote_audio.Pipeline = fake_pipeline_class

        with patch.dict(sys.modules, {
            "pyannote": MagicMock(),
            "pyannote.audio": fake_pyannote_audio,
            "torch": fake_torch,
        }):
            with self.assertRaises(RuntimeError):
                transcribe.load_diarization_pipeline(auth_token="hf_x")

        self.assertIs(fake_torch.load, original_load,
            "torch.load must be restored even when pipeline load fails")


class TestResolveHFToken(unittest.TestCase):
    def setUp(self):
        # Use a tempdir-based fake HOME so we never read/write real cache
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.fake_cached = os.path.join(self.tmpdir, "token")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_env_token_takes_precedence(self):
        # Even with cached file present, explicit env value wins
        with open(self.fake_cached, "w") as f:
            f.write("hf_cachedXXX")
        token, source = transcribe.resolve_hf_token("hf_envYYY", cached_path=self.fake_cached)
        self.assertEqual(token, "hf_envYYY")
        self.assertEqual(source, "env")

    def test_falls_back_to_cached_login(self):
        # Empty env, cached file present → return None (let huggingface_hub
        # use its own cached creds) and signal the source via label
        with open(self.fake_cached, "w") as f:
            f.write("hf_cached")
        token, source = transcribe.resolve_hf_token("", cached_path=self.fake_cached)
        self.assertIsNone(token)
        self.assertEqual(source, "cached-cli-login")

    def test_neither_raises_with_actionable_message(self):
        # Empty env, no cached file → raise so caller can print help
        token_path = os.path.join(self.tmpdir, "does-not-exist")
        with self.assertRaises(FileNotFoundError) as ctx:
            transcribe.resolve_hf_token("", cached_path=token_path)
        msg = str(ctx.exception)
        self.assertIn("HF_TOKEN", msg)
        self.assertIn("huggingface-cli login", msg)

    def test_none_env_token_treated_as_empty(self):
        # Defensive: callers may pass None instead of "" when env var unset
        token_path = os.path.join(self.tmpdir, "missing")
        with self.assertRaises(FileNotFoundError):
            transcribe.resolve_hf_token(None, cached_path=token_path)


if __name__ == "__main__":
    unittest.main()
