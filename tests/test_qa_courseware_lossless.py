"""Unit contracts for the read-only lossless courseware QA."""

from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from scripts import qa_courseware_lossless as qa


class CoursewareLosslessQATests(unittest.TestCase):
    def test_raw_line_validator_accepts_single_duplicate_and_low_confidence(self):
        lines = [
            {"index": 0, "text": "A", "confidence": 0.01,
             "box": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            {"index": 1, "text": "A", "confidence": 0.01,
             "box": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        ]
        self.assertEqual(qa.validate_raw_lines(lines), [])

    def test_raw_line_validator_rejects_malformed_fields_not_content(self):
        lines = [{"index": 4, "text": 7, "confidence": 2,
                  "box": [[0, 0], [1, 0]]}]
        errors = qa.validate_raw_lines(lines)
        self.assertTrue(any("index" in error for error in errors))
        self.assertTrue(any("text" in error for error in errors))
        self.assertTrue(any("confidence" in error for error in errors))
        self.assertTrue(any("四点框" in error for error in errors))

    def test_partition_requires_every_physical_page_and_contiguous_parts(self):
        valid = [
            {"page": 1, "part": 1, "part_count": 2},
            {"page": 1, "part": 2, "part_count": 2},
            {"page": 2, "part": 1, "part_count": 1},
        ]
        self.assertEqual(qa.validate_partition(valid, 2, allow_parts=True), [])
        self.assertTrue(qa.validate_partition(valid[:1], 2, allow_parts=True))
        self.assertTrue(qa.validate_partition(valid, 2, allow_parts=False))

    def test_visual_evidence_rehashes_image_and_keeps_native_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = np.zeros((20, 30, 3), np.uint8)
            success, encoded = cv2.imencode(".png", image)
            self.assertTrue(success)
            path = root / "page.png"
            path.write_bytes(encoded.tobytes())
            record = {
                "evidence_path": str(path), "image_sha256": qa.sha256_file(path),
                "raw_ocr_lines": [{
                    "index": 0, "text": "一", "confidence": 0,
                    "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                }],
                "native_lines": [{"text": "一"}],
            }
            self.assertEqual(qa.validate_visual_record(record, "fixture", root), [])
            record["image_sha256"] = "0" * 64
            self.assertTrue(any("SHA" in error
                                for error in qa.validate_visual_record(record, "fixture", root)))

    def test_manifest_control_includes_catalog_as_29th_input(self):
        manifest = {
            "catalog": {"source_path": "catalog.docx", "source_sha256": "c" * 64},
            "assets": [
                {"kind": "courseware", "source_path": f"{index}.pptx",
                 "source_sha256": f"{index:064x}"}
                for index in range(28)
            ] + [{"kind": "video", "source_path": "video.mp4", "source_sha256": "v" * 64}],
        }
        sources = qa.expected_manifest_sources(manifest)
        self.assertEqual(len(sources), 29)
        self.assertEqual(sources[0]["role"], "catalog_control")
        self.assertNotIn("video.mp4", [item["source_path"] for item in sources])

    def test_external_file_classifier_excludes_web_and_keeps_null_and_attachments(self):
        self.assertFalse(qa._file_like_external("https://example.com/a.pdf"))
        self.assertTrue(qa._file_like_external("附件/规范.docx"))
        self.assertTrue(qa._file_like_external("NULL"))

    def test_package_member_control_is_independent_and_byte_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fixture.docx"
            with qa.ZipFile(source, "w") as archive:
                archive.writestr("word/media/image1.png", b"one")
                archive.writestr("word/media/image2.jpeg", b"two")
                archive.writestr("word/document.xml", b"<document/>")
            facts = qa.source_embedded_member_facts(source)
            self.assertEqual(sorted(facts), [
                "word/media/image1.png", "word/media/image2.jpeg",
            ])
            self.assertEqual(facts["word/media/image1.png"]["size_bytes"], 3)
            self.assertEqual(
                facts["word/media/image2.jpeg"]["member_sha256"],
                qa.sha256_bytes(b"two"),
            )

    def test_xlsx_render_degradation_requires_native_cells_locator_and_exact_risk(self):
        obj = {
            "extension": ".xlsx",
            "member": "ppt/embeddings/Workbook1.xlsx",
            "source_pages": [28],
            "status": qa.XLSX_RENDER_DEGRADED_STATUS,
            "native_content": [{"name": "Sheet1", "cells": [
                {"coordinate": "A1", "value": "保密"},
            ]}],
            "raw_ocr_frames": [],
            "render_error": {"exception_type": "LosslessOCRError",
                             "message": "LibreOffice 渲染失败"},
        }
        risk = {
            "code": qa.XLSX_RENDER_DEGRADED_RISK,
            "member": obj["member"], "source_pages": [28],
            "exception_type": "LosslessOCRError",
            "exception_message": "LibreOffice 渲染失败",
            "coverage": "source_full_page_ocr",
        }
        self.assertEqual(qa.validate_xlsx_render_degradation(
            obj, [risk], source_extension=".pptx", physical_pages=30, label="fixture",
        ), [])
        variants = []
        missing_native = deepcopy(obj)
        missing_native["native_content"] = []
        variants.append((missing_native, [risk]))
        missing_page = deepcopy(obj)
        missing_page["source_pages"] = []
        variants.append((missing_page, [risk]))
        variants.append((deepcopy(obj), []))
        bad_risk = deepcopy(risk)
        bad_risk.pop("coverage")
        variants.append((deepcopy(obj), [bad_risk]))
        for record, risks in variants:
            with self.subTest(record=record, risks=risks):
                self.assertTrue(qa.validate_xlsx_render_degradation(
                    record, risks, source_extension=".pptx",
                    physical_pages=30, label="fixture",
                ))


if __name__ == "__main__":
    unittest.main()
