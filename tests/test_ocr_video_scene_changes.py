from __future__ import annotations

from fractions import Fraction
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from media_pipeline.ocr import OCRProcessingError, VideoInfo
from scripts import ocr_video_scene_changes as scene


class FakeFrame:
    def __init__(self, pts: int, image: np.ndarray, *, denominator: int = 10) -> None:
        self.pts = pts
        self.time_base = Fraction(1, denominator)
        self._image = image

    def to_ndarray(self, *, format: str) -> np.ndarray:
        if format != "bgr24":
            raise AssertionError(format)
        return self._image.copy()


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, image: np.ndarray, *, text_score: float):
        self.calls += 1
        if text_score != 0.0:
            raise AssertionError("raw OCR must set text_score=0.0")
        return SimpleNamespace(
            txts=["字", "字", "A"],
            scores=[0.01, 0.02, 0.0],
            boxes=np.array([
                [[0, 0], [5, 0], [5, 5], [0, 5]],
                [[1, 1], [6, 1], [6, 6], [1, 6]],
                [[2, 2], [7, 2], [7, 7], [2, 7]],
            ], dtype=np.float32),
        )


class SceneChangeOCRTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source(self, name: str = "普通课程.mp4", *, year: int = 2026) -> scene.VideoSource:
        path = self.root / name
        path.write_bytes(b"test-video-source")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return scene.VideoSource(
            asset_id=f"asset/{digest}", source_path=path,
            source_sha256=digest, source_name=name, year=year,
            source_type="top_level",
        )

    def test_raw_ocr_retains_duplicates_single_characters_and_low_scores(self) -> None:
        engine = FakeEngine()
        lines = scene.recognize_raw_lines(engine, np.zeros((20, 20, 3), dtype=np.uint8))
        self.assertEqual(["字", "字", "A"], [item["text"] for item in lines])
        self.assertEqual([0.01, 0.02, 0.0], [item["confidence"] for item in lines])
        self.assertEqual(3, len(lines))

    def test_change_detection_selects_only_stable_post_cut_frame_and_boundaries(self) -> None:
        black = np.zeros((80, 120, 3), dtype=np.uint8)
        white = np.full((80, 120, 3), 255, dtype=np.uint8)
        decoded = [
            (Fraction(0), FakeFrame(0, black)),
            (Fraction(1, 10), FakeFrame(1, white)),
            (Fraction(2, 10), FakeFrame(2, white)),
            (Fraction(3, 10), FakeFrame(3, white)),
            (Fraction(4, 10), FakeFrame(4, white)),
        ]
        config = dict(scene.DEFAULT_CONFIG)
        candidates = list(scene.iter_scene_candidates(decoded, config))
        reasons = [reason for candidate in candidates for reason in candidate.reasons]
        self.assertIn("video_start", reasons)
        self.assertIn("stable_after_strong_scene_change", reasons)
        self.assertIn("video_end", reasons)
        self.assertFalse(any(reason.startswith("before_") for reason in reasons))
        stable = next(item for item in candidates
                      if "stable_after_strong_scene_change" in item.reasons)
        self.assertEqual(4, stable.pts)

    def test_text_edge_change_alone_cannot_trigger_v2(self) -> None:
        base = np.zeros((80, 120, 3), dtype=np.uint8)
        text_a = base.copy()
        text_b = base.copy()
        text_a[30:32, 10:60] = 255
        text_b[30:32, 60:110] = 255
        decoded = [
            (Fraction(0), FakeFrame(0, text_a)),
            (Fraction(1, 10), FakeFrame(1, text_b)),
            (Fraction(2, 10), FakeFrame(2, text_b)),
            (Fraction(3, 10), FakeFrame(3, text_b)),
            (Fraction(4, 10), FakeFrame(4, text_b)),
        ]
        config = dict(scene.DEFAULT_CONFIG)
        config["scene_change_threshold"] = 0.10
        candidates = list(scene.iter_scene_candidates(decoded, config))
        self.assertFalse(any(
            "stable_after_strong_scene_change" in item.reasons for item in candidates))

    def test_process_excludes_existing_sha_preserves_raw_lines_and_resumes(self) -> None:
        source = self._source()
        black = np.zeros((80, 120, 3), dtype=np.uint8)
        white = np.full((80, 120, 3), 255, dtype=np.uint8)
        decoded = [
            (Fraction(0), FakeFrame(0, black)),
            (Fraction(1, 10), FakeFrame(1, white)),
        ]
        info = VideoInfo(Fraction(2), Fraction(0), Fraction(1, 10), 120, 80)
        existing_sha = hashlib.sha256(
            scene._jpeg(scene._resize_maximum(black, 1600), 95)).hexdigest()
        engine = FakeEngine()
        with (patch.object(scene, "_read_video_info", return_value=info),
              patch.object(scene, "_decoded_frames", return_value=iter(decoded))):
            result = scene.process_source(
                source, self.root / "work", self.root / "existing",
                dict(scene.DEFAULT_CONFIG), engine_factory=lambda params: engine,
                existing_hashes={existing_sha})
        self.assertEqual("complete", result["status"])
        self.assertEqual(1, result["excluded_existing_second_frame_count"])
        self.assertEqual(1, result["new_ocr_frame_count"])
        self.assertEqual(["字", "字", "A"],
                         [line["text"] for line in result["frames"][0]["lines"]])
        frame = result["frames"][0]
        self.assertEqual(source.source_sha256, frame["source_sha256"])
        self.assertEqual(1, frame["pts"])
        self.assertEqual({"numerator": 1, "denominator": 10}, frame["time_base"])
        self.assertEqual(4, len(frame["lines"][0]["box"]))
        self.assertTrue(Path(frame["image_path"]).is_file())

        with patch.object(scene, "_read_video_info", return_value=info):
            resumed = scene.process_source(
                source, self.root / "work", self.root / "existing",
                dict(scene.DEFAULT_CONFIG),
                engine_factory=lambda params: (_ for _ in ()).throw(
                    AssertionError("complete cache must avoid OCR")),
                existing_hashes={existing_sha})
        self.assertTrue(resumed["complete_cache_hit"])

    def test_detect_only_never_calls_ocr_and_reports_risk(self) -> None:
        source = self._source()
        black = np.zeros((80, 120, 3), dtype=np.uint8)
        white = np.full((80, 120, 3), 255, dtype=np.uint8)
        decoded = [
            (Fraction(0), FakeFrame(0, black)),
            (Fraction(1, 10), FakeFrame(1, white)),
            (Fraction(2, 10), FakeFrame(2, white)),
            (Fraction(3, 10), FakeFrame(3, white)),
            (Fraction(4, 10), FakeFrame(4, white)),
        ]
        info = VideoInfo(Fraction(5), Fraction(0), Fraction(1, 10), 120, 80)
        with (patch.object(scene, "_read_video_info", return_value=info),
              patch.object(scene, "_decoded_frames", return_value=iter(decoded))):
            result = scene.process_source(
                source, self.root / "work", self.root / "existing",
                dict(scene.DEFAULT_CONFIG), detect_only=True,
                engine_factory=lambda params: (_ for _ in ()).throw(
                    AssertionError("detect-only must never construct OCR")),
                existing_hashes=set(), existing_frame_count=5)
        self.assertEqual("detection_complete", result["status"])
        self.assertEqual(0, result["new_ocr_frame_count"])
        self.assertFalse(result["risk_gate"]["rejected"])
        self.assertEqual([], result["frames"])

    def test_candidate_risk_gate_rejects_before_ocr(self) -> None:
        risk = scene.assess_candidate_risk(11, 10.0, 10, 1.0)
        self.assertTrue(risk["rejected"])
        self.assertEqual(1.1, risk["candidate_rate_per_second"])
        self.assertEqual(2, len(risk["reasons"]))

    def test_2025_relay_and_zhenrong_use_original_resolution(self) -> None:
        self.assertTrue(scene.requires_original_resolution(
            self._source("核心价值宣传——《接力》.mp4", year=2025)))
        self.assertTrue(scene.requires_original_resolution(
            self._source("重大工程介绍——镇荣甲线.mp4", year=2025)))
        self.assertFalse(scene.requires_original_resolution(
            self._source("核心价值宣传——《接力》.mp4", year=2026)))

    def test_decode_failure_is_never_reported_as_success(self) -> None:
        source = self._source()
        black = np.zeros((80, 120, 3), dtype=np.uint8)
        info = VideoInfo(Fraction(1), Fraction(0), Fraction(1, 10), 120, 80)

        def broken_decode():
            yield Fraction(0), FakeFrame(0, black)
            raise OCRProcessingError("truncated stream")

        with (patch.object(scene, "_read_video_info", return_value=info),
              patch.object(scene, "_decoded_frames", return_value=broken_decode())):
            with self.assertRaisesRegex(scene.SceneOCRError, "视频解码失败.*truncated"):
                scene.process_source(
                    source, self.root / "work", self.root / "existing",
                    dict(scene.DEFAULT_CONFIG), engine_factory=lambda params: FakeEngine(),
                    existing_hashes=set())

    def test_discovery_uses_only_manifest_assets_and_embedded_files(self) -> None:
        source_paths = []
        for index in range(3):
            path = self.root / f"video-{index}.mp4"
            path.write_bytes(f"video-{index}".encode())
            source_paths.append(path)
        assets = []
        for index, path in enumerate(source_paths[:2], 1):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assets.append({
                "asset_id": f"asset-{index}", "kind": "video", "year": 2026,
                "source_path": path.name, "source_name": path.name,
                "source_sha256": digest,
            })
        assets.append({"asset_id": "not-video", "kind": "courseware"})
        asset_manifest = self.root / "asset_manifest.json"
        asset_manifest.write_text(json.dumps({"assets": assets}), encoding="utf-8")
        embedded_path = source_paths[2]
        embedded_sha = hashlib.sha256(embedded_path.read_bytes()).hexdigest()
        embedded_manifest = self.root / "embedded.json"
        embedded_manifest.write_text(json.dumps({
            "file_count": 1,
            "files": [{
                "output_path": str(embedded_path), "sha256": embedded_sha,
                "parent_source": str(self.root / "parent.pptx"), "slide": 4,
                "member": "ppt/media/media1.mp4",
            }],
        }), encoding="utf-8")
        sources = scene.discover_sources(
            asset_manifest, embedded_manifest, root=self.root,
            expected_top_level=2, expected_embedded=1)
        self.assertEqual(3, len(sources))
        self.assertEqual(["top_level", "top_level", "embedded"],
                         [item.source_type for item in sources])

    def _merge_fixture(self, shard_count: int = 4):
        expected = []
        for index in range(scene.SOURCE_COUNT):
            sha = f"{index:064x}"
            expected.append(scene.VideoSource(
                asset_id=f"asset-{index}", source_path=self.root / f"{index}.mp4",
                source_sha256=sha, source_name=f"{index}.mp4", year=2026,
                source_type="top_level",
            ))
        groups = [[] for _ in range(shard_count)]
        for index, source in enumerate(expected):
            groups[index % shard_count].append(source)
        paths = []
        for shard_index, group in enumerate(groups):
            records = [{
                "status": "complete",
                "source_sha256": source.source_sha256,
                "decode_coverage_verified": True,
                "risk_gate": {"rejected": False, "reasons": []},
                "new_candidate_frame_count": 0,
                "new_ocr_frame_count": 0,
                "frames": [],
            } for source in group]
            document = {
                "schema_version": scene.SCHEMA_VERSION,
                "processor_version": scene.PROCESSOR_VERSION,
                "status": "shard_complete",
                "mode": "shard",
                "source_count": len(records),
                "completed_source_count": len(records),
                "failed_source_count": 0,
                "config": {"test": "same-config"},
                "sources": records,
                "failures": [],
                "coverage_verified": True,
                "shard": {
                    "shard_index": shard_index,
                    "shard_count": shard_count,
                    "global_source_count": scene.SOURCE_COUNT,
                    "assigned_source_count": len(group),
                    "assigned_source_sha256": [item.source_sha256 for item in group],
                    "assigned_duration_seconds": float(len(group)),
                    "assignment_strategy": "longest_processing_time_first_greedy_v1",
                },
            }
            path = self.root / f"shard-{shard_index}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            paths.append(path)
        return expected, paths

    def test_greedy_shards_are_balanced_disjoint_and_complete(self) -> None:
        sources = [self._source(f"source-{index}.mp4") for index in range(8)]
        # _source uses content-identical files, so make identities distinct.
        sources = [scene.VideoSource(
            asset_id=f"asset-{index}", source_path=source.source_path,
            source_sha256=f"{index + 1:064x}", source_name=source.source_name,
            year=source.year, source_type=source.source_type,
        ) for index, source in enumerate(sources)]
        durations = {
            source.source_sha256: float(80 - index * 7)
            for index, source in enumerate(sources)
        }
        shards, totals = scene.greedy_duration_shards(sources, durations, 4)
        flattened = [item.source_sha256 for shard in shards for item in shard]
        self.assertEqual(8, len(flattened))
        self.assertEqual(8, len(set(flattened)))
        self.assertEqual({item.source_sha256 for item in sources}, set(flattened))
        self.assertLessEqual(max(totals) - min(totals), max(durations.values()))
        again, again_totals = scene.greedy_duration_shards(sources, durations, 4)
        self.assertEqual([[x.source_sha256 for x in group] for group in shards],
                         [[x.source_sha256 for x in group] for group in again])
        self.assertEqual(totals, again_totals)

    def test_merge_accepts_exact_sha_union_and_rejects_duplicate_or_missing(self) -> None:
        expected, paths = self._merge_fixture()
        output = self.root / "merged.json"
        merged = scene.merge_shards(paths, output, expected)
        self.assertEqual("complete", merged["status"])
        self.assertEqual(scene.SOURCE_COUNT, merged["source_count"])
        self.assertEqual(
            [item.source_sha256 for item in expected],
            [item["source_sha256"] for item in merged["sources"]])

        duplicate_documents = [json.loads(path.read_text()) for path in paths]
        duplicate_sha = duplicate_documents[0]["sources"][0]["source_sha256"]
        duplicate_documents[1]["sources"][0]["source_sha256"] = duplicate_sha
        duplicate_documents[1]["shard"]["assigned_source_sha256"][0] = duplicate_sha
        for path, document in zip(paths, duplicate_documents):
            path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(scene.SceneOCRError, "重复视频SHA"):
            scene.merge_shards(paths, output, expected)

        expected, paths = self._merge_fixture()
        missing = json.loads(paths[-1].read_text())
        missing["sources"].pop()
        missing["source_count"] -= 1
        missing["completed_source_count"] -= 1
        missing["shard"]["assigned_source_sha256"].pop()
        missing["shard"]["assigned_source_count"] -= 1
        paths[-1].write_text(json.dumps(missing), encoding="utf-8")
        with self.assertRaisesRegex(scene.SceneOCRError, "全集不一致"):
            scene.merge_shards(paths, output, expected)

    def test_merge_rejects_inconsistent_config(self) -> None:
        expected, paths = self._merge_fixture()
        document = json.loads(paths[-1].read_text())
        document["config"] = {"test": "different"}
        paths[-1].write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(scene.SceneOCRError, "config不一致"):
            scene.merge_shards(paths, self.root / "merged.json", expected)


if __name__ == "__main__":
    unittest.main()
