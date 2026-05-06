"""Tests for diarization helpers in transcribe.py.

These functions are pure (no model inference) so they're cheap to test
exhaustively. Covers Whisper↔Pyannote alignment, consecutive-speaker
merging, localized speaker labels, and transcript formatting.
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import transcribe  # noqa: E402


class FakeDiarization:
    """Minimal stand-in for Pyannote's Annotation.itertracks(yield_label=True)."""

    def __init__(self, turns):
        # turns: list of (start, end, speaker_id)
        self._turns = turns

    def itertracks(self, yield_label=True):
        for start, end, speaker in self._turns:
            yield SimpleNamespace(start=start, end=end), None, speaker


class TestAlign(unittest.TestCase):
    def test_segment_assigned_to_max_overlap_speaker(self):
        whisper_segs = [{"start": 0.0, "end": 5.0, "text": "hello"}]
        diar = FakeDiarization([
            (0.0, 1.0, "SPEAKER_00"),   # 1s overlap
            (1.0, 5.0, "SPEAKER_01"),   # 4s overlap → wins
        ])
        result = transcribe.align_whisper_with_diarization(whisper_segs, diar)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["speaker"], "SPEAKER_01")
        self.assertEqual(result[0]["text"], "hello")

    def test_no_overlap_yields_unknown_speaker(self):
        whisper_segs = [{"start": 10.0, "end": 12.0, "text": "orphan"}]
        diar = FakeDiarization([(0.0, 5.0, "SPEAKER_00")])
        result = transcribe.align_whisper_with_diarization(whisper_segs, diar)
        self.assertEqual(result[0]["speaker"], "UNKNOWN")

    def test_empty_segment_text_skipped(self):
        whisper_segs = [{"start": 0.0, "end": 1.0, "text": "   "}]
        diar = FakeDiarization([(0.0, 1.0, "SPEAKER_00")])
        result = transcribe.align_whisper_with_diarization(whisper_segs, diar)
        self.assertEqual(result, [])


class TestMerge(unittest.TestCase):
    def test_consecutive_same_speaker_merges(self):
        aligned = [
            {"speaker": "S0", "start": 0.0, "end": 1.0, "text": "one"},
            {"speaker": "S0", "start": 1.0, "end": 2.0, "text": "two"},
            {"speaker": "S1", "start": 2.0, "end": 3.0, "text": "three"},
        ]
        merged = transcribe.merge_consecutive_speaker_segments(aligned)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["text"], "one two")
        self.assertEqual(merged[0]["end"], 2.0)
        self.assertEqual(merged[1]["text"], "three")

    def test_alternating_speakers_not_merged(self):
        aligned = [
            {"speaker": "A", "start": 0, "end": 1, "text": "x"},
            {"speaker": "B", "start": 1, "end": 2, "text": "y"},
            {"speaker": "A", "start": 2, "end": 3, "text": "z"},
        ]
        merged = transcribe.merge_consecutive_speaker_segments(aligned)
        self.assertEqual(len(merged), 3)

    def test_empty_input(self):
        self.assertEqual(transcribe.merge_consecutive_speaker_segments([]), [])


class TestSpeakerLabel(unittest.TestCase):
    def test_hebrew_label(self):
        self.assertEqual(transcribe.get_speaker_label("SPEAKER_00", "he"), "דובר 1")
        self.assertEqual(transcribe.get_speaker_label("SPEAKER_02", "he"), "דובר 3")

    def test_english_label(self):
        self.assertEqual(transcribe.get_speaker_label("SPEAKER_00", "en"), "Speaker 1")

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(transcribe.get_speaker_label("SPEAKER_00", "xx"), "Speaker 1")

    def test_malformed_id_falls_back(self):
        # Non-integer suffix should not crash
        label = transcribe.get_speaker_label("UNKNOWN", "he")
        self.assertIn("דובר", label)


class TestFormatTranscript(unittest.TestCase):
    def test_plain_format(self):
        merged = [
            {"speaker": "SPEAKER_00", "start": 0, "end": 5, "text": "hello"},
            {"speaker": "SPEAKER_01", "start": 5, "end": 10, "text": "world"},
        ]
        out = transcribe.format_diarized_transcript(merged, "en", include_timestamps=False)
        self.assertEqual(out, "Speaker 1: hello\nSpeaker 2: world")

    def test_format_with_timestamps(self):
        merged = [{"speaker": "SPEAKER_00", "start": 65, "end": 130, "text": "hi"}]
        out = transcribe.format_diarized_transcript(merged, "en", include_timestamps=True)
        self.assertEqual(out, "Speaker 1: [01:05-02:10] hi")

    def test_hebrew_format(self):
        merged = [{"speaker": "SPEAKER_00", "start": 0, "end": 1, "text": "שלום"}]
        out = transcribe.format_diarized_transcript(merged, "he", include_timestamps=False)
        self.assertEqual(out, "דובר 1: שלום")


if __name__ == "__main__":
    unittest.main()
