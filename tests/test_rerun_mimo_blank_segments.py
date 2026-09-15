"""Offline tests for auditable MiMo blank-segment patch generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from media_pipeline import asr
from scripts import rerun_mimo_blank_segments as rerun


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def audit(path: Path, reviews: list[dict], *, binding: str = "f" * 64) -> None:
    write_json(path, {"asr_corpus_sha256": binding, "reviews": reviews})


def make_context(root: Path, *, seconds: int = 50) -> tuple[dict, np.ndarray]:
    samples = np.full(seconds * asr.SAMPLE_RATE, 4000, dtype=np.int16)
    samples[16 * asr.SAMPLE_RATE:17 * asr.SAMPLE_RATE] = 0
    samples[33 * asr.SAMPLE_RATE:34 * asr.SAMPLE_RATE] = 0
    source = root / "source.wav"
    source.write_bytes(asr._wav_bytes(samples))
    source_sha = rerun.sha256_file(source)
    original_dir = root / "single" / source_sha[:16]
    original_dir.mkdir(parents=True)
    original_asr = original_dir / "asr.json"
    write_json(original_asr, {
        "model": asr.MODEL,
        "source_sha256": source_sha,
        "configuration": {"endpoint": rerun.ENDPOINT},
        "segments": [{
            "index": 0, "sample_start": 0, "sample_end": len(samples),
            "start_seconds": 0.0, "end_seconds": float(seconds), "text": "",
            "model": asr.MODEL, "finish_reason": "stop", "id": "old-id",
        }],
        "coverage": {"complete": True, "sample_count": len(samples),
                     "covered_sample_count": len(samples)},
        "audio": {"audio_start_seconds": 0.0},
        "normalized_audio_path": str(source),
    })
    item = {
        "review_id": f"{source_sha[:16]}:0:0-{len(samples)}",
        "source_sha256": source_sha,
        "source_path": str(source),
        "mimo_asr_path": str(original_asr),
        "mimo_segment_index": 0,
        "sample_start": 0,
        "sample_end": len(samples),
        "judgment": "speech_detected",
    }
    return item, samples


class BlankSegmentRerunTests(unittest.TestCase):
    def test_union_keeps_only_speech_detected_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sha_a, sha_b = "a" * 64, "b" * 64
            first = root / "sense.json"
            second = root / "whisper.json"
            audit(first, [
                {"source_sha256": sha_a, "sample_start": 0, "sample_end": 100,
                 "judgment": "speech_detected", "review_id": "a"},
                {"source_sha256": sha_b, "sample_start": 0, "sample_end": 100,
                 "judgment": "no_speech"},
            ])
            audit(second, [
                {"source_sha256": sha_a, "sample_start": 0, "sample_end": 100,
                 "judgment": "speech_detected", "review_id": "a2"},
                {"source_sha256": sha_b, "sample_start": 5, "sample_end": 105,
                 "judgment": "speech_detected", "review_id": "b"},
            ])
            tasks = rerun.load_speech_union([first, second])
            self.assertEqual(len(tasks), 2)
            duplicate = next(item for item in tasks if item["source_sha256"] == sha_a)
            self.assertEqual(len(duplicate["audit_sources"]), 2)

    def test_queue_binding_mismatch_refuses_old_review_before_any_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "sense.json", root / "whisper.json"
            audit(first, [], binding="a" * 64)
            audit(second, [], binding="b" * 64)
            with self.assertRaisesRegex(rerun.BlankRerunError, "绑定不一致"):
                rerun.build_patch(
                    first, second, root / "patch.json", root / "cache", None,
                    requester=lambda *_: self.fail("stale queues must not request"),
                )

    def test_quiet_segmentation_is_contiguous_and_uses_silence(self):
        samples = np.full(50 * asr.SAMPLE_RATE, 5000, dtype=np.int16)
        samples[16 * asr.SAMPLE_RATE:17 * asr.SAMPLE_RATE] = 0
        samples[33 * asr.SAMPLE_RATE:34 * asr.SAMPLE_RATE] = 0
        intervals = rerun.split_near_silence(samples, 0, len(samples))
        self.assertEqual(len(intervals), 3)
        self.assertEqual(intervals[0][0], 0)
        self.assertEqual(intervals[-1][1], len(samples))
        self.assertTrue(all(left[1] == right[0] for left, right in zip(intervals, intervals[1:])))
        self.assertTrue(all(12 <= (end - begin) / asr.SAMPLE_RATE <= 18
                            for begin, end in intervals))
        self.assertLess(abs(intervals[0][1] / asr.SAMPLE_RATE - 16.5), 0.3)
        self.assertLess(abs(intervals[1][1] / asr.SAMPLE_RATE - 33.5), 0.3)

    def test_build_patch_uses_fixed_endpoint_and_reuses_verified_child_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item, _ = make_context(root)
            sense, whisper = root / "sense.json", root / "whisper.json"
            audit(sense, [item])
            audit(whisper, [{**item, "review_id": "whisper-copy"}])
            calls = []

            def requester(samples, key, endpoint):
                calls.append((len(samples), key, endpoint))
                return {"model": asr.MODEL, "finish_reason": "stop",
                        "id": f"request-{len(calls)}", "text": f"正文{len(calls)}", "usage": {}}

            output, cache = root / "patch.json", root / "cache"
            first = rerun.build_patch(
                sense, whisper, output, cache, "secret",
                approved_review_ids={item["review_id"]}, requester=requester,
            )
            self.assertTrue(first["complete"])
            self.assertEqual(first["asr_corpus_sha256"], "f" * 64)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call[2] == rerun.ENDPOINT for call in calls))
            patch_item = first["patches"][0]
            self.assertEqual(patch_item["coverage"]["expected_sample_count"],
                             patch_item["coverage"]["covered_sample_count"])
            self.assertEqual(patch_item["new_mimo_text"], "正文1\n正文2\n正文3")
            self.assertTrue(all("request_id_digest" in child for child in patch_item["children"]))
            self.assertNotIn("secret", output.read_text(encoding="utf-8"))

            calls.clear()
            second = rerun.build_patch(
                sense, whisper, output, cache, "secret",
                approved_review_ids={item["review_id"]},
                requester=lambda *_: self.fail("verified cache should avoid requester"),
            )
            self.assertTrue(second["complete"])
            self.assertTrue(all(child["cached"] for child in second["patches"][0]["children"]))

    def test_request_failure_is_not_complete_or_cached_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item, _ = make_context(root)
            sense, whisper = root / "sense.json", root / "whisper.json"
            audit(sense, [item])
            audit(whisper, [])
            count = 0

            def requester(samples, key, endpoint):
                nonlocal count
                count += 1
                if count == 2:
                    raise asr.ASRError("network failed")
                return {"model": asr.MODEL, "finish_reason": "stop",
                        "id": f"request-{count}", "text": "正文", "usage": {}}

            result = rerun.build_patch(
                sense, whisper, root / "patch.json", root / "cache", "secret",
                approved_review_ids={item["review_id"]},
                requester=requester, progress=lambda _: None,
            )
            self.assertFalse(result["complete"])
            self.assertFalse(result["patches"][0]["complete"])
            self.assertEqual(result["patches"][0]["coverage"]["completed_child_count"], 2)
            self.assertEqual(len(list((root / "cache").rglob("*.json"))), 2)

    def test_source_sha_mismatch_stops_before_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item, _ = make_context(root)
            source = Path(item["source_path"])
            source.write_bytes(source.read_bytes() + b"changed")
            sense, whisper = root / "sense.json", root / "whisper.json"
            audit(sense, [item])
            audit(whisper, [])
            result = rerun.build_patch(
                sense, whisper, root / "patch.json", root / "cache", "secret",
                approved_review_ids={item["review_id"]},
                requester=lambda *_: self.fail("must not request with bad source SHA"),
                progress=lambda _: None,
            )
            self.assertFalse(result["complete"])
            self.assertEqual(result["patches"][0]["errors"][0]["code"],
                             "task_validation_failed")

    def test_empty_approval_only_writes_hallucination_candidates_and_never_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item, _ = make_context(root)
            sense, whisper = root / "sense.json", root / "whisper.json"
            audit(sense, [item])
            audit(whisper, [])
            result = rerun.build_patch(
                sense, whisper, root / "patch.json", root / "cache", None,
                approved_review_ids=set(),
                requester=lambda *_: self.fail("empty approval must never request API"),
            )
            self.assertTrue(result["complete"])
            self.assertFalse(result["execution_requested"])
            self.assertEqual(result["patches"], [])
            self.assertEqual(result["candidates"][0]["risk"], "local_hallucination_risk")
            self.assertEqual(result["candidates"][0]["action"], "keep_original_mimo_blank")

    def test_api_key_comes_only_from_environment_or_hidden_prompt(self):
        self.assertEqual(rerun.acquire_api_key(env={"MIMO_API_KEY": " key "}), "key")
        with patch.object(rerun.getpass, "getpass", return_value="prompt-key"):
            self.assertEqual(rerun.acquire_api_key(env={}, stdin_isatty=True), "prompt-key")
        with self.assertRaises(rerun.BlankRerunError):
            rerun.acquire_api_key(env={}, stdin_isatty=False)


if __name__ == "__main__":
    unittest.main()
