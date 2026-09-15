import json
from pathlib import Path
import tempfile
import unittest

from media_pipeline.combined_word import export
from media_pipeline.delivery_qa import build_qa


class DeliveryQATests(unittest.TestCase):
    def test_records_structural_and_model_coverage_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "frame.jpg"
            evidence.write_bytes(b"jpeg")
            digest = "a" * 64
            work = root / digest[:16]
            work.mkdir()
            manifest = {"source_name": "静音.mp4", "source_path": str(root / "静音.mp4"),
                        "sha256": digest, "has_audio": False, "expected_frame_count": 1}
            (work / "asr.json").write_text(json.dumps({
                "source_sha256": digest, "skipped_reason": "no_audio_stream"}))
            (work / "ocr.json").write_text(json.dumps({
                "engine": "RapidOCR", "sample_interval_seconds": 1.0,
                "coverage_verified": True, "expected_frame_count": 1,
                "source_fingerprint": {"sha256": digest},
                "frames": [{"timestamp_seconds": 0, "image_path": str(evidence)}],
            }))
            inventory = {"videos": [manifest], "courseware": [], "audio_video_count": 0,
                         "silent_video_count": 1}
            corpus = {"files": [{
                "source_name": "静音.mp4", "source_sha256": digest, "kind": "video",
                "evidence_status": {"asr_source": "no-audio-stream"},
                "sections": {"ASR 结果": ["不适用"], "OCR 结果": ["画面"],
                             "GPT 融合校对结果": ["融合"]},
            }]}
            word = root / "result.docx"
            export(corpus, inventory, word)
            result = build_qa(inventory, {"files": [], "page_count": 0}, corpus,
                              word, root, rehash=False)
            self.assertTrue(result["structural_delivery_complete"])
            self.assertEqual(result["silent_asr_skips_complete"], 1)
            self.assertTrue(result["specified_mimo_asr_complete"])
            self.assertTrue(result["specified_rapidocr_complete"])
            self.assertTrue(result["full_fusion"]["passed"])
            self.assertTrue(result["specified_processing_complete"])
            self.assertFalse(result["mimo_visual_supplement_complete"])


if __name__ == "__main__":
    unittest.main()
