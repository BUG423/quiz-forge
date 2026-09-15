#!/usr/bin/env python3
"""Validate and mechanically merge independent per-asset fusion results.

No paragraph is edited, supplemented, concatenated, or recovered from evidence
or an older draft.  Any invalid result rejects the entire output before write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASK_DIR = ROOT / ".work" / "first-principles-2026" / "gpt-tasks"
DEFAULT_RESULT_DIR = ROOT / ".work" / "first-principles-2026" / "gpt-results"
DEFAULT_OUTPUT = ROOT / ".work" / "first-principles-2026" / "gpt_final.json"
DEFAULT_RISK_REPORT = ROOT / ".work" / "first-principles-2026" / "gpt_final.risks.json"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
RESULT_KEYS = frozenset({
    "asset_id", "source_sha256", "evidence_sha256", "task_sha256", "model", "paragraphs",
})
TASK_KEYS = frozenset({
    "asset_id", "source_name", "source_sha256", "evidence_sha256",
    "ASR 结果", "OCR 结果", "task_constraints",
})
FORBIDDEN_LITERALS = (
    "完整语音识别底稿", "完整画面文字", "ASR底稿", "ASR 底稿",
    "OCR底稿", "OCR 底稿", "OCR原始文字", "整页OCR原始文字",
    "OCR补充", "OCR 补充", "既有GPT逐文件校对正文",
    "文档原生文字、脚注及图片交叉校正", "原生文字、备注、图表及替代文本",
    "嵌入工作簿完整单元格", "完整内嵌媒体语音", "完整内嵌媒体画面文字",
)
FORBIDDEN_PATTERNS = (
    re.compile(
        r"(?i)(?:完整(?:的)?\s*)?"
        r"(?:(?:ASR|OCR|语音识别|文字识别)"
        r"(?:\s*(?:和|与|/|\+|、)\s*(?:ASR|OCR|语音识别|文字识别))*)"
        r"\s*(?:的)?\s*(?:识别)?\s*(?:底稿|原稿|原文|结果|补充)"
    ),
    re.compile(r"(?i)根据\s*(?:上述|前述|所给|提供的)?\s*(?:ASR|OCR|语音识别|文字识别|识别结果)"),
    re.compile(r"(?:从画面可见|识别结果显示|语音识别显示|文字识别显示|OCR显示|ASR显示)"),
    re.compile(r"(?i)(?:frame_index|frame_source|frame_image_sha256|merged_order|timestamp_seconds|request_id|cache_path|sample_start|sample_end|confidence|source_sha256|evidence_sha256|task_sha256)\s*[:=：]"),
    re.compile(r"(?:帧号|时间戳|置信度|缓存路径|请求信息|模型日志)\s*[:=：]"),
)
CONTROL_RE = re.compile(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
RAW_COPY_MIN_CHARACTERS = 400


class FusionMergeError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FusionMergeError(f"无法读取有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise FusionMergeError(f"JSON 顶层必须是对象：{path}")
    return value


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def valid_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
        raise FusionMergeError(f"{field} 必须是 64 位 SHA-256")
    return value.lower()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_paragraphs(value: Any, asset_id: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise FusionMergeError(f"paragraphs 不能为空：{asset_id}")
    paragraphs: list[str] = []
    for index, paragraph in enumerate(value):
        if (not isinstance(paragraph, str) or not paragraph
                or paragraph != paragraph.strip() or CONTROL_RE.search(paragraph)):
            raise FusionMergeError(f"paragraphs[{index}] 必须是无控制字符的非空纯字符串：{asset_id}")
        if any(literal in paragraph for literal in FORBIDDEN_LITERALS):
            raise FusionMergeError(f"正文含旧底稿/处理标签：{asset_id}")
        if any(pattern.search(paragraph) for pattern in FORBIDDEN_PATTERNS):
            raise FusionMergeError(f"正文含处理过程或技术字段：{asset_id}")
        paragraphs.append(paragraph)
    return paragraphs


def _collect_evidence_text(value: Any, *, parent_key: str | None = None) -> list[str]:
    """Collect only evidence-bearing strings, never technical metadata strings."""
    output: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "text" and isinstance(child, str):
                output.append(child)
            elif key == "value" and parent_key == "cells" and isinstance(child, str):
                output.append(child)
            else:
                output.extend(_collect_evidence_text(child, parent_key=key))
    elif isinstance(value, list):
        for child in value:
            output.extend(_collect_evidence_text(child, parent_key=parent_key))
    return output


def _space_normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def assert_not_full_raw_copy(
    paragraphs: list[str], task_document: dict[str, Any], asset_id: str,
) -> None:
    """Reject a long, exact ASR/OCR dump masquerading as model-written prose.

    This is deliberately limited to exact whole-stream equality.  It never
    rewrites, ranks, shortens, or otherwise judges the model's prose.
    """
    candidate = _space_normalized(" ".join(paragraphs))
    channels = {
        "ASR": _collect_evidence_text(task_document["ASR 结果"]),
        "OCR": _collect_evidence_text(task_document["OCR 结果"]),
    }
    channels["ASR+OCR"] = channels["ASR"] + channels["OCR"]
    for channel, values in channels.items():
        evidence_stream = _space_normalized(" ".join(values))
        if (len(evidence_stream) >= RAW_COPY_MIN_CHARACTERS
                and candidate == evidence_stream):
            raise FusionMergeError(f"正文与完整{channel}原始文字逐字相同，疑似机械底稿复制：{asset_id}")


def load_index(
    task_dir: Path, *, expected_files: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    index = read_json(task_dir / "index.json")
    tasks = index.get("tasks")
    if (index.get("status") != "ready" or index.get("file_count") != expected_files
            or not isinstance(tasks, list) or len(tasks) != expected_files):
        raise FusionMergeError(f"任务索引必须为 ready 且恰好 {expected_files} 项")
    seen_ids, seen_sources, seen_task_shas = set(), set(), set()
    task_documents: dict[str, dict[str, Any]] = {}
    previous_order = 0
    for position, item in enumerate(tasks, 1):
        if not isinstance(item, dict):
            raise FusionMergeError(f"index.tasks[{position}] 必须是对象")
        order = item.get("order")
        if not isinstance(order, int) or order != previous_order + 1:
            raise FusionMergeError("任务索引 order 必须从 1 连续递增")
        previous_order = order
        asset_id = item.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id or asset_id in seen_ids:
            raise FusionMergeError("任务索引 asset_id 缺失或重复")
        source_sha = valid_sha(item.get("source_sha256"), f"{asset_id}.source_sha256")
        evidence_sha = valid_sha(item.get("evidence_sha256"), f"{asset_id}.evidence_sha256")
        task_sha = valid_sha(item.get("task_sha256"), f"{asset_id}.task_sha256")
        filename = item.get("task_file")
        if (not isinstance(filename, str) or Path(filename).name != filename
                or not filename.endswith(".json")):
            raise FusionMergeError(f"task_file 非法：{asset_id}")
        task_document = read_json(task_dir / filename)
        if set(task_document) != TASK_KEYS:
            raise FusionMergeError(f"任务文件含非纯ASR/OCR证据字段：{asset_id}")
        if canonical_sha256(task_document) != task_sha:
            raise FusionMergeError(f"任务文件 SHA 与索引不一致：{asset_id}")
        if (task_document.get("asset_id") != asset_id
                or task_document.get("source_sha256") != source_sha
                or task_document.get("evidence_sha256") != evidence_sha):
            raise FusionMergeError(f"任务文件身份绑定失败：{asset_id}")
        if source_sha in seen_sources or task_sha in seen_task_shas:
            raise FusionMergeError("任务索引 source/task SHA 重复")
        seen_ids.add(asset_id)
        seen_sources.add(source_sha)
        seen_task_shas.add(task_sha)
        task_documents[asset_id] = task_document
    return index, tasks, task_documents


def load_results(result_dir: Path, *, expected_files: int) -> dict[str, tuple[Path, dict[str, Any]]]:
    paths = sorted(result_dir.glob("*.json")) if result_dir.is_dir() else []
    if len(paths) != expected_files:
        raise FusionMergeError(f"结果文件必须恰好 {expected_files} 份，实际 {len(paths)}")
    output: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in paths:
        result = read_json(path)
        if set(result) != RESULT_KEYS:
            raise FusionMergeError(f"结果字段必须严格为 {sorted(RESULT_KEYS)}：{path.name}")
        asset_id = result.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise FusionMergeError(f"结果 asset_id 缺失：{path.name}")
        if asset_id in output:
            raise FusionMergeError(f"结果含重复 asset_id：{asset_id}")
        output[asset_id] = (path, result)
    return output


def merge_results(
    *,
    task_dir: Path = DEFAULT_TASK_DIR,
    result_dir: Path = DEFAULT_RESULT_DIR,
    output_path: Path | None = DEFAULT_OUTPUT,
    risk_report_path: Path | None = DEFAULT_RISK_REPORT,
    expected_files: int = 91,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _, tasks, task_documents = load_index(task_dir, expected_files=expected_files)
    results = load_results(result_dir, expected_files=expected_files)
    indexed_ids = {item["asset_id"] for item in tasks}
    if set(results) != indexed_ids:
        missing = sorted(indexed_ids - set(results))
        extra = sorted(set(results) - indexed_ids)
        raise FusionMergeError(f"结果与任务资产集不一致：missing={missing}, extra={extra}")

    models: set[str] = set()
    final_files: list[dict[str, Any]] = []
    report_items: list[dict[str, Any]] = []
    paragraph_payloads: dict[str, str] = {}
    for item in tasks:
        asset_id = item["asset_id"]
        path, result = results[asset_id]
        source_sha = valid_sha(result.get("source_sha256"), f"{asset_id}.result.source_sha256")
        evidence_sha = valid_sha(result.get("evidence_sha256"), f"{asset_id}.result.evidence_sha256")
        task_sha = valid_sha(result.get("task_sha256"), f"{asset_id}.result.task_sha256")
        if (source_sha != item["source_sha256"] or evidence_sha != item["evidence_sha256"]
                or task_sha != item["task_sha256"]):
            raise FusionMergeError(f"结果 source/evidence/task SHA 绑定失败：{asset_id}")
        model = result.get("model")
        if not isinstance(model, str) or not model or model != model.strip():
            raise FusionMergeError(f"结果 model 必须为如实填写的非空字符串：{asset_id}")
        models.add(model)
        paragraphs = validate_paragraphs(result.get("paragraphs"), asset_id)
        assert_not_full_raw_copy(paragraphs, task_documents[asset_id], asset_id)
        payload_sha = canonical_sha256(paragraphs)
        prior_asset = paragraph_payloads.get(payload_sha)
        if prior_asset is not None:
            raise FusionMergeError(f"跨源正文完全相同，疑似串稿：{prior_asset} / {asset_id}")
        paragraph_payloads[payload_sha] = asset_id
        # List and string objects are carried unchanged.  There is no fallback
        # to task evidence, no old draft, and no joining or appended sentence.
        final_files.append({
            "asset_id": asset_id,
            "source_sha256": source_sha,
            "evidence_sha256": evidence_sha,
            "task_sha256": task_sha,
            "paragraphs": paragraphs,
        })
        report_items.append({
            "order": item["order"], "asset_id": asset_id,
            "result_file": path.name, "source_sha256": source_sha,
            "evidence_sha256": evidence_sha, "task_sha256": task_sha,
            "paragraph_count": len(paragraphs),
            "character_count": sum(len(value) for value in paragraphs),
            "paragraph_payload_sha256": payload_sha,
        })
    if len(models) != 1:
        raise FusionMergeError(f"91 份结果的 model 必须完全一致，实际 {sorted(models)}")
    model = next(iter(models))
    final = {"model": model, "file_count": len(final_files), "files": final_files}
    risks = {
        "schema_version": "gpt-fusion-merge-risk-report/v1",
        "status": "validated_no_automatic_edits",
        "model": model,
        "file_count": len(report_items),
        "policy": {
            "paragraphs_modified": False,
            "fallback_from_asr_or_ocr": False,
            "fallback_from_old_draft": False,
            "automatic_risk_rewrite": False,
        },
        "warnings": [],
        "files": report_items,
    }
    if output_path is not None:
        atomic_json(output_path, final)
    if risk_report_path is not None:
        atomic_json(risk_report_path, risks)
    return final, risks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, default=DEFAULT_TASK_DIR)
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--risk-report", type=Path, default=DEFAULT_RISK_REPORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        final, risks = merge_results(
            task_dir=args.task_dir, result_dir=args.result_dir,
            output_path=args.output, risk_report_path=args.risk_report,
            expected_files=91,
        )
    except FusionMergeError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc),
                          "output_written": False}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "status": risks["status"], "model": final["model"],
        "file_count": final["file_count"], "output": str(args.output.resolve()),
        "risk_report": str(args.risk_report.resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
