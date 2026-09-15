"""Concise three-section delivery must retain independently traceable evidence."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from docx import Document

from media_pipeline.fusion import (
    SECTION_TITLES, export_clean_word, representative_frames, verify_evidence,
)


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"sha256": "a" * 64, "source_name": "测试视频.mp4"}
        self.asr = {"segments": [{"index": 0, "text": "施工。"}]}
        self.ocr = {"frames": [
            {"index": i, "timestamp_seconds": i, "image_path": f"frame_{i}.jpg",
             "lines": [{"text": text, "confidence": .9}] if text else []}
            for i, text in enumerate(["项目经理曾雷", "项目经理曾雷", "", "项目经理曾雷"])
        ]}
        self.vision = {"model": "mimo-v2.5", "source_sha256": "a" * 64,
                       "frames": [{"frame_index": 0}]}
        self.content = {"source_sha256": "a" * 64, "title": "测试视频", "blocks": [
            {"type": "heading", "text": SECTION_TITLES[0]},
            {"type": "paragraph", "text": "施工。", "evidence": {"asr_segments": [0]}},
            {"type": "heading", "text": SECTION_TITLES[1]},
            {"type": "paragraph", "text": "项目经理曾雷。", "evidence": {"ocr_frames": [0]}},
            {"type": "heading", "text": SECTION_TITLES[2]},
            {"type": "paragraph", "text": "项目经理曾雷参与施工。", "evidence": {
                "asr_segments": [0], "ocr_frames": [0], "vision_frames": [0],
                "visual_only_information": True}},
        ]}

    def verify(self):
        verify_evidence(self.content, self.manifest, self.asr, self.ocr, self.vision)

    def test_valid_three_part_content(self):
        self.verify()

    def test_sections_must_be_exact_and_in_order(self):
        self.content["blocks"][0]["text"] = SECTION_TITLES[1]
        with self.assertRaisesRegex(ValueError, "三个结果"):
            self.verify()

    def test_each_section_must_have_actual_content(self):
        del self.content["blocks"][1]
        with self.assertRaisesRegex(ValueError, "均不能为空"):
            self.verify()

    def test_visual_only_fact_must_appear_in_fusion_not_just_ocr(self):
        self.content["blocks"][3]["evidence"].update(
            vision_frames=[0], visual_only_information=True)
        self.content["blocks"][5]["evidence"].pop("visual_only_information")
        with self.assertRaisesRegex(ValueError, "单独ASR"):
            self.verify()

    def test_source_fingerprint_must_match(self):
        self.content["source_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "指纹"):
            self.verify()

    def test_vision_must_match_actual_source(self):
        self.vision["source_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "视觉复核"):
            self.verify()

    def test_visual_evidence_cannot_cite_unreviewed_frame(self):
        self.content["blocks"][5]["evidence"]["vision_frames"] = [999]
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.verify()

    def test_visual_evidence_must_corroborate_same_frame(self):
        self.content["blocks"][5]["evidence"]["ocr_frames"] = [1]
        with self.assertRaisesRegex(ValueError, "原图"):
            self.verify()

    def test_asr_section_cannot_contain_picture_only_facts(self):
        self.content["blocks"][1]["evidence"] = {"ocr_frames": [0]}
        with self.assertRaisesRegex(ValueError, "纯视觉"):
            self.verify()

    def test_ocr_section_cannot_contain_audio_only_facts(self):
        self.content["blocks"][3]["evidence"] = {"asr_segments": [0]}
        with self.assertRaisesRegex(ValueError, "纯语音"):
            self.verify()

    def test_representatives_preserve_reappearance_and_do_not_mutate(self):
        original = copy.deepcopy(self.ocr)
        frames, groups = representative_frames(self.asr, self.ocr, {})
        self.assertEqual([f["index"] for f in frames], [0, 3])
        self.assertEqual([g["covered_frames"] for g in groups], [[0, 1], [3]])
        self.assertEqual(self.ocr, original)

    def test_visual_supplement_in_blank_scene_is_also_reviewed(self):
        frames, _ = representative_frames(self.asr, self.ocr, {
            "ocr_supplements": [{"start_seconds": 2, "end_seconds": 2}]})
        self.assertEqual([f["index"] for f in frames], [0, 2, 3])

    def test_word_does_not_export_internal_evidence_or_notes(self):
        self.content["internal_notes"] = "00:03 frame_999.jpg request_id secret"
        self.content["blocks"][5]["evidence"]["notes"] = "校对过程"
        with tempfile.TemporaryDirectory() as tmp:
            result = export_clean_word(self.content, Path(tmp) / "test.docx")
            doc = Document(result)
            paragraphs = [p.text for p in doc.paragraphs]
            self.assertEqual([p for p in paragraphs if p in SECTION_TITLES], list(SECTION_TITLES))
            combined = "\n".join(paragraphs)
            for private in ["frame_", "00:03", "request_id", "secret", "校对过程", "asr_segments"]:
                self.assertNotIn(private, combined)


if __name__ == "__main__":
    unittest.main()
