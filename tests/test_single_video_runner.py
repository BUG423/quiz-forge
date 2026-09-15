from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from media_pipeline.runner import atomic_json, inspect_source, validate_results


class RunnerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        image = Path(self.tmp.name) / "frame.jpg"
        image.touch()
        self.manifest = {"sha256": "sample", "expected_frame_count": 2, "audio_start_seconds": 0.0, "audio_end_seconds": 1.5}
        self.asr = {"model": "mimo-v2.5-asr", "source_sha256": "sample",
                    "coverage": {"complete": True, "sample_count": 24000, "covered_sample_count": 24000}, "segments": [
            {"start_seconds": 0, "end_seconds": 0.8, "text": "测试", "finish_reason": "stop"},
            {"start_seconds": 0.8, "end_seconds": 1.5, "text": "视频", "finish_reason": "stop"},
        ]}
        self.ocr = {"expected_frame_count": 2, "source_fingerprint": {"sha256": "sample"},
                    "coverage_verified": True, "engine": "RapidOCR", "sample_interval_seconds": 1.0, "frames": [
            {"timestamp_seconds": i, "actual_timestamp_seconds": i, "image_path": str(image), "lines": []}
            for i in range(2)
        ]}

    def test_complete_even_when_no_text_on_frames(self):
        self.assertTrue(validate_results(self.manifest, self.asr, self.ocr)["passed"])

    def test_missing_ocr_second_rejected(self):
        self.ocr["frames"].pop()
        with self.assertRaisesRegex(ValueError, "OCR 帧数"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_audio_gap_rejected(self):
        self.asr["segments"][1]["start_seconds"] = 1.1
        with self.assertRaisesRegex(ValueError, "时间间隙"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_wrong_model_rejected(self):
        self.asr["model"] = "another-model"
        with self.assertRaisesRegex(ValueError, "指定模型"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_truncated_response_rejected(self):
        self.asr["segments"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(ValueError, "未正常完成"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_evidence_required(self):
        self.ocr["frames"][0]["image_path"] = str(Path(self.tmp.name) / "missing.jpg")
        with self.assertRaisesRegex(ValueError, "证据图片"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_directory_is_not_batch_input(self):
        with self.assertRaisesRegex(ValueError, "一个视频文件"):
            inspect_source(Path(self.tmp.name))

    def test_wrong_source_cache_rejected(self):
        self.ocr["source_fingerprint"]["sha256"] = "other"
        with self.assertRaisesRegex(ValueError, "指纹"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_incomplete_decoder_flag_rejected(self):
        self.ocr["coverage_verified"] = False
        with self.assertRaisesRegex(ValueError, "完整视频解码"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_missing_samples_flag_rejected(self):
        self.asr["coverage"]["covered_sample_count"] = 23999
        with self.assertRaisesRegex(ValueError, "音频样本覆盖"):
            validate_results(self.manifest, self.asr, self.ocr)

    def test_atomic_json_allows_parallel_writers(self):
        target = Path(self.tmp.name) / "state.json"
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda value: atomic_json(target, {"value": value}), range(40)))
        self.assertIn(json.loads(target.read_text())["value"], range(40))
        self.assertEqual(list(target.parent.glob(".state.json.*")), [])


if __name__ == "__main__":
    unittest.main()
