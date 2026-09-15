import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts import prepare_gpt_fusion_tasks as prepare


class PrepareFusionTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contract = Path(__file__).resolve().parent.parent / "docs/gpt-fusion-contract.md"

    def tearDown(self):
        self.temp.cleanup()

    def evidence(self, *, status="complete", blockers=None):
        files = []
        for index in range(1, 5):
            source_sha = f"{index:064x}"
            raw = {
                "asset_id": f"asset-{index}", "source_name": f"课程{index}.mp4",
                "source_sha256": source_sha, "catalog_position": index,
                "catalog_suborder": 1, "kind": "video",
                "asr_result": {"result": {"segments": [
                    {"index": 0, "text": "重复语音"}, {"index": 1, "text": "重复语音"},
                ]}},
                "ocr_result": {"frames": [{"lines": [
                    {"text": "重复画面", "box": [[0, 0], [1, 0], [1, 1], [0, 1]]},
                    {"text": "重复画面", "box": [[0, 0], [1, 0], [1, 1], [0, 1]]},
                ]}]},
            }
            files.append({
                "asset_id": raw["asset_id"], "source_sha256": source_sha,
                "evidence_sha256": prepare.canonical_sha256(raw),
                "sections": {"ASR 结果": ["重复语音", "重复语音"],
                             "OCR 结果": ["重复画面", "重复画面"]},
                "raw_evidence": raw,
            })
        return {"status": status, "blockers": [] if blockers is None else blockers,
                "file_count": len(files), "files": files}

    def write_evidence(self, document):
        path = self.root / "evidence.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def test_tasks_are_verbatim_bound_and_balanced_in_three_batches(self):
        document = self.evidence()
        output = self.root / "tasks"
        result = prepare.prepare_tasks(
            evidence_path=self.write_evidence(document), contract_path=self.contract,
            output_dir=output, expected_files=4, batch_count=3,
        )
        index = result["index"]
        self.assertEqual(index["file_count"], 4)
        self.assertEqual(len(index["batches"]), 3)
        self.assertEqual([item["order"] for item in index["tasks"]], [1, 2, 3, 4])
        self.assertEqual(sum(item["task_count"] for item in index["batches"]), 4)
        for entry, original in zip(index["tasks"], document["files"]):
            task = json.loads((output / entry["task_file"]).read_text())
            self.assertEqual(set(task), set(prepare.TASK_KEYS))
            self.assertEqual(task["ASR 结果"], original["raw_evidence"]["asr_result"])
            self.assertEqual(task["OCR 结果"], original["raw_evidence"]["ocr_result"])
            self.assertEqual([s["text"] for s in task["ASR 结果"]["result"]["segments"]],
                             ["重复语音", "重复语音"])
            self.assertEqual([x["text"] for x in task["OCR 结果"]["frames"][0]["lines"]],
                             ["重复画面", "重复画面"])
            self.assertEqual(entry["task_sha256"], prepare.canonical_sha256(task))
            self.assertEqual(task["evidence_sha256"], original["evidence_sha256"])
            self.assertEqual(
                task["task_constraints"]["output"]["required_fields"],
                ["asset_id", "source_sha256", "evidence_sha256", "task_sha256",
                 "model", "paragraphs"],
            )
        self.assertEqual(json.loads((output / "index.json").read_text()), index)

    def test_incomplete_or_blocked_input_is_refused_before_writes(self):
        for document in (
            self.evidence(status="incomplete"),
            self.evidence(blockers=[{"code": "pending"}]),
        ):
            with self.subTest(status=document["status"], blockers=document["blockers"]):
                output = self.root / f"tasks-{len(list(self.root.glob('tasks-*')))}"
                with self.assertRaises(prepare.TaskPreparationError):
                    prepare.prepare_tasks(
                        evidence_path=self.write_evidence(document), contract_path=self.contract,
                        output_dir=output, expected_files=4,
                    )
                self.assertFalse(output.exists())

    def test_old_generated_fields_and_broken_evidence_sha_are_refused(self):
        variants = []
        root_old_field = self.evidence()
        root_old_field["fused_content"] = {"files": ["旧稿"]}
        variants.append(root_old_field)
        old_field = self.evidence()
        old_field["files"][0]["raw_evidence"]["gpt_result"] = "旧稿"
        variants.append(old_field)
        old_section = self.evidence()
        old_section["files"][0]["sections"]["GPT 融合校对结果"] = ["旧稿"]
        variants.append(old_section)
        old_fused = self.evidence()
        old_fused["files"][0]["raw_evidence"]["ocr_result"]["fused_content"] = "旧稿"
        variants.append(old_fused)
        old_paragraphs = self.evidence()
        old_paragraphs["files"][0]["raw_evidence"]["asr_result"]["paragraphs"] = ["旧稿"]
        variants.append(old_paragraphs)
        broken_sha = self.evidence()
        broken_sha["files"][0]["raw_evidence"]["source_name"] = "被篡改.mp4"
        variants.append(broken_sha)
        for index, document in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(prepare.TaskPreparationError):
                prepare.prepare_tasks(
                    evidence_path=self.write_evidence(deepcopy(document)),
                    contract_path=self.contract, output_dir=self.root / f"bad-{index}",
                    expected_files=4,
                )


if __name__ == "__main__":
    unittest.main()
