"""Offline OCR contracts: synthetic video and fake recognition, no API calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import av
import numpy as np

from media_pipeline import ocr


def make_video(path: Path, *, frames: int = 22, rate: int = 10, value: int = 80):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width, stream.height = 96, 64
        stream.pix_fmt = "yuv420p"
        for index in range(frames):
            pixels = np.full((64, 96, 3), min(value + index, 255), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="bgr24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


class FakeEngine:
    def __init__(self, *, empty=False, fail_at=None):
        self.calls = 0
        self.empty = empty
        self.fail_at = fail_at

    def __call__(self, image, *, text_score):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("inference failure")
        assert text_score == 0.0
        if self.empty:
            return SimpleNamespace(txts=None, scores=None, boxes=None)
        return SimpleNamespace(
            txts=("低置信原文",), scores=(0.15,),
            boxes=np.array([[[1, 2], [40, 2], [40, 20], [1, 20]]]),
        )


class OCRTests(unittest.TestCase):
    def test_fractional_last_second_is_sampled(self):
        self.assertEqual(ocr.sample_times(Fraction(27104, 100))[-1], 271.0)
        self.assertEqual(len(ocr.sample_times(271.04)), 272)
        self.assertEqual(ocr.sample_times(2), [0.0, 1.0])
        self.assertEqual(ocr.sample_times(0.04), [0.0])

    def test_nearest_actual_timestamp_and_earlier_ties(self):
        decoded = [(Fraction(0), "a"), (Fraction(4, 5), "b"),
                   (Fraction(6, 5), "c"), (Fraction(19, 10), "d")]
        selected = list(ocr._nearest_frames(decoded, [0.0, 1.0, 2.0]))
        self.assertEqual([item[3] for item in selected], ["a", "b", "d"])
        self.assertEqual([float(item[2]) for item in selected], [0, 0.8, 1.9])

    def test_decoder_is_exhausted_after_last_sample(self):
        def broken_decode():
            yield Fraction(0), "a"
            yield Fraction(1), "b"
            raise ocr.OCRProcessingError("truncated end")
        with self.assertRaisesRegex(ocr.OCRProcessingError, "truncated end"):
            list(ocr._nearest_frames(broken_decode(), [0.0, 1.0]))

    def test_no_text_and_evidence_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mp4"
            make_video(source)
            engine = FakeEngine(empty=True)
            events = []
            with patch.object(ocr, "_new_engine", return_value=engine):
                result = ocr.recognize(source, root / "work", progress=events.append)
            self.assertEqual(result["expected_frame_count"], 3)
            self.assertEqual(engine.calls, 3)
            self.assertEqual(result["empty_frame_count"], 3)
            self.assertEqual([r["timestamp_seconds"] for r in result["frames"]], [0, 1, 2])
            self.assertTrue(all(Path(r["image_path"]).is_file() for r in result["frames"]))
            self.assertEqual(events[-1]["completed_frames"], 3)
            self.assertTrue(result["coverage_verified"])

    def test_low_confidence_preserved_and_cache_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mp4"
            make_video(source)
            engine = FakeEngine()
            with patch.object(ocr, "_new_engine", return_value=engine):
                first = ocr.recognize(source, root / "work")
            self.assertEqual(first["frames"][0]["lines"][0]["confidence"], 0.15)
            with patch.object(ocr, "_new_engine", side_effect=AssertionError("unneeded engine")):
                second = ocr.recognize(source, root / "work")
            self.assertEqual(second["cache_hits"], 3)
            self.assertEqual(first["frames"], second["frames"])

    def test_source_and_config_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mp4"
            make_video(source)
            engine = FakeEngine()
            with patch.object(ocr, "_new_engine", return_value=engine):
                first = ocr.recognize(source, root / "work")
                make_video(source, value=150)
                second = ocr.recognize(source, root / "work")
                with patch.dict(ocr.OCR_PARAMS, {"Det.limit_side_len": 960}):
                    third = ocr.recognize(source, root / "work")
            self.assertEqual(engine.calls, 9)
            self.assertNotEqual(first["cache_signature"], second["cache_signature"])
            self.assertNotEqual(second["cache_signature"], third["cache_signature"])

    def test_missing_evidence_invalidates_only_one_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mp4"
            make_video(source)
            engine = FakeEngine()
            with patch.object(ocr, "_new_engine", return_value=engine):
                first = ocr.recognize(source, root / "work")
                Path(first["frames"][1]["image_path"]).unlink()
                second = ocr.recognize(source, root / "work")
            self.assertEqual(engine.calls, 4)
            self.assertEqual(second["cache_hits"], 2)

    def test_failure_is_not_cached_as_completion_and_can_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mp4"
            make_video(source)
            with patch.object(ocr, "_new_engine", return_value=FakeEngine(fail_at=2)):
                with self.assertRaisesRegex(RuntimeError, "inference failure"):
                    ocr.recognize(source, root / "work")
            cached = list((root / "work" / "ocr_cache").glob("*/frame_*.json"))
            self.assertEqual(len(cached), 1)
            self.assertEqual(json.loads(cached[0].read_text())["frame"]["index"], 0)
            engine = FakeEngine()
            with patch.object(ocr, "_new_engine", return_value=engine):
                resumed = ocr.recognize(source, root / "work")
            self.assertEqual(resumed["cache_hits"], 1)
            self.assertEqual(engine.calls, 2)

    def test_malformed_engine_output_is_not_empty_success(self):
        with self.assertRaises(ocr.OCRProcessingError):
            ocr._recognize_lines(lambda *args, **kwargs: None, None)

    def test_truncated_decode_rejects_even_if_all_targets_were_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "clip.mp4"
            make_video(source)
            info = ocr._read_video_info(source)
            wrong_info = ocr.VideoInfo(Fraction(4), info.origin, info.frame_period, info.width, info.height)
            with self.assertRaisesRegex(ocr.OCRProcessingError, "Incomplete video decode"):
                list(ocr._decoded_frames(source, wrong_info))


if __name__ == "__main__":
    unittest.main()
