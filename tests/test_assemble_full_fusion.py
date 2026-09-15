import unittest

from media_pipeline.assemble_full_fusion import assemble, REVIEW_MODEL


class AssembleFullFusionTests(unittest.TestCase):
    def test_requires_exact_coverage_and_passes_evidence_to_review(self):
        speech = "完整保留第一项要求和第二项要求。" * 30
        base = {"files": [{
            "source_name": "课程.mp4", "source_sha256": "a" * 64,
            "kind": "video",
            "sections": {
                "ASR 结果": [speech], "OCR 结果": ["扫描背景误识57年。"],
                "GPT 融合校对结果": ["旧摘要"],
            },
        }]}
        exclusions = {"课程.mp4": [{
            "kind": "time", "canonical": "57年", "reason": "ocr_noise",
        }]}
        review, qa = assemble(base, {"课程.mp4": [speech]}, exclusions)
        self.assertTrue(qa["passed"])
        self.assertEqual(review["model"], REVIEW_MODEL)
        self.assertEqual(review["files"][0]["sections"]["GPT 融合校对结果"],
                         [speech])
        self.assertEqual(
            review["files"][0]["evidence_status"]["verified_ocr_noise_anchors"],
            exclusions["课程.mp4"],
        )

        with self.assertRaisesRegex(ValueError, "覆盖不完整"):
            assemble(base, {}, exclusions)

    def test_rejects_summary_even_when_shape_is_valid(self):
        speech = "必须逐项保留培训中的全部考试信息和事实细节。" * 30
        base = {"files": [{
            "source_name": "课程.mp4", "source_sha256": "a" * 64,
            "kind": "video",
            "sections": {"ASR 结果": [speech], "OCR 结果": [],
                         "GPT 融合校对结果": ["旧稿"]},
        }]}
        with self.assertRaisesRegex(ValueError, "未通过"):
            assemble(base, {"课程.mp4": ["本课程介绍考试信息。"]})


if __name__ == "__main__":
    unittest.main()
