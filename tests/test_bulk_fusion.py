import json
from pathlib import Path
import tempfile
import unittest

from media_pipeline.bulk_fusion import (
    _courseware_fusion_lines,
    _courseware_pages,
    _bottom_subtitle_transcript,
    _strong_rapid_lines,
    _visual_supplements,
    apply_full_fusion_review,
    build_video,
    build_courseware,
    dedupe_lines,
    load_manual_overrides,
    paragraphize,
    similar,
)


class BulkFusionTests(unittest.TestCase):
    def test_deduplicates_repeated_and_near_identical_ocr(self):
        values = ["[课件/slide_content] 安全生产责任制", "安全生产责任制",
                  "安全 生产 责任制", "风险分级管控"]
        self.assertEqual(dedupe_lines(values, "安全课"),
                         ["安全生产责任制", "风险分级管控"])

    def test_similarity_requires_real_text(self):
        self.assertTrue(similar("中国南方电网", "中国 南方电网"))
        self.assertFalse(similar("", "中国南方电网"))

    def test_paragraphize_preserves_all_text(self):
        paragraphs = paragraphize("第一句。第二句。第三句。", "", maximum=8)
        self.assertEqual("".join(paragraphs), "第一句。第二句。第三句。")
        self.assertGreater(len(paragraphs), 1)

    def test_supplemental_reviewed_drafts_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "manual_fusion_overrides.json"
            primary.write_text(json.dumps({"甲.mp4": ["甲"]}), encoding="utf-8")
            (root / "fusion-draft-a.json").write_text(
                json.dumps({"乙.mp4": ["乙"]}), encoding="utf-8")
            self.assertEqual(load_manual_overrides(primary),
                             {"甲.mp4": ["甲"], "乙.mp4": ["乙"]})

    def test_full_review_replaces_every_provisional_fusion(self):
        files = [{
            "source_name": "课程.mp4", "source_sha256": "a" * 64,
            "sections": {"GPT 融合校对结果": ["摘要"]},
        }]
        review = {
            "model": "mimo-v2.5-pro",
            "files": [{
                "source_name": "课程.mp4", "source_sha256": "a" * 64,
                "sections": {"GPT 融合校对结果": ["无损全文校对结果"]},
            }],
        }
        apply_full_fusion_review(files, review)
        self.assertEqual(files[0]["sections"]["GPT 融合校对结果"],
                         ["无损全文校对结果"])
        self.assertEqual(files[0]["evidence_status"]["fusion_text_source"],
                         "mimo-v2.5-pro-lossless-full-review")

    def test_gpt_review_copies_verified_ocr_noise_evidence(self):
        files = [{
            "source_name": "课程.mp4", "source_sha256": "a" * 64,
            "sections": {"GPT 融合校对结果": ["摘要"]},
        }]
        exclusions = [{"kind": "time", "canonical": "7年",
                       "reason": "malformed_duplicate"}]
        review = {
            "model": "gpt-lossless-full-review",
            "files": [{
                "source_name": "课程.mp4", "source_sha256": "a" * 64,
                "evidence_status": {
                    "fusion_text_source": "gpt-lossless-full-review",
                    "verified_ocr_noise_anchors": exclusions,
                },
                "sections": {"GPT 融合校对结果": ["完整正文"]},
            }],
        }
        apply_full_fusion_review(files, review)
        self.assertEqual(files[0]["evidence_status"]["verified_ocr_noise_anchors"],
                         exclusions)
        self.assertIsNot(files[0]["evidence_status"]["verified_ocr_noise_anchors"],
                         exclusions)

    def test_courseware_fusion_keeps_headings_and_core_facts(self):
        pages = [["第一章 安全管理", "课程目标", "装饰字", "风险分级管控要求：明确职责。",
                  "一般说明文字很长并且用于解释本页的核心概念。", "页脚", "无关"]]
        result = _courseware_fusion_lines(pages, "安全管理.pptx")
        self.assertIn("第一章 安全管理", result)
        self.assertIn("风险分级管控要求：明确职责。", result)
        self.assertLessEqual(len(result), 6)

    def test_long_native_document_is_split_before_fusion_selection(self):
        record = {"reviewed_document_lines": ["专题：" + chr(0x4e00 + i) * 8
                                                for i in range(25)]}
        pages = _courseware_pages(record, "工作概况.docx")
        self.assertEqual([len(page) for page in pages], [24, 1])

    def test_courseware_requires_matching_source_hash(self):
        item = {"source_name": "课件.pdf", "sha256": "a" * 64}
        record = {"source": "课件.pdf", "source_sha256": "b" * 64, "pages": []}
        with self.assertRaises(ValueError):
            build_courseware(item, record, None)

    def test_courseware_uses_manual_cross_checked_fusion(self):
        digest = "a" * 64
        item = {"source_name": "课件.pdf", "sha256": digest}
        record = {"source": "课件.pdf", "source_sha256": digest,
                  "pages": [{"reviewed_lines": ["原始 OCR 错字"]}]}
        result = build_courseware(item, record, None, ["人工校对后的连贯结果。"])
        self.assertEqual(result["sections"]["GPT 融合校对结果"],
                         ["人工校对后的连贯结果。"])
        self.assertEqual(result["evidence_status"]["fusion_text_source"],
                         "manual-cross-checked")

    def test_strong_visual_facts_exclude_bottom_subtitles(self):
        def frame(index, text, top):
            return {"index": index, "timestamp_seconds": float(index),
                    "height": 900, "lines": [{"text": text, "confidence": 0.99,
                                                "box": [[10, top], [500, top],
                                                        [500, top + 30], [10, top + 30]]}]}

        ocr = {"frames": [
            frame(0, "当面对公司安全生产任务重", 825),
            frame(1, "当面对公司安全生产任务重", 825),
            frame(2, "当面对公司安全生产任务重", 825),
            frame(3, "安全管理十项要求", 300),
            frame(4, "安全管理十项要求", 300),
            frame(5, "安全管理十项要求", 300),
        ]}
        self.assertEqual(_strong_rapid_lines(ocr, ""), ["安全管理十项要求"])

    def test_bottom_subtitles_are_ordered_and_adjacent_duplicates_collapse(self):
        def frame(index, text, top):
            return {"index": index, "timestamp_seconds": float(index), "height": 900,
                    "lines": [{"text": text, "confidence": 0.99,
                               "box": [[10, top], [500, top],
                                       [500, top + 30], [10, top + 30]]}]}

        ocr = {"frames": [frame(0, "画面标题", 200), frame(1, "第一句字幕", 820),
                          frame(2, "第一句字幕", 820), frame(3, "第二句字幕", 820)]}
        self.assertEqual(_bottom_subtitle_transcript(ocr, ""),
                         "第一句字幕；第二句字幕")

    def test_visual_supplements_drop_orphaned_job_titles(self):
        lines = ["党支部书记", "主任工程师", "安全管理十项要求"]
        self.assertEqual(_visual_supplements(lines, "", limit=10),
                         ["安全管理十项要求"])

    def test_complete_mimo_is_primary_fusion_narrative(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            digest = "a" * 64
            (work / "asr.json").write_text(json.dumps({
                "model": "mimo-v2.5-asr", "source_sha256": digest,
                "coverage": {"complete": True},
                "segments": [{"text": "新的准确语音内容。"}],
            }))
            (work / "ocr.json").write_text(json.dumps({"frames": []}))
            manifest = {"source_name": "课程.mp4", "sha256": digest,
                        "has_audio": True}
            legacy = {"asr_paragraphs": ["旧的错误识别内容。"], "ocr_blocks": []}
            result = build_video(manifest, work, legacy)
            fused = "".join(result["sections"]["GPT 融合校对结果"])
            self.assertIn("新的准确语音内容", fused)
            self.assertNotIn("旧的错误识别内容", fused)

    def test_subtitles_can_proofread_fusion_when_mimo_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            subtitle = "字幕校对内容" * 55
            (work / "ocr.json").write_text(json.dumps({"frames": [{
                "index": 0, "timestamp_seconds": 0.0, "height": 900,
                "lines": [{"text": subtitle, "confidence": 0.99,
                           "box": [[10, 820], [900, 820], [900, 860], [10, 860]]}],
            }]}))
            manifest = {"source_name": "课程.mp4", "sha256": "a" * 64,
                        "has_audio": True}
            legacy = {"asr_paragraphs": ["旧的错误识别内容" * 45], "ocr_blocks": []}
            result = build_video(manifest, work, legacy)
            fused = "".join(result["sections"]["GPT 融合校对结果"])
            self.assertIn("字幕校对内容", fused)
            self.assertNotIn("旧的错误识别内容", fused)
            self.assertEqual(result["evidence_status"]["fusion_text_source"],
                             "rapidocr-subtitle-assisted")


if __name__ == "__main__":
    unittest.main()
