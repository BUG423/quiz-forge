from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import ocr_video_scene_changes as scene
from scripts import qa_video_scene_ocr as qa


class VideoSceneOCRQATest(unittest.TestCase):
    def test_line_gate_preserves_raw_text_but_rejects_invalid_evidence(self) -> None:
        qa._validate_line({
            "text": "字",
            "confidence": 0.0,
            "box": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        }, "line")
        with self.assertRaisesRegex(qa.SceneQAError, "confidence"):
            qa._validate_line({
                "text": "字", "confidence": True,
                "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
            }, "line")
        with self.assertRaisesRegex(qa.SceneQAError, "4个点"):
            qa._validate_line({
                "text": "字", "confidence": 0.5,
                "box": [[0, 0], [1, 0], [1, 1]],
            }, "line")

    def test_merged_top_requires_exact_authoritative_order_and_shard_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [scene.VideoSource(
                asset_id=f"asset-{index}", source_path=root / f"{index}.mp4",
                source_sha256=f"{index:064x}", source_name=f"{index}.mp4",
                year=2026, source_type="top_level",
            ) for index in range(scene.SOURCE_COUNT)]
            provenance = []
            for index in range(4):
                path = root / f"shard-{index}.json"
                path.write_text(json.dumps({"shard": index}), encoding="utf-8")
                provenance.append({
                    "shard_index": index,
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "source_count": 0,
                })
            document = {
                "schema_version": scene.SCHEMA_VERSION,
                "processor_version": scene.PROCESSOR_VERSION,
                "status": "complete",
                "mode": "ocr",
                "source_count": scene.SOURCE_COUNT,
                "completed_source_count": scene.SOURCE_COUNT,
                "failed_source_count": 0,
                "config": scene.build_config(),
                "sources": [{"source_sha256": item.source_sha256}
                            for item in sources],
                "failures": [],
                "coverage_verified": True,
                "shard": None,
                "merged_shards": provenance,
            }
            qa._validate_merged_top(document, sources)
            document["sources"][0], document["sources"][1] = (
                document["sources"][1], document["sources"][0])
            with self.assertRaisesRegex(qa.SceneQAError, "精确顺序"):
                qa._validate_merged_top(document, sources)

    def test_cache_locator_rejects_duplicate_source_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sha = "a" * 64
            for shard in (0, 1):
                path = (root / f"shard-{shard}-of-4" / "video-scene-ocr-cache"
                        / sha / (str(shard) * 64) / "result.json")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"source_sha256": sha}), encoding="utf-8")
            with self.assertRaisesRegex(qa.SceneQAError, "多个完整"):
                qa._locate_results(root)


if __name__ == "__main__":
    unittest.main()
