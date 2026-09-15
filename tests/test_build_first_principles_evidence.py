import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts import build_first_principles_evidence as evidence
from scripts.export_first_principles_word import load_bundle


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class EvidenceCorpusTests(unittest.TestCase):
    def completed_patch(self, source_sha, original, binding, *, local_marker=None):
        segment = original["segments"][0]
        start, end = segment["sample_start"], segment["sample_end"]
        middle = start + (end - start) // 2
        request_a, request_b = sha("patch-request-a"), sha("patch-request-b")
        review_id = f"{source_sha[:16]}:{segment['index']}:{start}-{end}"
        patch = {
            "review_id": review_id,
            "source_sha256": source_sha,
            "original_segment_index": segment["index"],
            "original_sample_start": start,
            "original_sample_end": end,
            "original_start_seconds": segment.get("start_seconds"),
            "original_end_seconds": segment.get("end_seconds"),
            "original_text": segment["text"],
            "new_mimo_text": "补识MiMo",
            "children": [
                {"index": 0, "sample_start": start, "sample_end": middle,
                 "sample_count": middle - start, "start_seconds": 0.0,
                 "end_seconds": 0.0003125, "duration_seconds": 0.0003125,
                 "text": "补识MiMo", "finish_reason": "stop",
                 "model": evidence.MIMO_MODEL, "request_count": 1,
                 "request_id_sha256": request_a,
                 "request_id_digest": request_a[:24],
                 "content_filter_split": False},
                {"index": 1, "sample_start": middle, "sample_end": end,
                 "sample_count": end - middle, "start_seconds": 0.0003125,
                 "end_seconds": 0.000625, "duration_seconds": 0.0003125,
                 "text": "", "finish_reason": "stop",
                 "model": evidence.MIMO_MODEL, "request_count": 1,
                 "request_id_sha256": request_b,
                 "request_id_digest": request_b[:24],
                 "content_filter_split": False},
            ],
            "coverage": {"complete": True, "expected_sample_count": end - start,
                         "covered_sample_count": end - start, "child_count": 2,
                         "completed_child_count": 2},
            "complete": True, "errors": [],
        }
        if local_marker is not None:
            patch["audit_sources"] = [{"auditor": "sensevoice", "text": local_marker}]
        return {
            "schema_version": "mimo-blank-segment-patch/v1",
            "complete": True, "endpoint": evidence.MIMO_ENDPOINT,
            "model": evidence.MIMO_MODEL, "asr_corpus_sha256": binding,
            "original_asr_files_modified": False, "errors": [],
            "approved_review_ids": [review_id],
            "selected_original_segment_count": 1,
            "completed_original_segment_count": 1,
            "execution_requested": True,
            "patches": [patch],
            "candidates": ([{"local_text": local_marker}] if local_marker else []),
        }

    def fixture(self, root: Path, *, incomplete: bool = False):
        video_sha, parent_sha, media_sha = sha("video"), sha("parent"), sha("media")
        video = root / "data/2025/video.mp4"
        parent = root / "data/2025/parent.pptx"
        media = root / ".work/embedded/clip.wmv"
        for path in (video, parent, media):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.name.encode())

        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "catalog": {"source_sha256": sha("catalog")},
            "assets": [
                {"asset_id": f"2025/video/{video_sha}", "source_sha256": video_sha,
                 "source_path": "data/2025/video.mp4", "source_name": "video.mp4",
                 "year": 2025, "kind": "video", "catalog_position": 1, "catalog_suborder": 1},
                {"asset_id": f"2025/courseware/{parent_sha}", "source_sha256": parent_sha,
                 "source_path": "data/2025/parent.pptx", "source_name": "parent.pptx",
                 "year": 2025, "kind": "courseware", "catalog_position": 2, "catalog_suborder": 1},
            ],
        }), encoding="utf-8")
        embedded = root / "embedded.json"
        embedded.write_text(json.dumps({"files": [{
            "parent_source": str(parent), "output_path": str(media), "sha256": media_sha,
            "slide": 3, "member": "ppt/media/media1.wmv",
        }]}), encoding="utf-8")

        work = root / ".work/single-video"
        base_image_sha = sha("same-physical-frame")
        asr_documents = {}
        for source_sha, line_text in ((video_sha, "重复文字"), (media_sha, "内嵌文字")):
            folder = work / source_sha[:16]
            folder.mkdir(parents=True)
            asr = {
                "model": evidence.MIMO_MODEL, "source_sha256": source_sha,
                "configuration": {"endpoint": evidence.MIMO_ENDPOINT},
                "segments": [
                    {"index": 0, "sample_start": 0, "sample_end": 10, "text": "原始MiMo"},
                    {"index": 1, "sample_start": 10, "sample_end": 20, "text": "原始MiMo"},
                ],
                "coverage": {"complete": True},
            }
            asr_documents[source_sha] = asr
            (folder / "asr.json").write_text(json.dumps(asr), encoding="utf-8")
            frames = [
                {"index": 0, "timestamp_seconds": 0.0, "actual_timestamp_seconds": 0.0,
                 "image_path": "frame0.jpg", "width": 10, "height": 10,
                 "lines": [{"text": line_text, "confidence": .9, "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}]},
                {"index": 1, "timestamp_seconds": 1.0, "actual_timestamp_seconds": 1.0,
                 "image_path": "frame1.jpg", "width": 10, "height": 10,
                 "lines": [{"text": line_text, "confidence": .8, "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}]},
            ]
            signature = sha("config-" + source_sha)
            ocr = {"engine": "RapidOCR", "expected_frame_count": 2, "frames": frames,
                   "cache_signature": signature, "coverage_verified": True,
                   "source_fingerprint": {"sha256": source_sha}}
            (folder / "ocr.json").write_text(json.dumps(ocr), encoding="utf-8")
            cache = folder / "ocr_cache" / signature
            cache.mkdir(parents=True)
            for index in range(2):
                (cache / f"frame_{index:06d}.json").write_text(json.dumps({
                    "signature": signature, "image_sha256": base_image_sha, "frame": frames[index],
                }), encoding="utf-8")

        courseware = root / "courseware.json"
        course_record = {
            "source_sha256": parent_sha, "processing_complete": not incomplete,
            "pages": [{"page": 1, "part": 1,
                       "raw_ocr_lines": [{"index": 0, "text": "页面OCR", "confidence": .7,
                                          "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
                       "native_lines": [{"index": 0, "text": "原生文字", "kind": "slide_text"}]}],
            "native_document_text": [],
            "embedded_objects": [{"member": "ppt/embeddings/book.xlsx",
                                  "native_content": [{"name": "Sheet1", "cells": [{"coordinate": "A1", "value": "表格字"}]}],
                                  "raw_ocr_frames": []}],
            "external_relationships": [], "errors": [], "risks": [],
        }
        courseware.write_text(json.dumps({
            "expected_file_count": 1, "selected_file_count": 0 if incomplete else 1,
            "processed_file_count": 0 if incomplete else 1,
            "processing_complete": not incomplete, "source_complete": not incomplete,
            "engine": {"name": "RapidOCR"}, "errors": [], "files": [course_record],
        }), encoding="utf-8")

        scene = root / "scene.json"
        if not incomplete:
            sources = []
            for source_sha in (video_sha, media_sha):
                sources.append({
                    "status": "complete", "source_sha256": source_sha,
                    "frames": [{"source_sha256": source_sha, "pts": 5,
                                "time_base": {"numerator": 1, "denominator": 10},
                                "timestamp_seconds": .5, "frame_image_sha256": base_image_sha,
                                "lines": [{"text": "场景文字", "confidence": .6,
                                           "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}],
                })
            scene.write_text(json.dumps({
                "status": "complete", "source_count": 2, "completed_source_count": 2,
                "failed_source_count": 0, "coverage_verified": True,
                "failures": [], "sources": sources,
            }), encoding="utf-8")

        blank = root / "blank.json"
        asr_binding = evidence.asr_corpus_sha256(asr_documents)
        if incomplete:
            reviews = [{"review_id": "r1", "judgment": "speech_detected"},
                       {"review_id": "m1", "judgment": "needs_manual"}]
            queue = [{"review_id": "r1", "judgment": "speech_detected"}]
            needs_manual = 1
        else:
            # Local uncertainty is not an ASR blocker after an independent
            # adjudication approves no re-run.
            reviews = [{"review_id": "m1", "judgment": "needs_manual"}]
            queue, needs_manual = [], 1
        blank.write_text(json.dumps({
            "asr_corpus_sha256": asr_binding,
            "summary": {"judgments": {"needs_manual": needs_manual}},
            "reviews": reviews, "mimo_resegmentation_rerun_queue": queue,
        }), encoding="utf-8")
        adjudication = root / "adjudication.json"
        approved_ids = ["r1"] if incomplete else []
        adjudication.write_text(json.dumps({
            "complete": not incomplete,
            "endpoint": evidence.MIMO_ENDPOINT,
            "model": evidence.MIMO_MODEL,
            "asr_corpus_sha256": asr_binding,
            "approved_review_ids": approved_ids,
            "selected_original_segment_count": len(approved_ids),
            "completed_original_segment_count": 0,
            "execution_requested": bool(approved_ids),
            "patches": [], "errors": [], "original_asr_files_modified": False,
        }), encoding="utf-8")
        return manifest, embedded, work, courseware, scene, blank, adjudication

    def build(self, root: Path, *, incomplete: bool, output: Path | None):
        manifest, embedded, work, courseware, scene, blank, adjudication = self.fixture(
            root, incomplete=incomplete)
        return evidence.build_corpus(
            root=root, manifest_path=manifest, asr_ocr_root=work,
            embedded_manifest_path=embedded, scene_path=scene,
            courseware_path=courseware, blank_review_path=blank,
            blank_adjudication_path=adjudication,
            output_path=output, allow_incomplete=incomplete,
            expected_assets=2, expected_courseware_inputs=1,
            expected_video_sources=2, expected_no_audio=0,
        )

    def test_complete_corpus_preserves_all_evidence_and_parent_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            corpus = self.build(root, incomplete=False, output=None)
            self.assertEqual(corpus["status"], "complete")
            self.assertEqual(corpus["file_count"], 2)
            video_file = corpus["files"][0]
            video = video_file["raw_evidence"]
            self.assertEqual([s["text"] for s in video["asr_result"]["result"]["segments"]],
                             ["原始MiMo", "原始MiMo"])
            self.assertEqual(len(video["ocr_result"]["frames"]), 2)
            self.assertEqual([f["lines"][0]["text"] for f in video["ocr_result"]["frames"]],
                             ["重复文字", "重复文字"])
            self.assertEqual(video["ocr_result"]["supplemental_physical_duplicates_merged"], 1)
            parent_file = corpus["files"][1]
            parent = parent_file["raw_evidence"]
            self.assertEqual(len(parent["asr_result"]["embedded_media"]), 1)
            self.assertEqual(len(parent["ocr_result"]["embedded_media"]), 1)
            record = parent["ocr_result"]["courseware_record"]
            self.assertEqual(record["pages"][0]["raw_ocr_lines"][0]["text"], "页面OCR")
            self.assertEqual(record["pages"][0]["native_lines"][0]["text"], "原生文字")
            self.assertEqual(record["embedded_objects"][0]["native_content"][0]["cells"][0]["value"], "表格字")
            for item in corpus["files"]:
                self.assertEqual(evidence.canonical_sha256(item["raw_evidence"]),
                                 item["evidence_sha256"])
                self.assertEqual(set(item["sections"]), {"ASR 结果", "OCR 结果"})

    def test_strict_refuses_and_allow_incomplete_is_debug_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.fixture(root, incomplete=True)
            output = root / "evidence.json"
            with self.assertRaises(evidence.IncompleteEvidenceError):
                evidence.build_corpus(
                    root=root, manifest_path=args[0], embedded_manifest_path=args[1],
                    asr_ocr_root=args[2], courseware_path=args[3], scene_path=args[4],
                    blank_review_path=args[5], blank_adjudication_path=args[6],
                    output_path=output, allow_incomplete=False,
                    expected_assets=2, expected_courseware_inputs=1,
                    expected_video_sources=2, expected_no_audio=0,
                )
            self.assertFalse(output.exists())
            corpus = evidence.build_corpus(
                root=root, manifest_path=args[0], embedded_manifest_path=args[1],
                asr_ocr_root=args[2], courseware_path=args[3], scene_path=args[4],
                blank_review_path=args[5], blank_adjudication_path=args[6],
                output_path=output, allow_incomplete=True,
                expected_assets=2, expected_courseware_inputs=1,
                expected_video_sources=2, expected_no_audio=0,
            )
            self.assertEqual(corpus["status"], "incomplete")
            self.assertEqual(corpus["artifact_role"], "debug_only_not_for_final_fusion")
            self.assertEqual({b["code"] for b in corpus["blockers"]}, {
                "courseware_ocr_incomplete", "video_scene_ocr_incomplete",
                "blank_asr_adjudication_incomplete", "blank_asr_approved_mimo_rerun_pending",
            })
            self.assertEqual(len(json.loads(output.read_text())["files"]), 2)

    def test_known_missing_external_assets_are_explicit_risks_not_ocr_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.fixture(root, incomplete=False)
            document = json.loads(args[3].read_text(encoding="utf-8"))
            document["source_complete"] = False
            document["risks"] = [{
                "code": "missing_external_asset",
                "source": str(root / "data/2025/parent.pptx"),
                "relationship_id": "rId9",
                "target": "missing.xlsx",
            }]
            args[3].write_text(json.dumps(document), encoding="utf-8")
            corpus = evidence.build_corpus(
                root=root, manifest_path=args[0], embedded_manifest_path=args[1],
                asr_ocr_root=args[2], courseware_path=args[3], scene_path=args[4],
                blank_review_path=args[5], blank_adjudication_path=args[6],
                output_path=None, allow_incomplete=False,
                expected_assets=2, expected_courseware_inputs=1,
                expected_video_sources=2, expected_no_audio=0,
            )
            self.assertEqual(corpus["status"], "complete")
            self.assertFalse(corpus["readiness"]["courseware"]["source_complete"])
            self.assertTrue(corpus["readiness"]["courseware"]
                            ["missing_external_asset_risk_recorded"])

    def test_generated_prose_fields_are_rejected_from_every_evidence_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = self.fixture(root, incomplete=False)
            courseware_original = json.loads(base[3].read_text(encoding="utf-8"))
            first_asr = next(base[2].glob("*/asr.json"))
            asr_original = json.loads(first_asr.read_text(encoding="utf-8"))
            variants = (
                ("manifest paragraphs", base[0],
                 json.loads(base[0].read_text(encoding="utf-8")), ("paragraphs",)),
                ("courseware fused_content", base[3], courseware_original,
                 ("files", 0, "fused_content")),
                ("courseware paragraphs", base[3], courseware_original,
                 ("files", 0, "paragraphs")),
                ("ASR fusion_text", first_asr, asr_original, ("fusion_text",)),
            )
            for label, target, original, path in variants:
                with self.subTest(label=label):
                    polluted = deepcopy(original)
                    cursor = polluted
                    for part in path[:-1]:
                        cursor = cursor[part]
                    cursor[path[-1]] = "旧融合正文"
                    target.write_text(json.dumps(polluted), encoding="utf-8")
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.build_corpus(
                            root=root, manifest_path=base[0], embedded_manifest_path=base[1],
                            asr_ocr_root=base[2], courseware_path=base[3], scene_path=base[4],
                            blank_review_path=base[5], blank_adjudication_path=base[6],
                            output_path=None, allow_incomplete=False,
                            expected_assets=2, expected_courseware_inputs=1,
                            expected_video_sources=2, expected_no_audio=0,
                        )
                    target.write_text(json.dumps(original), encoding="utf-8")

    def test_audio_requires_token_plan_endpoint_but_no_audio_remains_not_applicable(self):
        source_sha = sha("endpoint-fixture")
        audio = {
            "model": evidence.MIMO_MODEL,
            "source_sha256": source_sha,
            "segments": [{"sample_start": 0, "sample_end": 10, "text": "正文"}],
            "coverage": {"complete": True},
            "configuration": {"endpoint": "https://api.xiaomimimo.com/v1/chat/completions"},
        }
        with self.assertRaisesRegex(evidence.EvidenceError, "token-plan-cn"):
            evidence.mimo_result(audio, source_sha)
        no_audio = {
            "model": evidence.MIMO_MODEL,
            "source_sha256": source_sha,
            "has_audio": False,
            "skipped_reason": "no_audio_stream",
            "segments": [],
            "coverage": {"complete": True},
        }
        self.assertEqual(evidence.mimo_result(no_audio, source_sha)["status"], "not_applicable")

    def test_changed_asr_content_invalidates_old_review_and_patch_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.fixture(root, incomplete=False)
            path = next(args[2].glob("*/asr.json"))
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["segments"][0]["text"] = "本轮重跑后的不同文本"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(evidence.IncompleteEvidenceError) as caught:
                evidence.build_corpus(
                    root=root, manifest_path=args[0], embedded_manifest_path=args[1],
                    asr_ocr_root=args[2], courseware_path=args[3], scene_path=args[4],
                    blank_review_path=args[5], blank_adjudication_path=args[6],
                    output_path=None, allow_incomplete=False,
                    expected_assets=2, expected_courseware_inputs=1,
                    expected_video_sources=2, expected_no_audio=0,
                )
            self.assertIn("blank_asr_review_stale",
                          {item["code"] for item in caught.exception.blockers})
            self.assertIn("blank_asr_adjudication_incomplete",
                          {item["code"] for item in caught.exception.blockers})

    def test_completed_patch_becomes_effective_mimo_segments_without_local_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.fixture(root, incomplete=False)
            manifest = json.loads(args[0].read_text(encoding="utf-8"))
            source_sha = manifest["assets"][0]["source_sha256"]
            asr_path = args[2] / source_sha[:16] / "asr.json"
            original = json.loads(asr_path.read_text(encoding="utf-8"))
            original["segments"][0]["text"] = ""
            asr_path.write_text(json.dumps(original), encoding="utf-8")
            immutable_bytes = asr_path.read_bytes()
            documents = {}
            for path in args[2].glob("*/asr.json"):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(path.parent.name, value["source_sha256"][:16])
                documents[value["source_sha256"]] = value
            binding = evidence.asr_corpus_sha256(documents)
            review_id = f"{source_sha[:16]}:0:0-10"
            blank = json.loads(args[5].read_text(encoding="utf-8"))
            blank.update({
                "asr_corpus_sha256": binding,
                "summary": {"judgments": {"needs_manual": 0, "speech_detected": 1}},
                "reviews": [{"review_id": review_id, "judgment": "speech_detected"}],
                "mimo_resegmentation_rerun_queue": [
                    {"review_id": review_id, "judgment": "speech_detected"},
                ],
            })
            args[5].write_text(json.dumps(blank), encoding="utf-8")
            marker = "禁止进入正式ASR的SenseVoice文本"
            patch = self.completed_patch(source_sha, original, binding, local_marker=marker)
            args[6].write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")

            corpus = evidence.build_corpus(
                root=root, manifest_path=args[0], embedded_manifest_path=args[1],
                asr_ocr_root=args[2], courseware_path=args[3], scene_path=args[4],
                blank_review_path=args[5], blank_adjudication_path=args[6],
                output_path=None, allow_incomplete=False,
                expected_assets=2, expected_courseware_inputs=1,
                expected_video_sources=2, expected_no_audio=0,
            )
            item = corpus["files"][0]
            asr_result = item["raw_evidence"]["asr_result"]
            self.assertEqual(item["sections"]["ASR 结果"], ["补识MiMo", "原始MiMo"])
            self.assertEqual([segment["evidence_origin"]
                              for segment in asr_result["result"]["segments"]], [
                                  "approved_mimo_blank_segment_patch",
                                  "approved_mimo_blank_segment_patch",
                                  "original_mimo_asr",
                              ])
            self.assertEqual(asr_result["traceability"]["original_segments"][0]["text"], "")
            self.assertEqual(asr_result["traceability"]["applied_mimo_patches"][0]
                             ["new_mimo_text"], "补识MiMo")
            self.assertNotIn(marker, json.dumps(asr_result, ensure_ascii=False))
            self.assertEqual(asr_path.read_bytes(), immutable_bytes)
            self.assertEqual(corpus["readiness"]["blank_asr_adjudication"]
                             ["approved_mimo_rerun_pending"], 0)

    def test_completed_patch_rejects_original_mismatch_gap_and_non_mimo_child(self):
        source_sha = sha("patch-source")
        original = {
            "source_sha256": source_sha,
            "segments": [{"index": 0, "sample_start": 0, "sample_end": 10, "text": ""}],
        }
        documents = {source_sha: original}
        binding = evidence.asr_corpus_sha256(documents)
        base = self.completed_patch(source_sha, original, binding)
        variants = []
        wrong_text = deepcopy(base)
        wrong_text["patches"][0]["original_text"] = "不匹配"
        variants.append(wrong_text)
        gap = deepcopy(base)
        gap["patches"][0]["children"][1]["sample_start"] += 1
        gap["patches"][0]["children"][1]["sample_count"] -= 1
        variants.append(gap)
        wrong_model = deepcopy(base)
        wrong_model["patches"][0]["children"][0]["model"] = "local-whisper"
        variants.append(wrong_model)
        for document in variants:
            with self.subTest(document=document), self.assertRaises(evidence.EvidenceError):
                evidence.validate_completed_mimo_patches(document, documents, binding)

    def test_exporter_load_bundle_accepts_flattened_schema_with_fake_gpt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            corpus = self.build(root, incomplete=False, output=None)
            manifest_assets, evidence_files, gpt_files = [], [], []
            for index in range(1, 92):
                if index <= 2:
                    built = corpus["files"][index - 1]
                    asset_id, source_sha = built["asset_id"], built["source_sha256"]
                    evidence_item = built
                else:
                    source_sha = f"{index:064x}"
                    asset_id = f"fixture-{index:03d}"
                    evidence_item = {"asset_id": asset_id, "source_sha256": source_sha,
                                     "evidence_sha256": f"{index + 1000:064x}",
                                     "sections": {"ASR 结果": ["不适用"], "OCR 结果": ["测试OCR"]}}
                position = min(index, 81)
                manifest_assets.append({
                    "asset_id": asset_id, "source_sha256": source_sha,
                    "source_name": f"文件{index}.mp4", "year": 2025,
                    "kind": "video", "catalog_position": position,
                    "catalog_suborder": index - 80 if position == 81 else 1,
                    "coverage_positions": [position],
                })
                evidence_files.append(evidence_item)
                gpt_files.append({"asset_id": asset_id, "source_sha256": source_sha,
                                  "evidence_sha256": evidence_item["evidence_sha256"],
                                  "task_sha256": f"{index + 2000:064x}",
                                  "paragraphs": [f"最终正文{index}"]})
            manifest_path, corpus_path, gpt_path = root / "m.json", root / "e.json", root / "g.json"
            manifest_path.write_text(json.dumps({
                "asset_count": 91,
                "catalog": {"entry_count": 81, "entries": [f"目录{i}" for i in range(1, 82)]},
                "assets": manifest_assets,
            }, ensure_ascii=False), encoding="utf-8")
            corpus_path.write_text(json.dumps({"file_count": 91, "files": evidence_files},
                                              ensure_ascii=False), encoding="utf-8")
            gpt_path.write_text(json.dumps({"model": "fake-test-model", "file_count": 91,
                                           "files": gpt_files}, ensure_ascii=False), encoding="utf-8")
            bundle = load_bundle(manifest_path, corpus_path, gpt_path)
            self.assertEqual(bundle["asset_count"], 91)
            self.assertEqual(bundle["assets"][0]["sections"]["ASR 结果"], ["原始MiMo", "原始MiMo"])


if __name__ == "__main__":
    unittest.main()
