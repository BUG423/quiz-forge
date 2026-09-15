#!/usr/bin/env python3
"""Package completed ASR/OCR evidence into independent model work items.

This script performs no model call and writes no fused prose.  It refuses any
incomplete evidence corpus or input carrying a prior generated-prose field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE = ROOT / ".work" / "first-principles-2026" / "evidence_corpus.json"
DEFAULT_CONTRACT = ROOT / "docs" / "gpt-fusion-contract.md"
DEFAULT_OUTPUT_DIR = ROOT / ".work" / "first-principles-2026" / "gpt-tasks"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
TASK_KEYS = (
    "asset_id", "source_name", "source_sha256", "evidence_sha256",
    "ASR 结果", "OCR 结果", "task_constraints",
)
FORBIDDEN_INPUT_KEYS = frozenset({
    "paragraphs", "gpt", "gpt_result", "gpt_final", "gpt_fusion",
    "gpt_fused", "fusion_text", "fused_content", "generated_prose",
    "GPT 融合校对结果",
})
CONTRACT_ANCHORS = (
    "完整阅读 ASR 与 OCR",
    "正文不是摘要",
    "不得编造缺失内容",
    "只返回该文件的最终正文段落",
    "不得把旧 GPT 融合稿作为输入证据",
    "文件级最终复核",
)


class TaskPreparationError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskPreparationError(f"无法读取有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise TaskPreparationError(f"JSON 顶层必须是对象：{path}")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def valid_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
        raise TaskPreparationError(f"{field} 必须是 64 位 SHA-256")
    return value.lower()


def assert_no_prior_generated_content(value: Any, location: str = "evidence") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().casefold()
            if (key_text in FORBIDDEN_INPUT_KEYS or normalized in FORBIDDEN_INPUT_KEYS
                    or "gpt" in normalized):
                raise TaskPreparationError(f"输入含旧 GPT/融合字段：{location}.{key_text}")
            assert_no_prior_generated_content(child, f"{location}.{key_text}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_prior_generated_content(child, f"{location}[{index}]")


def load_contract_summary(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise TaskPreparationError(f"无法读取融合契约：{path}") from exc
    missing = [anchor for anchor in CONTRACT_ANCHORS if anchor not in text]
    if missing:
        raise TaskPreparationError(f"融合契约缺少必要条款：{missing}")
    return {
        "contract_path": "docs/gpt-fusion-contract.md",
        "contract_sha256": hashlib.sha256(payload).hexdigest(),
        "objective": "完整阅读本资产全部ASR与OCR证据，输出可检索、可答题的详尽最终课程正文，不作摘要。",
        "must_preserve": [
            "定义、概念、观点、原理、背景和不同条件下的信息",
            "数字、日期、比例、单位、型号、名称和专有词",
            "流程、步骤、菜单、字段、按钮、条件、例外和注意事项",
            "制度条款、职责边界、风险点、案例事实、处理结果和经验要求",
            "语音未说出但画面、表格、图示、界面、附件中出现的信息",
        ],
        "conflict_policy": "证据冲突且无法可靠判断时不夸大，只保留可确认信息，不编造。",
        "allowed_deduplication": "只可按语义合并同一事实的重复出现，不得删除看似次要的内容。",
        "output": {
            "format": "single JSON object",
            "required_fields": [
                "asset_id", "source_sha256", "evidence_sha256", "task_sha256",
                "model", "paragraphs",
            ],
            "metadata_policy": "标识字段只作身份与任务绑定，不进入Word正文。",
            "paragraphs": "只含文件级最终正文段落；不得追加前言、标签或处理说明。",
        },
        "prohibited": [
            "摘要、概览、结论性缩写或‘主要讲了’式改写",
            "ASR/OCR/语音识别/文字识别底稿标签以及‘根据ASR/OCR’‘根据识别结果’等处理过程",
            "帧号、时间戳、置信度、缓存路径、请求信息或模型日志",
            "复制整份原始底稿作为失败兜底",
            "使用任何旧融合正文补齐失败",
        ],
        "final_review": "超长证据可分段阅读，但最终必须进行一次文件级复核后再输出paragraphs。",
    }


def evidence_character_count(file_record: dict[str, Any]) -> int:
    sections = file_record.get("sections")
    if not isinstance(sections, dict) or set(sections) != {"ASR 结果", "OCR 结果"}:
        raise TaskPreparationError("evidence sections 必须且只能含 ASR 结果、OCR 结果")
    total = 0
    for name in ("ASR 结果", "OCR 结果"):
        values = sections[name]
        if not isinstance(values, list) or not values or not all(isinstance(item, str) for item in values):
            raise TaskPreparationError(f"{name} 必须是非空字符串数组")
        total += sum(len(item) for item in values)
    return total


def validate_evidence(document: dict[str, Any], *, expected_files: int) -> list[dict[str, Any]]:
    assert_no_prior_generated_content(document)
    if document.get("status") != "complete":
        raise TaskPreparationError("只接受 status=complete 的 evidence_corpus")
    blockers = document.get("blockers")
    if blockers != []:
        raise TaskPreparationError("完成态 evidence_corpus 的 blockers 必须为空数组")
    files = document.get("files")
    if (document.get("file_count") != expected_files or not isinstance(files, list)
            or len(files) != expected_files):
        actual = len(files) if isinstance(files, list) else "invalid"
        raise TaskPreparationError(f"evidence_corpus 必须恰好 {expected_files} 项，实际 {actual}")
    seen_ids, seen_source_shas = set(), set()
    previous_order: tuple[float, int] | None = None
    for order, item in enumerate(files, 1):
        if not isinstance(item, dict):
            raise TaskPreparationError(f"files[{order}] 必须是对象")
        asset_id = item.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id or asset_id in seen_ids:
            raise TaskPreparationError(f"asset_id 缺失或重复：files[{order}]")
        source_sha = valid_sha(item.get("source_sha256"), f"{asset_id}.source_sha256")
        evidence_sha = valid_sha(item.get("evidence_sha256"), f"{asset_id}.evidence_sha256")
        raw = item.get("raw_evidence")
        if not isinstance(raw, dict):
            raise TaskPreparationError(f"{asset_id}.raw_evidence 缺失")
        if (raw.get("asset_id") != asset_id or raw.get("source_sha256") != source_sha
                or canonical_sha256(raw) != evidence_sha):
            raise TaskPreparationError(f"asset/source/evidence SHA 绑定失败：{asset_id}")
        if not isinstance(raw.get("source_name"), str) or not raw["source_name"]:
            raise TaskPreparationError(f"source_name 缺失：{asset_id}")
        if not isinstance(raw.get("asr_result"), dict) or not isinstance(raw.get("ocr_result"), dict):
            raise TaskPreparationError(f"完整 ASR/OCR 结构缺失：{asset_id}")
        position, suborder = raw.get("catalog_position"), raw.get("catalog_suborder")
        if not isinstance(position, (int, float)) or not isinstance(suborder, int):
            raise TaskPreparationError(f"目录位置无效：{asset_id}")
        sort_key = (float(position), suborder)
        if previous_order is not None and sort_key < previous_order:
            raise TaskPreparationError("evidence files 未保持目录顺序")
        previous_order = sort_key
        evidence_character_count(item)
        if source_sha in seen_source_shas:
            raise TaskPreparationError(f"source_sha256 重复：{source_sha}")
        seen_ids.add(asset_id)
        seen_source_shas.add(source_sha)
    return files


def greedy_batches(tasks: list[dict[str, Any]], batch_count: int = 3) -> list[dict[str, Any]]:
    if batch_count < 1:
        raise ValueError("batch_count must be positive")
    batches = [{"batch": index + 1, "evidence_character_count": 0, "tasks": []}
               for index in range(batch_count)]
    # Longest-processing-time greedy assignment, deterministic on original order.
    for task in sorted(tasks, key=lambda item: (-item["evidence_character_count"], item["order"])):
        target = min(batches, key=lambda item: (item["evidence_character_count"], item["batch"]))
        target["tasks"].append({
            "order": task["order"], "asset_id": task["asset_id"],
            "task_file": task["task_file"], "task_sha256": task["task_sha256"],
            "evidence_character_count": task["evidence_character_count"],
        })
        target["evidence_character_count"] += task["evidence_character_count"]
    for batch in batches:
        batch["tasks"].sort(key=lambda item: item["order"])
        batch["task_count"] = len(batch["tasks"])
    return batches


def write_task_directory(output_dir: Path, tasks: list[tuple[str, dict[str, Any]]], index: dict[str, Any]) -> None:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise TaskPreparationError(f"任务目录已存在，拒绝覆盖：{output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        for filename, task in tasks:
            path = staged / filename
            with path.open("wb") as handle:
                handle.write(canonical_bytes(task) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        with (staged / "index.json").open("wb") as handle:
            handle.write(canonical_bytes(index) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, output_dir)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def prepare_tasks(
    *,
    evidence_path: Path = DEFAULT_EVIDENCE,
    contract_path: Path = DEFAULT_CONTRACT,
    output_dir: Path | None = DEFAULT_OUTPUT_DIR,
    expected_files: int = 91,
    batch_count: int = 3,
) -> dict[str, Any]:
    document = read_json(evidence_path)
    files = validate_evidence(document, expected_files=expected_files)
    constraints = load_contract_summary(contract_path)
    task_files: list[tuple[str, dict[str, Any]]] = []
    index_tasks: list[dict[str, Any]] = []
    for order, item in enumerate(files, 1):
        raw = item["raw_evidence"]
        task = {
            "asset_id": item["asset_id"],
            "source_name": raw["source_name"],
            "source_sha256": item["source_sha256"],
            "evidence_sha256": item["evidence_sha256"],
            "ASR 结果": raw["asr_result"],
            "OCR 结果": raw["ocr_result"],
            "task_constraints": constraints,
        }
        if tuple(task) != TASK_KEYS:
            raise AssertionError("Task schema changed unexpectedly")
        filename = f"{order:04d}-{item['source_sha256'][:16]}.json"
        task_sha = canonical_sha256(task)
        characters = evidence_character_count(item)
        task_files.append((filename, task))
        index_tasks.append({
            "order": order,
            "catalog_position": raw["catalog_position"],
            "catalog_suborder": raw["catalog_suborder"],
            "asset_id": item["asset_id"],
            "source_name": raw["source_name"],
            "source_sha256": item["source_sha256"],
            "evidence_sha256": item["evidence_sha256"],
            "evidence_character_count": characters,
            "task_file": filename,
            "task_sha256": task_sha,
        })
    batches = greedy_batches(index_tasks, batch_count=batch_count)
    batch_by_asset = {
        task["asset_id"]: batch["batch"]
        for batch in batches for task in batch["tasks"]
    }
    for task in index_tasks:
        task["batch"] = batch_by_asset[task["asset_id"]]
    index = {
        "schema_version": "gpt-fusion-task-index/v1",
        "status": "ready",
        "file_count": len(index_tasks),
        "batch_count": batch_count,
        "contract_path": constraints["contract_path"],
        "contract_sha256": constraints["contract_sha256"],
        "total_evidence_character_count": sum(item["evidence_character_count"] for item in index_tasks),
        "tasks": index_tasks,
        "batches": batches,
    }
    if len({item["task_sha256"] for item in index_tasks}) != len(index_tasks):
        raise TaskPreparationError("任务 SHA 重复，拒绝输出")
    if output_dir is not None:
        write_task_directory(output_dir, task_files, index)
    return {"index": index, "task_files": task_files}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = prepare_tasks(
            evidence_path=args.evidence,
            contract_path=args.contract,
            output_dir=args.output_dir,
            expected_files=91,
            batch_count=3,
        )
    except TaskPreparationError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc),
                          "output_written": False}, ensure_ascii=False, indent=2))
        return 2
    index = result["index"]
    print(json.dumps({
        "status": index["status"],
        "output_dir": str(args.output_dir.resolve()),
        "file_count": index["file_count"],
        "total_evidence_character_count": index["total_evidence_character_count"],
        "batches": [{"batch": item["batch"], "task_count": item["task_count"],
                     "evidence_character_count": item["evidence_character_count"]}
                    for item in index["batches"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
