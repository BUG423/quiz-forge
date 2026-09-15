"""Offline checks for source-bound resumable batch decisions."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from media_pipeline.batch_2025 import TOKEN_PLAN_ENDPOINT, _valid_asr, _valid_ocr, discover


class Batch2025Tests(unittest.TestCase):
    def test_discovery_skips_zip_and_is_non_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("b.mp4", "a.mp4", "课件.pdf", "其它课件.zip"):
                (root / name).write_bytes(b"x")
            (root / "nested").mkdir()
            (root / "nested" / "hidden.mp4").write_bytes(b"x")
            videos, courseware = discover(root)
            self.assertEqual([p.name for p in videos], ["a.mp4", "b.mp4"])
            self.assertEqual([p.name for p in courseware], ["课件.pdf"])

    def test_no_audio_skip_is_source_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asr.json"
            path.write_text(json.dumps({"source_sha256": "a" * 64,
                                        "skipped_reason": "no_audio_stream"}))
            manifest = {"sha256": "a" * 64, "has_audio": False}
            self.assertTrue(_valid_asr(path, manifest))
            manifest["sha256"] = "b" * 64
            self.assertFalse(_valid_asr(path, manifest))

    def test_asr_requires_complete_samples_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asr.json"
            value = {"model": "mimo-v2.5-asr", "source_sha256": "a" * 64,
                     "configuration": {"endpoint": TOKEN_PLAN_ENDPOINT},
                     "coverage": {"complete": True, "sample_count": 10,
                                  "covered_sample_count": 10},
                     "segments": [{"finish_reason": "stop"}]}
            path.write_text(json.dumps(value))
            manifest = {"sha256": "a" * 64, "has_audio": True}
            self.assertTrue(_valid_asr(path, manifest))
            value["segments"][0]["finish_reason"] = "length"
            path.write_text(json.dumps(value))
            self.assertFalse(_valid_asr(path, manifest))

    def test_asr_rejects_non_token_plan_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asr.json"
            value = {"model": "mimo-v2.5-asr", "source_sha256": "a" * 64,
                     "configuration": {
                         "endpoint": "https://api.xiaomimimo.com/v1/chat/completions",
                     },
                     "coverage": {"complete": True, "sample_count": 10,
                                  "covered_sample_count": 10},
                     "segments": [{"finish_reason": "stop"}]}
            path.write_text(json.dumps(value))
            manifest = {"sha256": "a" * 64, "has_audio": True}
            self.assertFalse(_valid_asr(path, manifest))

    def test_ocr_requires_every_second_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "frame.jpg"
            evidence.write_bytes(b"jpeg")
            value = {"engine": "RapidOCR", "sample_interval_seconds": 1.0,
                     "coverage_verified": True,
                     "source_fingerprint": {"sha256": "a" * 64},
                     "expected_frame_count": 2,
                     "frames": [{"timestamp_seconds": 0, "image_path": str(evidence)},
                                {"timestamp_seconds": 1, "image_path": str(evidence)}]}
            path = root / "ocr.json"
            path.write_text(json.dumps(value))
            manifest = {"sha256": "a" * 64, "expected_frame_count": 2}
            self.assertTrue(_valid_ocr(path, manifest))
            value["frames"][1]["timestamp_seconds"] = 2
            path.write_text(json.dumps(value))
            self.assertFalse(_valid_ocr(path, manifest))


if __name__ == "__main__":
    unittest.main()
