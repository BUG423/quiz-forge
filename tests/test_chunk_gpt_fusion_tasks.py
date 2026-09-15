import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts import chunk_gpt_fusion_tasks as chunker


class ChunkGPTFusionTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tasks_dir = self.root / "tasks"
        self.tasks_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_task(self):
        sha = "1" * 64
        evidence_sha = "2" * 64
        final_review = "必须汇总全部分块，并在文件级最终复核后才能输出最终正文。"
        return {
            "asset_id": "asset-1",
            "source_name": "课程.mp4",
            "source_sha256": sha,
            "evidence_sha256": evidence_sha,
            "ASR 结果": {
                "status": "complete",
                "result": {"segments": [
                    {"index": 0, "sample_start": 0, "sample_end": 10, "text": "语音重复"},
                    {"index": 1, "sample_start": 10, "sample_end": 20, "text": "语音重复"},
                    {"index": 2, "sample_start": 20, "sample_end": 30, "text": ""},
                ]},
            },
            "OCR 结果": {
                "status": "complete", "source_type": "video", "frames": [
                    {"index": 0, "timestamp_seconds": 0.0, "frame_source": "whole_second",
                     "lines": [{"text": "标题", "confidence": 0.01, "box": [0]}]},
                    {"index": 1, "timestamp_seconds": 1.0, "frame_source": "whole_second",
                     "lines": [{"text": "标题", "confidence": 0.99, "box": [1]}]},
                    {"index": 2, "timestamp_seconds": 2.0, "frame_source": "whole_second",
                     "lines": [{"text": "标题 ", "confidence": 0.99, "box": [2]}]},
                    {"index": 3, "timestamp_seconds": 3.0, "frame_source": "whole_second",
                     "lines": [{"text": "别的内容", "confidence": 0.5, "box": [3]}]},
                    {"index": 4, "timestamp_seconds": 4.0, "frame_source": "whole_second",
                     "lines": [{"text": "标题", "confidence": 0.7, "box": [4]}]},
                ],
            },
            "task_constraints": {"final_review": final_review},
        }

    def write_bundle(self, task=None, *, mutate_entry=None):
        task = self.make_task() if task is None else task
        filename = "0001-task.json"
        (self.tasks_dir / filename).write_text(
            json.dumps(task, ensure_ascii=False), encoding="utf-8",
        )
        entry = {
            "order": 1, "catalog_position": 1, "catalog_suborder": 1,
            "asset_id": task["asset_id"], "source_name": task["source_name"],
            "source_sha256": task["source_sha256"],
            "evidence_sha256": task["evidence_sha256"],
            "task_file": filename, "task_sha256": chunker.canonical_sha256(task),
        }
        if mutate_entry:
            mutate_entry(entry)
        index = {"schema_version": "gpt-fusion-task-index/v1", "status": "ready",
                 "file_count": 1, "tasks": [entry]}
        (self.tasks_dir / "index.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8",
        )
        return task, index

    def test_exact_rle_and_full_stream_round_trip(self):
        task, _ = self.write_bundle()
        output = self.root / "chunks"
        result = chunker.build_reading_chunks(
            input_dir=self.tasks_dir, output_dir=output, expected_files=1,
            min_chars=5, target_chars=10, max_chars=15,
        )
        events = chunker.extract_text_events(task)
        payloads = [json.loads((output / item["chunk_file"]).read_text())
                    for item in result["index"]["assets"][0]["chunks"]]
        records = [record for payload in payloads for record in payload["records"]]
        expanded = chunker.expand_records(records)
        self.assertEqual(expanded, events)
        self.assertEqual(chunker.event_texts(expanded), [
            "语音重复", "语音重复", "", "标题", "标题", "标题 ", "别的内容", "标题",
        ])
        self.assertEqual(result["index"]["assets"][0]["text_stream_sha256"],
                         chunker.canonical_sha256(chunker.event_texts(events)))

        runs = [record for record in records if record["record_type"] == "video_frame_run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["frame_count"], 2)
        self.assertEqual(runs[0]["time_range_seconds"], {"first": 0.0, "last": 1.0})
        self.assertEqual([f["locator"]["timestamp_seconds"] for f in runs[0]["frames"]],
                         [0.0, 1.0])
        # Confidence changed radically but was never used for filtering; both
        # physical line metadata records survive expansion.
        self.assertEqual([f["metadata"]["lines"][0]["confidence"] for f in runs[0]["frames"]],
                         [0.01, 0.99])
        self.assertNotEqual(events[5]["texts"], events[3]["texts"])  # trailing space is material
        self.assertEqual(sum(record["frame_count"] for record in runs), 2)

        for payload in payloads:
            stored_sha = payload.pop("chunk_sha256")
            self.assertEqual(stored_sha, chunker.canonical_sha256(payload))
            self.assertTrue(payload["provisional_only"])
            self.assertEqual(payload["file_level_final_review"],
                             task["task_constraints"]["final_review"])
            self.assertNotIn("paragraphs", json.dumps(payload, ensure_ascii=False))

    def test_courseware_pages_attachments_and_embedded_media_keep_source_order(self):
        task = self.make_task()
        task["source_name"] = "课件.pptx"
        task["ASR 结果"] = {
            "status": "embedded_media_only", "embedded_media": [{
                "source_sha256": "3" * 64, "slide": 2, "member": "media/a.mp4",
                "asr_result": {"result": {"segments": [
                    {"index": 0, "text": "内嵌语音"}, {"index": 1, "text": "内嵌语音"},
                ]}},
            }],
        }
        task["OCR 结果"] = {
            "source_type": "courseware", "courseware_record": {
                "pages": [
                    {"page": 1, "part": 1,
                     "raw_ocr_lines": [{"text": "页面OCR", "confidence": 0.02, "box": []}],
                     "native_lines": [{"index": 0, "kind": "shape", "text": "页面原生"}]},
                    {"page": 2, "part": 1,
                     "raw_ocr_lines": [{"text": "重复", "confidence": 1.0, "box": []}],
                     "native_lines": [{"index": 0, "text": "重复"}]},
                ],
                "native_document_text": [{"index": 0, "kind": "paragraph", "text": "文档原生"}],
                "embedded_objects": [{
                    "member": "embeddings/book.xlsx", "status": "complete",
                    "raw_ocr_frames": [{"page": 2, "part": 1,
                                        "raw_ocr_lines": [{"text": "附件OCR", "box": []}],
                                        "native_lines": []}],
                    "native_content": [{"name": "Sheet1", "cells": [
                        {"coordinate": "A1", "value": "单元格"},
                        {"coordinate": "A2", "value": "单元格"},
                    ]}],
                }],
            },
            "embedded_media": [{
                "source_sha256": "3" * 64, "slide": 2, "member": "media/a.mp4",
                "ocr_result": {"frames": [
                    {"index": 0, "timestamp_seconds": 0, "lines": [{"text": "媒体画面", "box": []}]},
                ]},
            }],
        }
        self.write_bundle(task)
        result = chunker.build_reading_chunks(
            input_dir=self.tasks_dir, output_dir=None, expected_files=1,
            min_chars=8, target_chars=12, max_chars=20,
        )
        records = [record for _, payload in result["chunk_files"] for record in payload["records"]]
        events = chunker.expand_records(records)
        self.assertEqual(chunker.event_texts(events), [
            "内嵌语音", "内嵌语音", "页面OCR", "页面原生", "重复", "重复",
            "文档原生", "附件OCR", "Sheet1", "单元格", "单元格", "媒体画面",
        ])
        self.assertEqual([event["record_kind"] for event in events], [
            "asr_segment", "asr_segment", "ocr_courseware_page", "ocr_courseware_page",
            "ocr_native_document_item", "ocr_attachment_frame",
            "ocr_attachment_native_content", "ocr_video_frame",
        ])

    def test_large_frame_run_can_cross_chunks_and_remains_reversible(self):
        task = self.make_task()
        task["ASR 结果"]["result"]["segments"] = []
        text = "甲乙丙丁" * 4
        task["OCR 结果"]["frames"] = [
            {"index": i, "timestamp_seconds": float(i),
             "lines": [{"text": text, "confidence": i / 10, "box": [i]}]}
            for i in range(10)
        ]
        self.write_bundle(task)
        result = chunker.build_reading_chunks(
            input_dir=self.tasks_dir, output_dir=None, expected_files=1,
            min_chars=32, target_chars=48, max_chars=64,
        )
        self.assertGreater(result["index"]["chunk_count"], 1)
        records = [record for _, payload in result["chunk_files"] for record in payload["records"]]
        self.assertEqual(chunker.expand_records(records), chunker.extract_text_events(task))
        self.assertEqual(sum(chunker.record_character_count(record) for record in records),
                         len(text) * 10)

    def test_broken_binding_generated_field_and_existing_output_are_refused(self):
        _, _ = self.write_bundle(mutate_entry=lambda item: item.update(task_sha256="0" * 64))
        with self.assertRaises(chunker.ChunkingError):
            chunker.build_reading_chunks(
                input_dir=self.tasks_dir, output_dir=self.root / "bad", expected_files=1,
            )
        self.assertFalse((self.root / "bad").exists())

        # Restore a valid bundle, then add a generated-body field.  Exact task
        # schema validation rejects it before any output directory is created.
        task = self.make_task()
        task["paragraphs"] = ["不应进入分块器的旧正文"]
        self.write_bundle(task)
        with self.assertRaises(chunker.ChunkingError):
            chunker.build_reading_chunks(
                input_dir=self.tasks_dir, output_dir=self.root / "bad2", expected_files=1,
            )
        self.assertFalse((self.root / "bad2").exists())

        valid = self.make_task()
        self.write_bundle(valid)
        occupied = self.root / "occupied"
        occupied.mkdir()
        marker = occupied / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(chunker.ChunkingError):
            chunker.build_reading_chunks(
                input_dir=self.tasks_dir, output_dir=occupied, expected_files=1,
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
