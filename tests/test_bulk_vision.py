from pathlib import Path
import tempfile
import unittest

from media_pipeline.bulk_vision import _legacy_seconds, select_frames


class BulkVisionTests(unittest.TestCase):
    def test_legacy_seconds(self):
        self.assertEqual(_legacy_seconds("画面 01:02:03.500"), 3723.5)
        self.assertIsNone(_legacy_seconds("无时间"))

    def test_select_frames_is_bounded_and_keeps_useful_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = []
            for index in range(200):
                image = root / f"{index}.jpg"
                image.write_bytes(b"\xff\xd8\xffx")
                frames.append({
                    "index": index,
                    "timestamp_seconds": float(index),
                    "image_path": str(image),
                    "lines": [{"text": f"第{index // 10}页", "confidence": 0.9}],
                })
            manifest = {"duration_seconds": 200.0, "has_audio": True}
            legacy = {"ocr_blocks": [{"label": "画面 00:01:40.000", "text": "核心页"}]}
            selected = select_frames(manifest, {"frames": frames}, legacy)
            self.assertLessEqual(len(selected), 48)
            self.assertGreaterEqual(len(selected), 1)
            self.assertEqual(len({item["index"] for item in selected}), len(selected))
            self.assertTrue(any(abs(item["index"] - 100) <= 2 for item in selected))


if __name__ == "__main__":
    unittest.main()
