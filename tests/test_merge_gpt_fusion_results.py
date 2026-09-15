import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts import merge_gpt_fusion_results as merge


class MergeFusionResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks"
        self.results = self.root / "results"
        self.tasks.mkdir()
        self.results.mkdir()
        self.count = 4
        index_tasks = []
        self.original_paragraphs = {}
        for order in range(1, self.count + 1):
            asset_id = f"asset-{order}"
            source_sha = f"{order:064x}"
            evidence_sha = f"{order + 100:064x}"
            task = {
                "asset_id": asset_id, "source_name": f"课程{order}.mp4",
                "source_sha256": source_sha, "evidence_sha256": evidence_sha,
                "ASR 结果": {"segments": [{"text": f"语音{order}"}]},
                "OCR 结果": {"frames": [{"lines": [{"text": f"画面{order}"}]}]},
                "task_constraints": {"final_review": "模型必须完成文件级最终复核。"},
            }
            task_sha = merge.canonical_sha256(task)
            filename = f"{order:04d}-{source_sha[:16]}.json"
            (self.tasks / filename).write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
            index_tasks.append({
                "order": order, "asset_id": asset_id, "source_sha256": source_sha,
                "evidence_sha256": evidence_sha, "task_sha256": task_sha,
                "task_file": filename,
            })
            paragraphs = [f"课程{order}完整正文第一段。", f"课程{order}完整正文第一段。"]
            self.original_paragraphs[asset_id] = paragraphs
            self.write_result(order, {
                "asset_id": asset_id, "source_sha256": source_sha,
                "evidence_sha256": evidence_sha, "task_sha256": task_sha,
                "model": "test-model-v1", "paragraphs": paragraphs,
            })
        (self.tasks / "index.json").write_text(json.dumps({
            "status": "ready", "file_count": self.count, "tasks": index_tasks,
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_result(self, order, value):
        path = self.results / f"result-{order:04d}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def load_result(self, order):
        return json.loads((self.results / f"result-{order:04d}.json").read_text())

    def merge(self):
        return merge.merge_results(
            task_dir=self.tasks, result_dir=self.results,
            output_path=self.root / "gpt_final.json",
            risk_report_path=self.root / "risks.json", expected_files=self.count,
        )

    def test_valid_results_are_reordered_and_preserved_verbatim(self):
        # Filenames need not supply order; the task index always does.
        first = self.results / "result-0001.json"
        first.rename(self.results / "z-result.json")
        final, risks = self.merge()
        self.assertEqual(final["model"], "test-model-v1")
        self.assertEqual([item["asset_id"] for item in final["files"]],
                         [f"asset-{i}" for i in range(1, 5)])
        for item in final["files"]:
            self.assertEqual(item["paragraphs"], self.original_paragraphs[item["asset_id"]])
            self.assertIsNot(item["paragraphs"], self.original_paragraphs[item["asset_id"]])
            order = int(item["asset_id"].split("-")[-1])
            self.assertEqual(item["evidence_sha256"], f"{order + 100:064x}")
            self.assertEqual(item["task_sha256"], json.loads(
                (self.tasks / "index.json").read_text())["tasks"][order - 1]["task_sha256"])
        self.assertEqual(json.loads((self.root / "gpt_final.json").read_text()), final)
        self.assertFalse(risks["policy"]["paragraphs_modified"])
        self.assertEqual(risks["warnings"], [])

    def test_missing_duplicate_and_sha_mismatch_are_rejected(self):
        missing = self.results / "result-0004.json"
        missing.unlink()
        with self.assertRaises(merge.FusionMergeError):
            self.merge()
        self.write_result(4, {**self.load_result(3), "asset_id": "asset-3"})
        with self.assertRaises(merge.FusionMergeError):
            self.merge()

        # Restore the fourth result, then break each of the three SHA bindings.
        item = self.load_result(3)
        index_four = json.loads((self.tasks / "index.json").read_text())["tasks"][3]
        correct = {
            "asset_id": "asset-4", "source_sha256": index_four["source_sha256"],
            "evidence_sha256": index_four["evidence_sha256"],
            "task_sha256": index_four["task_sha256"], "model": "test-model-v1",
            "paragraphs": ["课程4恢复后的正文。"],
        }
        for field in ("source_sha256", "evidence_sha256", "task_sha256"):
            value = deepcopy(correct)
            value[field] = "f" * 64
            self.write_result(4, value)
            with self.subTest(field=field), self.assertRaises(merge.FusionMergeError):
                self.merge()

    def test_forbidden_labels_process_fields_and_cross_source_copy_are_rejected(self):
        prohibited = (
            "【ASR结果】这里是旧底稿。",
            "根据ASR与OCR可以得出以下内容。",
            "以下是完整的语音识别底稿。",
            "以下是完整的OCR识别底稿。",
            "ASR/OCR原文附后。",
            "根据识别结果整理如下。",
            "timestamp_seconds: 12.5",
            "置信度：0.99",
        )
        original = self.load_result(1)
        for index, paragraph in enumerate(prohibited):
            changed = deepcopy(original)
            changed["paragraphs"] = [paragraph]
            self.write_result(1, changed)
            with self.subTest(index=index), self.assertRaises(merge.FusionMergeError):
                self.merge()
        self.write_result(1, original)
        copied = self.load_result(2)
        copied["paragraphs"] = original["paragraphs"]
        self.write_result(2, copied)
        with self.assertRaisesRegex(merge.FusionMergeError, "跨源"):
            self.merge()

    def test_long_exact_raw_evidence_copy_and_generated_task_field_are_rejected(self):
        task_index = json.loads((self.tasks / "index.json").read_text())["tasks"][0]
        task_path = self.tasks / task_index["task_file"]
        task = json.loads(task_path.read_text())
        raw_dump = "原始语音底稿句子。" * 60
        task["ASR 结果"] = {"segments": [{"text": raw_dump}]}
        task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        task_index["task_sha256"] = merge.canonical_sha256(task)
        index = json.loads((self.tasks / "index.json").read_text())
        index["tasks"][0] = task_index
        (self.tasks / "index.json").write_text(json.dumps(index), encoding="utf-8")
        result = self.load_result(1)
        result["task_sha256"] = task_index["task_sha256"]
        result["paragraphs"] = [raw_dump]
        self.write_result(1, result)
        with self.assertRaisesRegex(merge.FusionMergeError, "机械底稿复制"):
            self.merge()

        # A task carrying prior/generated prose is not a pure evidence task,
        # even if the index hash is recomputed to make the file look bound.
        task["paragraphs"] = ["旧融合正文"]
        task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        index["tasks"][0]["task_sha256"] = merge.canonical_sha256(task)
        (self.tasks / "index.json").write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(merge.FusionMergeError, "非纯ASR/OCR证据字段"):
            self.merge()

    def test_cross_asset_identity_and_model_disagreement_are_rejected(self):
        second = self.load_result(2)
        second["source_sha256"] = self.load_result(1)["source_sha256"]
        self.write_result(2, second)
        with self.assertRaises(merge.FusionMergeError):
            self.merge()
        second = self.load_result(2)
        index_second = json.loads((self.tasks / "index.json").read_text())["tasks"][1]
        second["source_sha256"] = index_second["source_sha256"]
        second["model"] = "different-model"
        self.write_result(2, second)
        with self.assertRaises(merge.FusionMergeError):
            self.merge()


if __name__ == "__main__":
    unittest.main()
