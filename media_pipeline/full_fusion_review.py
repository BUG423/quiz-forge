"""Resumable, lossless ASR/OCR fusion proofreading with MiMo.

This module intentionally ignores any pre-existing fused section.  Each task
is derived only from the source-bound ``ASR 结果`` and ``OCR 结果`` arrays in
``fused_content.json``.  Running the CLI is a dry run unless ``--execute`` is
supplied, so importing or inspecting a batch can never spend API quota.

Credentials are accepted only from ``MIMO_API_KEY`` or a hidden ``getpass``
prompt.  They are never put in request-independent records, cache files,
merged output, progress messages, or provider-error messages.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request

from .full_fusion_qa import check_file, extract_anchors

MODEL = "mimo-v2.5-pro"
ENDPOINT = "https://api.xiaomimimo.com/v1/chat/completions"
ALLOWED_ENDPOINTS = frozenset({
    ENDPOINT,
    "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
    "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions",
    "https://token-plan-ams.xiaomimimo.com/v1/chat/completions",
})
PROMPT_VERSION = "lossless-full-fusion-zh-v9-final-only"
CACHE_SCHEMA = "full-fusion-review-cache-v9"
OUTPUT_SCHEMA = "full-fusion-review-v1"
DEFAULT_INPUT = Path(".work/batch-2025/fused_content.json")
DEFAULT_CACHE_DIR = Path(".work/batch-2025/full-fusion-review-cache")
DEFAULT_OUTPUT = Path(".work/batch-2025/full_fusion_review.json")

MIN_COMBINED_EVIDENCE_RATIO = 0.90
MIN_CHILD_EVIDENCE_RATIO = 0.44
MIN_COURSEWARE_EVIDENCE_RATIO = 0.44
MIN_VIDEO_VISUAL_EVIDENCE_RATIO = 0.12
MIN_CHUNK_CHARACTERS = 60
MAX_SPLIT_DEPTH = 10
MAX_DIRECT_EVIDENCE_CHARACTERS = 48_000
SPLITTABLE_FINISH_REASONS = frozenset({"content_filter", "length"})
FORBIDDEN_FUSION_LABELS = (
    "完整语音识别底稿", "完整画面文字", "OCR补充", "OCR 补充",
    "既有GPT逐文件校对正文", "完整文档文字", "完整内嵌媒体语音",
    "完整内嵌媒体画面文字", "ASR底稿", "ASR 底稿", "OCR底稿", "OCR 底稿",
    "OCR原始文字", "整页OCR原始文字", "文档原生文字、脚注及图片交叉校正",
    "原生文字、备注、图表及替代文本", "嵌入工作簿完整单元格",
)
_EVIDENCE_SCAFFOLD = re.compile(
    r"【(?:整页OCR原始文字|文档原生文字、脚注及图片交叉校正|"
    r"嵌入工作簿完整单元格|[^】]+：(?:OCR原始文字|"
    r"原生文字、备注、图表及替代文本))】\s*"
)

SYSTEM_PROMPT = """你是中文音视频全文融合校对员。你的任务是逐字逐项复核后形成无损全文，不是摘要、提纲、改写或节选。

必须逐项覆盖 ASR 与 OCR 中能够成立且与文件主题相关的全部内容：每个事实、数字、日期、时刻、比例、数量、姓名、职务、机构、地点、标题、定义、观点、步骤、例子、例外、条件、因果、引语和画面专有文字都要保留。ASR 是语音叙述主线，OCR 用于校正错字并补入语音没有讲出的课件、表格、清单和画面核心内容。同一内容只在确属重复时合并一次；冲突时结合上下文选择有依据的写法。明显 OCR 乱码、水印、网页编号、无关报刊背景和不属于课程正文的环境文字必须删除，不能为了凑字数原样倾倒 OCR。不得补充证据中没有的事实，不得概括压缩，不得把多个独立细节合并成笼统结论。

只修正错别字、同音误识、标点、断句和紊乱语序，删除口头填充词、确定无关的 OCR 噪声及真正重复。保持原有叙述次序和信息粒度。阿拉伯数字、百分号、计量单位及专有名称原则上保持源文写法，以便完整性核验。绝不能为了行文简短而删项，也不得输出由大量分号碎片组成的 OCR 原样堆砌。

输出必须是一个 JSON 对象，且只能有 paragraphs 字段；paragraphs 是按原文顺序排列的非空中文段落字符串数组。不要输出 Markdown、说明、摘要标题、处理过程、遗漏声明或时间戳标记。最终正文不得出现“ASR底稿”“OCR底稿”“完整语音识别底稿”“完整画面文字”“OCR补充”等证据分区或处理过程标签；只输出融合校对完成后的课程正文。"""

_NUMERIC_FACT = re.compile(
    r"(?:"
    r"\d{2,4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"
    r"|\d+(?:\.\d+)?\s*(?:%|％|万人次|万人|人次|千伏|公里|分钟|小时|人|项|条|个|步|阶段|元|米|吨|台|户|倍|岁|年|月|日)"
    r"|\d{1,2}\s*[:：]\s*\d{2}"
    r")"
)
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class FusionReviewError(RuntimeError):
    """Safe-to-display failure whose message never contains provider data."""


class IncompleteResponseError(FusionReviewError):
    """The provider did not complete a response and it must not be cached."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class LosslessAuditError(FusionReviewError):
    """The proposed result appears to have omitted source information."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an authorization header to another address.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    """Atomically write UTF-8 JSON without involving media dependencies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configured_endpoint(value: str | None = None) -> str:
    """Resolve an allow-listed official OpenAI-compatible MiMo endpoint."""
    candidate = (value or os.environ.get("MIMO_API_BASE_URL") or ENDPOINT).strip().rstrip("/")
    if candidate.endswith("/v1"):
        candidate += "/chat/completions"
    parsed = urllib.parse.urlsplit(candidate)
    if (candidate not in ALLOWED_ENDPOINTS or parsed.scheme != "https"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise FusionReviewError("MiMo 接口地址不在允许的小米官方地址清单中。")
    return candidate


def acquire_api_key(*, env: dict[str, str] | None = None,
                    stdin_isatty: bool | None = None) -> str:
    """Read a credential from the environment or an interactive hidden prompt."""
    environment = os.environ if env is None else env
    key = environment.get("MIMO_API_KEY", "").strip()
    if key:
        return key
    interactive = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    if not interactive:
        raise FusionReviewError("需要 MIMO_API_KEY 环境变量或终端隐藏输入。")
    key = getpass.getpass("MiMo API key (hidden): ").strip()
    if not key:
        raise FusionReviewError("MiMo API 密钥为空。")
    return key


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _task_identity(*, source_name: str, source_sha256: str | None, kind: str | None,
                   asr: list[str], ocr: list[str], chunk_path: list[int],
                   root_input_sha256: str | None) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "source_name": source_name,
        "source_sha256": source_sha256,
        "kind": kind,
        "asr": asr,
        "ocr": ocr,
        "chunk_path": chunk_path,
        "root_input_sha256": root_input_sha256,
    }


def _strings(value: Any, *, field: str, source_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source_name} 的 {field} 必须是字符串数组。")
    return list(value)


def make_task(record: dict[str, Any]) -> dict[str, Any]:
    """Create a stable task identity from ASR/OCR only."""
    if (not isinstance(record, dict) or not isinstance(record.get("source_name"), str)
            or not record["source_name"].strip()):
        raise ValueError("融合输入条目缺少 source_name。")
    name = record["source_name"]
    sections = record.get("sections")
    if not isinstance(sections, dict):
        raise ValueError(f"{name} 缺少 sections。")
    asr = _strings(sections.get("ASR 结果"), field="ASR 结果", source_name=name)
    ocr = _strings(sections.get("OCR 结果"), field="OCR 结果", source_name=name)
    if not any(item.strip() for item in asr + ocr):
        raise ValueError(f"{name} 的 ASR/OCR 均为空。")
    identity = _task_identity(
        source_name=name, source_sha256=record.get("source_sha256"),
        kind=record.get("kind"),
        asr=asr, ocr=ocr, chunk_path=[], root_input_sha256=None)
    return {
        "source_name": name,
        "source_sha256": record.get("source_sha256"),
        "kind": record.get("kind"),
        "asr": asr,
        "ocr": ocr,
        "chunk_path": [],
        "root_input_sha256": None,
        "input_sha256": hashlib.sha256(_canonical_json(identity)).hexdigest(),
    }


def _evidence_characters(task: dict[str, Any]) -> int:
    return len(re.findall(
        r"[0-9A-Za-z\u3400-\u9fff]", "".join(task["asr"] + task["ocr"])))


def _split_stream(values: list[str]) -> tuple[list[str], list[str]]:
    """Split near the midpoint, preferring sentence/line semantic boundaries."""
    text = "\n".join(value for value in values if value)
    if len(text) < 2:
        return ([text] if text else []), []
    midpoint = len(text) / 2
    # Sentence terminators and original paragraph boundaries are strongest.
    candidates = [index + 1 for index, character in enumerate(text[:-1])
                  if character in "。！？!?；;\n"]
    # A very long sentence still needs a deterministic fallback.  Prefer a
    # clause boundary before splitting at an arbitrary character.
    if not candidates:
        candidates = [index + 1 for index, character in enumerate(text[:-1])
                      if character in "，,、：: "]
    if not candidates:
        candidates = [len(text) // 2]
    cut = min(candidates, key=lambda value: (abs(value - midpoint), value))
    left, right = text[:cut].strip(), text[cut:].strip()
    return ([left] if left else []), ([right] if right else [])


def _chunk_task(parent: dict[str, Any], *, asr: list[str], ocr: list[str],
                side: int) -> dict[str, Any]:
    chunk_path = [*parent.get("chunk_path", []), side]
    root_hash = parent.get("root_input_sha256") or parent["input_sha256"]
    identity = _task_identity(
        source_name=parent["source_name"],
        source_sha256=parent.get("source_sha256"),
        kind=parent.get("kind"),
        asr=asr, ocr=ocr, chunk_path=chunk_path,
        root_input_sha256=root_hash,
    )
    return {
        "source_name": parent["source_name"],
        "source_sha256": parent.get("source_sha256"),
        "kind": parent.get("kind"),
        "asr": asr,
        "ocr": ocr,
        "chunk_path": chunk_path,
        "root_input_sha256": root_hash,
        "input_sha256": hashlib.sha256(_canonical_json(identity)).hexdigest(),
    }


def split_task(task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Bisect ASR and OCR independently into matching relative-position blocks."""
    asr_left, asr_right = _split_stream(task["asr"])
    ocr_left, ocr_right = _split_stream(task["ocr"])
    if not (asr_left or ocr_left) or not (asr_right or ocr_right):
        return None
    left = _chunk_task(task, asr=asr_left, ocr=ocr_left, side=0)
    right = _chunk_task(task, asr=asr_right, ocr=ocr_right, side=1)
    # A malformed/non-reducing split must never drive infinite recursion.
    if (_evidence_characters(left) >= _evidence_characters(task)
            or _evidence_characters(right) >= _evidence_characters(task)):
        return None
    return left, right


def load_tasks(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load tasks in source order, rejecting duplicates and malformed evidence."""
    document = read_json(Path(path))
    records = document.get("files") if isinstance(document, dict) else None
    if not isinstance(records, list):
        raise ValueError("fused_content.json 缺少 files 数组。")
    declared_count = document.get("file_count")
    if declared_count is not None and declared_count != len(records):
        raise ValueError("fused_content.json 的 file_count 与 files 数量不一致。")
    tasks = [make_task(record) for record in records]
    names = [task["source_name"] for task in tasks]
    if len(set(names)) != len(names):
        raise ValueError("fused_content.json 含重复 source_name。")
    metadata = {
        "source_schema_version": document.get("schema_version"),
        "source_file_count": document.get("file_count"),
        "source_document_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    }
    return metadata, tasks


def build_messages(task: dict[str, Any], retry_feedback: str | None = None) -> list[dict[str, str]]:
    """Build the full, unabridged prompt for one source."""
    evidence = {
        "文件名": task["source_name"],
        "ASR 结果（完整）": task["asr"],
        "OCR 结果（完整）": task["ocr"],
    }
    asr_characters = len(re.findall(
        r"[0-9A-Za-z\u3400-\u9fff]", "".join(task["asr"])
    ))
    if task.get("kind") == "video" and asr_characters < 50:
        evidence["屏幕录制特别要求"] = (
            "本文件语音极少或无有效语音，必须以OCR还原画面中能够确认的系统名称、"
            "菜单、字段、按钮、状态、日期和操作过程；合并重复帧并校正乱码，但不得"
            "因ASR不足而返回空正文。"
        )
    chunk_path = task.get("chunk_path", [])
    if chunk_path:
        evidence["当前证据块"] = {
            "递归层级": len(chunk_path),
            "相对位置": "→".join("前半" if side == 0 else "后半"
                                for side in chunk_path),
            "要求": "本次数组是该相对位置的完整证据，仍须无损保留全部细节。",
        }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, indent=2)},
    ]
    if retry_feedback:
        messages.append({
            "role": "user",
            "content": (
                "上一稿未通过无损完整性审计。请从头重新生成，不得摘要或缩写。"
                "必须保留两路证据中的全部可确认信息，并逐一保留所有数字、时间、"
                "比例、型号、条目、例子和例外。审计反馈：" + retry_feedback
            ),
        })
    return messages


def build_payload(task: dict[str, Any], retry_feedback: str | None = None) -> dict[str, Any]:
    return {
        "model": MODEL,
        "messages": build_messages(task, retry_feedback),
        "max_completion_tokens": 65536,
        # This is a constrained, source-bound proofreading transform.  Hidden
        # reasoning adds latency and has triggered avoidable filtering, while
        # the deterministic lossless audit still guards every returned block.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "stream": False,
    }


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    allowed = ("prompt_tokens", "completion_tokens", "total_tokens")
    return {key: int(value[key]) for key in allowed
            if isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
            and value[key] >= 0}


def _parse_content(content: str) -> list[str]:
    cleaned = _CODE_FENCE.sub("", content.strip()).strip()
    try:
        value = json.loads(cleaned)
    except (ValueError, TypeError):
        raise FusionReviewError("MiMo 返回正文不是约定的 JSON。") from None
    if not isinstance(value, dict) or set(value) != {"paragraphs"}:
        raise FusionReviewError("MiMo 返回正文必须只包含 paragraphs 字段。")
    paragraphs = value["paragraphs"]
    if (not isinstance(paragraphs, list)
            or not all(isinstance(item, str) and item.strip() for item in paragraphs)):
        raise FusionReviewError("MiMo 返回的 paragraphs 必须是字符串数组。")
    return [item.strip() for item in paragraphs]


def validate_response(response: Any, *, api_key: str) -> dict[str, Any]:
    """Accept only a complete, schema-conforming, non-credential-echo response."""
    if not isinstance(response, dict):
        raise FusionReviewError("MiMo 响应结构不正确。")
    # Check the complete decoded provider object before selecting cache fields.
    serialized = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if api_key and api_key in serialized:
        raise FusionReviewError("MiMo 响应异常地包含认证信息，结果已拒绝保存。")
    choices = response.get("choices")
    if (response.get("error") or not isinstance(choices, list) or len(choices) != 1
            or not isinstance(choices[0], dict)):
        raise FusionReviewError("MiMo 响应缺少唯一有效结果。")
    choice = choices[0]
    reason = choice.get("finish_reason")
    if reason != "stop":
        safe_reason = reason if isinstance(reason, str) and reason in {
            "length", "content_filter", "tool_calls"
        } else "unknown"
        raise IncompleteResponseError(
            f"MiMo 未完整完成（finish_reason={safe_reason}），结果不得缓存。",
            reason=safe_reason)
    if response.get("model") != MODEL:
        raise FusionReviewError("MiMo 响应模型与指定模型不一致。")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise FusionReviewError("MiMo 响应缺少文本内容。")
    paragraphs = _parse_content(message["content"])
    response_id = response.get("id")
    if response_id is not None and not isinstance(response_id, str):
        raise FusionReviewError("MiMo 响应 ID 结构不正确。")
    return {
        "paragraphs": paragraphs,
        "finish_reason": "stop",
        "response_id": response_id,
        "usage": _safe_usage(response.get("usage")),
    }


def _compact_length(values: Iterable[str]) -> int:
    return len(re.sub(r"\s+", "", "".join(values)))


def _numeric_facts(values: Iterable[str]) -> set[str]:
    text = "\n".join(values).replace("％", "%")
    return {re.sub(r"\s+", "", match).replace("％", "%")
            for match in _NUMERIC_FACT.findall(text)}


def audit_lossless(task: dict[str, Any], paragraphs: list[str], *,
                   minimum_ratio: float = MIN_COMBINED_EVIDENCE_RATIO) -> dict[str, Any]:
    """Reject obvious summaries and loss of machine-checkable numeric facts.

    Semantic preservation remains primarily the model's instructed job.  This
    deterministic guard catches the two dangerous mechanical failure modes:
    a short synopsis and silently dropped dates/counts/percentages.
    """
    if not 0 < minimum_ratio <= 1:
        raise ValueError("minimum_ratio 必须在 0 到 1 之间。")
    joined = "\n".join(paragraphs)
    present = [label for label in FORBIDDEN_FUSION_LABELS if label in joined]
    if present:
        raise LosslessAuditError(
            "最终融合正文含证据底稿或处理过程标签：" + "、".join(present)
        )
    # Evidence-origin labels are useful to the model but are not course facts
    # and must neither be demanded as exam anchors nor inflate the denominator.
    audit_asr = [_EVIDENCE_SCAFFOLD.sub("", value) for value in task["asr"]]
    audit_ocr = [_EVIDENCE_SCAFFOLD.sub("", value) for value in task["ocr"]]
    evidence_status: dict[str, Any] = {}
    if task.get("kind") != "video":
        audit_ocr_text = "\n".join(audit_ocr)
        asr_keys = {
            (item["kind"], item["canonical"])
            for item in extract_anchors(audit_asr)
        }
        exclusions = []
        for item in extract_anchors(audit_ocr):
            key = item["kind"], item["canonical"]
            isolated_ocr_code = (
                key not in asr_keys and item["kind"] == "model"
                and re.fullmatch(r"[A-Z]{1,12}\d[A-Z0-9]*", item["canonical"])
                and not re.search(
                    r"(?:KV|KW|MW|GW|KWH|MWH|GHZ|MHZ|GB|TB)$",
                    item["canonical"], re.I,
                )
            )
            raw_position = audit_ocr_text.find(item["value"])
            raw_context = (audit_ocr_text[max(0, raw_position - 80):raw_position + 80]
                           if raw_position >= 0 else "")
            watermark_code = (
                item["kind"] == "model"
                and any(marker in raw_context.casefold()
                        for marker in ("昵图网", "nipic.com", "www.", "by:"))
            )
            malformed_number_fragment = (
                item["kind"] == "number"
                and bool(re.search(r"[\u3400-\u9fff%％]$", item["value"].strip()))
                and bool(re.search(
                    r"\d\s+" + re.escape(item["value"]), audit_ocr_text
                ))
            )
            cross_column_number = (
                item["kind"] == "number"
                and item["value"].strip().endswith("家")
                and bool(re.search(
                    r"年\s*" + re.escape(item["value"].strip()) + r"监管",
                    raw_context,
                ))
            )
            if (key not in asr_keys
                    and (isolated_ocr_code or watermark_code
                         or malformed_number_fragment or cross_column_number)):
                exclusions.append({
                    "kind": item["kind"], "canonical": item["canonical"],
                    "reason": ("malformed_duplicate"
                               if malformed_number_fragment or cross_column_number
                               else "ocr_noise"),
                })
        if exclusions:
            evidence_status["verified_ocr_noise_anchors"] = exclusions
    effective_minimum = (
        minimum_ratio if task.get("kind") == "video"
        else min(minimum_ratio, MIN_COURSEWARE_EVIDENCE_RATIO)
    )
    report = check_file({
        "source_name": task["source_name"],
        "kind": task.get("kind"),
        "evidence_status": evidence_status,
        "sections": {
            "ASR 结果": audit_asr,
            "OCR 结果": audit_ocr,
            "GPT 融合校对结果": paragraphs,
        },
    }, min_length_retention=effective_minimum, min_fused_chars=0,
       min_visual_retention=MIN_VIDEO_VISUAL_EVIDENCE_RATIO,
       # Video-frame OCR contains playback clocks and unstable alphanumeric
       # debris.  The complete raw OCR remains independently searchable; the
       # final prose is guarded by ASR anchors plus its visual-length floor.
       require_ocr_anchors=task.get("kind") != "video")
    if not report["passed"]:
        anchors = report["anchors"]
        preview = "、".join(
            f"{item['kind']}={item['value']}"
            for item in anchors["missing"][:12]
        )
        raise LosslessAuditError(
            "全文完整性检查失败："
            f"内容保留率 {report['length_retention_ratio']:.3f}"
            f"（最低 {report['minimum_length_retention_ratio']:.2f}），"
            f"缺少考试锚点 {len(anchors['missing'])} 个"
            + (f"（{preview}）" if preview else "") + "。"
        )
    return {
        "combined_source_characters": report["combined_source_length"],
        "output_characters": report["fused_length"],
        "length_retention_ratio": report["length_retention_ratio"],
        "exam_anchor_count": report["anchors"]["total"],
        "exam_anchors_preserved": True,
    }


def _request_json(payload: dict[str, Any], api_key: str, *, endpoint: str,
                  timeout: float = 180.0, max_attempts: int = 4) -> dict[str, Any]:
    """Call MiMo without exposing request/response bodies in any exception."""
    if max_attempts < 1:
        raise ValueError("max_attempts 必须为正整数。")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(max_attempts):
        request = urllib.request.Request(endpoint, data=body, headers={
            "api-key": api_key,
            "Content-Type": "application/json",
        }, method="POST")
        delay = min(2 ** attempt, 15)
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise FusionReviewError("MiMo 响应超过预期大小。")
            if api_key and api_key.encode("utf-8") in raw:
                raise FusionReviewError("MiMo 响应异常地包含认证信息，结果已拒绝保存。")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise FusionReviewError("MiMo 返回内容不是有效 JSON。") from None
            if not isinstance(value, dict):
                raise FusionReviewError("MiMo 响应结构不正确。")
            return value
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After") if error.headers else None
            error.close()  # Deliberately never read the provider error body.
            if status not in (429,) and not 500 <= status <= 599:
                hint = "鉴权失败，请检查密钥及权限。" if status in (401, 403) else "请求被拒绝。"
                raise FusionReviewError(f"MiMo HTTP {status}：{hint}") from None
            if attempt + 1 >= max_attempts:
                raise FusionReviewError(
                    f"MiMo HTTP {status}：已达到 {max_attempts} 次尝试上限。") from None
            if retry_after:
                try:
                    delay = min(30.0, max(delay, float(retry_after)))
                except ValueError:
                    pass
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, OSError):
            if attempt + 1 >= max_attempts:
                raise FusionReviewError(
                    f"MiMo 网络请求失败，已达到 {max_attempts} 次尝试上限。") from None
        time.sleep(delay)
    raise AssertionError("unreachable")


def cache_path(cache_dir: Path, task: dict[str, Any]) -> Path:
    file_key = hashlib.sha256(task["source_name"].encode("utf-8")).hexdigest()[:20]
    return Path(cache_dir) / file_key / f"{task['input_sha256']}.json"


def _cache_record(task: dict[str, Any], validated: dict[str, Any],
                  audit: dict[str, Any], *, strategy: str = "single-call",
                  child_input_sha256: list[str] | None = None) -> dict[str, Any]:
    record = {
        "schema_version": CACHE_SCHEMA,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "source_name": task["source_name"],
        "source_sha256": task.get("source_sha256"),
        "input_sha256": task["input_sha256"],
        "root_input_sha256": task.get("root_input_sha256"),
        "chunk_path": list(task.get("chunk_path", [])),
        "finish_reason": "stop",
        "strategy": strategy,
        "paragraphs": validated["paragraphs"],
        "response_id": validated.get("response_id"),
        "usage": validated.get("usage", {}),
        "lossless_audit": audit,
    }
    if child_input_sha256 is not None:
        record["child_input_sha256"] = list(child_input_sha256)
    return record


def load_cached(task: dict[str, Any], cache_dir: Path) -> dict[str, Any] | None:
    """Return only a cache entry tied to this exact task and a complete call."""
    path = cache_path(cache_dir, task)
    try:
        value = read_json(path)
        if not isinstance(value, dict):
            return None
        if any((
            value.get("schema_version") != CACHE_SCHEMA,
            value.get("model") != MODEL,
            value.get("prompt_version") != PROMPT_VERSION,
            value.get("source_name") != task["source_name"],
            value.get("source_sha256") != task.get("source_sha256"),
            value.get("input_sha256") != task["input_sha256"],
            value.get("root_input_sha256") != task.get("root_input_sha256"),
            value.get("chunk_path") != list(task.get("chunk_path", [])),
            value.get("finish_reason") != "stop",
        )):
            return None
        paragraphs = value.get("paragraphs")
        if (not isinstance(paragraphs, list)
                or not all(isinstance(item, str) and item.strip() for item in paragraphs)):
            return None
        deferred = value.get("lossless_audit", {}).get("deferred_to_parent") is True
        if not paragraphs:
            if not task.get("chunk_path") or not deferred:
                return None
            return value
        audit_lossless(
            task, paragraphs,
            minimum_ratio=(MIN_CHILD_EVIDENCE_RATIO
                           if task.get("chunk_path")
                           else MIN_COMBINED_EVIDENCE_RATIO),
        )
        return value
    except (OSError, ValueError, TypeError, KeyError, LosslessAuditError):
        return None


def has_descendant_cache(task: dict[str, Any], cache_dir: Path) -> bool:
    """Return whether a validated cache tree exists below this task node."""
    directory = cache_path(cache_dir, task).parent
    root_hash = task.get("root_input_sha256") or task["input_sha256"]
    prefix = list(task.get("chunk_path", []))
    for path in directory.glob("*.json"):
        try:
            value = read_json(path)
        except (OSError, ValueError):
            continue
        chunk_path = value.get("chunk_path")
        if (value.get("schema_version") == CACHE_SCHEMA
                and value.get("model") == MODEL
                and value.get("prompt_version") == PROMPT_VERSION
                and value.get("root_input_sha256") == root_hash
                and isinstance(chunk_path, list)
                and len(chunk_path) > len(prefix)
                and chunk_path[:len(prefix)] == prefix):
            return True
    return False


RequestFunction = Callable[..., dict[str, Any]]


def _merge_usage(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for record in records:
        for key, value in record.get("usage", {}).items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                merged[key] = merged.get(key, 0) + value
    return merged


def _store_cache(task: dict[str, Any], record: dict[str, Any],
                 cache_dir: Path, key: str) -> None:
    serialized = json.dumps(record, ensure_ascii=False)
    if key in serialized:
        raise FusionReviewError("待缓存结果异常地包含认证信息，结果已拒绝保存。")
    atomic_json(cache_path(cache_dir, task), record)


def _request_one(task: dict[str, Any], key: str, *, endpoint: str | None,
                 request_fn: RequestFunction,
                 retry_feedback: str | None = None) -> dict[str, Any]:
    try:
        response = request_fn(build_payload(task, retry_feedback), key,
                              endpoint=configured_endpoint(endpoint))
    except Exception as exc:
        # Third-party/client exceptions may echo arguments.  Never relay them.
        if isinstance(exc, FusionReviewError):
            message = str(exc).replace(key, "[REDACTED]")
            if isinstance(exc, IncompleteResponseError):
                raise IncompleteResponseError(message, reason=exc.reason) from None
            raise type(exc)(message) from None
        raise FusionReviewError("MiMo 请求失败；已隐藏底层异常详情。") from None
    return validate_response(response, api_key=key)


def _review_recursive(task: dict[str, Any], cache_dir: Path, key: str, *,
                      endpoint: str | None, resume: bool,
                      request_fn: RequestFunction, depth: int,
                      max_split_depth: int,
                      min_chunk_characters: int) -> tuple[dict[str, Any], bool]:
    if resume:
        cached = load_cached(task, cache_dir)
        if cached is not None:
            return cached, True

    minimum_ratio = (MIN_CHILD_EVIDENCE_RATIO if depth else
                     MIN_COMBINED_EVIDENCE_RATIO)

    # Large Chinese OCR streams can exceed the provider's input/output context
    # before a structured finish_reason is returned.  Split them proactively so
    # a predictable HTTP 400 cannot waste a request or defeat resumability.
    if (depth < max_split_depth
            and ((resume and has_descendant_cache(task, cache_dir))
                 or _evidence_characters(task) > MAX_DIRECT_EVIDENCE_CHARACTERS)):
        children = split_task(task)
        if children is not None:
            child_records: list[dict[str, Any]] = []
            for child in children:
                record, _ = _review_recursive(
                    child, cache_dir, key, endpoint=endpoint, resume=resume,
                    request_fn=request_fn, depth=depth + 1,
                    max_split_depth=max_split_depth,
                    min_chunk_characters=min_chunk_characters,
                )
                child_records.append(record)
            paragraphs = [paragraph for record in child_records
                          for paragraph in record["paragraphs"]]
            parent_audit = audit_lossless(
                task, paragraphs, minimum_ratio=minimum_ratio,
            )
            combined = {
                "paragraphs": paragraphs,
                "finish_reason": "stop",
                "response_id": None,
                "usage": _merge_usage(child_records),
            }
            parent_record = _cache_record(
                task, combined, parent_audit, strategy="proactive-split",
                child_input_sha256=[child["input_sha256"] for child in children],
            )
            _store_cache(task, parent_record, cache_dir, key)
            return parent_record, False

    fallback_error: IncompleteResponseError | LosslessAuditError | None = None
    last_validated: dict[str, Any] | None = None
    try:
        validated = _request_one(task, key, endpoint=endpoint,
                                 request_fn=request_fn)
        last_validated = validated
        audit = audit_lossless(
            task, validated["paragraphs"], minimum_ratio=minimum_ratio,
        )
    except IncompleteResponseError as exc:
        # Only provider truncation/filter outcomes authorize semantic splitting.
        # HTTP/auth/network/JSON/model errors remain terminal for this attempt.
        if exc.reason not in SPLITTABLE_FINISH_REASONS:
            raise
        fallback_error = exc
    except LosslessAuditError as exc:
        # A focused second pass is usually cheaper and more coherent than
        # immediately bisecting a document merely because one value was
        # omitted.  The exact safe audit feedback tells the model what to
        # restore; a second failure still falls back to semantic splitting.
        try:
            validated = _request_one(
                task, key, endpoint=endpoint, request_fn=request_fn,
                retry_feedback=str(exc),
            )
            last_validated = validated
            audit = audit_lossless(
                task, validated["paragraphs"], minimum_ratio=minimum_ratio,
            )
        except IncompleteResponseError as retry_exc:
            if retry_exc.reason not in SPLITTABLE_FINISH_REASONS:
                raise
            fallback_error = retry_exc
        except LosslessAuditError as retry_exc:
            fallback_error = retry_exc
        else:
            record = _cache_record(task, validated, audit,
                                   strategy="single-call-repair")
            _store_cache(task, record, cache_dir, key)
            return record, False
    else:
        record = _cache_record(task, validated, audit)
        _store_cache(task, record, cache_dir, key)
        return record, False

    at_minimum = (depth >= max_split_depth
                  or _evidence_characters(task) <= min_chunk_characters)
    if (at_minimum and depth > 0 and last_validated is not None
            and not last_validated["paragraphs"]):
        # A leaf containing only OCR debris may correctly produce no prose.
        # Its parent/root audit still enforces aggregate retention and anchors,
        # so an entire file can never pass merely by returning empty chunks.
        deferred_audit = {
            "combined_source_characters": _evidence_characters(task),
            "output_characters": 0,
            "length_retention_ratio": 0.0,
            "exam_anchor_count": 0,
            "exam_anchors_preserved": False,
            "deferred_to_parent": True,
        }
        record = _cache_record(
            task, last_validated, deferred_audit, strategy="deferred-noise"
        )
        _store_cache(task, record, cache_dir, key)
        return record, False
    if at_minimum:
        raise fallback_error
    children = split_task(task)
    if children is None:
        raise fallback_error

    child_records: list[dict[str, Any]] = []
    for child in children:
        record, _ = _review_recursive(
            child, cache_dir, key, endpoint=endpoint, resume=resume,
            request_fn=request_fn, depth=depth + 1,
            max_split_depth=max_split_depth,
            min_chunk_characters=min_chunk_characters,
        )
        child_records.append(record)

    # Relative-position children are already ordered, so concatenation restores
    # the parent's sequence without fuzzy boundary deletion.
    paragraphs = [paragraph for record in child_records
                  for paragraph in record["paragraphs"]]
    parent_audit = audit_lossless(
        task, paragraphs, minimum_ratio=minimum_ratio,
    )
    combined = {
        "paragraphs": paragraphs,
        "finish_reason": "stop",
        "response_id": None,
        "usage": _merge_usage(child_records),
    }
    parent_record = _cache_record(
        task, combined, parent_audit, strategy="recursive-split",
        child_input_sha256=[child["input_sha256"] for child in children],
    )
    _store_cache(task, parent_record, cache_dir, key)
    return parent_record, False


def review_one(task: dict[str, Any], cache_dir: Path, api_key: str, *,
               endpoint: str | None = None, resume: bool = True,
               request_fn: RequestFunction = _request_json,
               max_split_depth: int = MAX_SPLIT_DEPTH,
               min_chunk_characters: int = MIN_CHUNK_CHARACTERS,
               ) -> tuple[dict[str, Any], bool]:
    """Review one file, returning ``(safe_cache_record, cache_hit)``."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise FusionReviewError("未提供 MiMo API 密钥。")
    if max_split_depth < 0:
        raise ValueError("max_split_depth 不能小于 0。")
    if min_chunk_characters < 1:
        raise ValueError("min_chunk_characters 必须为正整数。")
    key = api_key.strip()
    return _review_recursive(
        task, Path(cache_dir), key, endpoint=endpoint, resume=resume,
        request_fn=request_fn, depth=0, max_split_depth=max_split_depth,
        min_chunk_characters=min_chunk_characters,
    )


def merge_results(metadata: dict[str, Any], tasks: list[dict[str, Any]],
                  results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build one stable final artifact in the original source order."""
    missing = [task["source_name"] for task in tasks
               if task["source_name"] not in results]
    if missing:
        raise ValueError(f"不能合并：仍缺少 {len(missing)} 个文件结果。")
    files = []
    for task in tasks:
        result = results[task["source_name"]]
        if result.get("input_sha256") != task["input_sha256"] or result.get("finish_reason") != "stop":
            raise ValueError(f"不能合并未验证结果：{task['source_name']}")
        files.append({
            "source_name": task["source_name"],
            "source_sha256": task.get("source_sha256"),
            "input_sha256": task["input_sha256"],
            "sections": {"GPT 融合校对结果": list(result["paragraphs"])},
        })
    return {
        "schema_version": OUTPUT_SCHEMA,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "source_document_sha256": metadata.get("source_document_sha256"),
        "file_count": len(files),
        "files": files,
    }


def run_pipeline(input_path: Path, cache_dir: Path, output_path: Path, *,
                 execute: bool = False, workers: int = 2, resume: bool = True,
                 endpoint: str | None = None, api_key: str | None = None,
                 request_fn: RequestFunction = _request_json,
                 progress: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Plan or execute the corpus; default mode performs no API calls."""
    if not 1 <= workers <= 8:
        raise ValueError("workers 必须在 1 到 8 之间。")
    metadata, tasks = load_tasks(Path(input_path))
    results: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    if resume:
        for task in tasks:
            cached = load_cached(task, cache_dir)
            if cached is None:
                pending.append(task)
            else:
                results[task["source_name"]] = cached
    else:
        pending = list(tasks)

    plan = {
        "mode": "execute" if execute else "plan",
        "total": len(tasks),
        "cached": len(results),
        "pending": len(pending),
        "output_ready": not pending,
    }
    if not execute:
        # A dry run may merge an already complete cache, but never contacts the API.
        if not pending:
            atomic_json(Path(output_path), merge_results(metadata, tasks, results))
        return plan

    key = api_key.strip() if isinstance(api_key, str) else ""
    if pending and not key:
        key = acquire_api_key()
    failures: dict[str, str] = {}

    def announce(message: str) -> None:
        if progress:
            progress(message)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="full-fusion") as executor:
        futures = {
            executor.submit(review_one, task, cache_dir, key,
                            endpoint=endpoint, resume=resume,
                            request_fn=request_fn): task
            for task in pending
        }
        for future in as_completed(futures):
            task = futures[future]
            name = task["source_name"]
            try:
                result, hit = future.result()
                results[name] = result
                announce(f"[FUSION {'CACHED' if hit else 'DONE'}] {name}")
            except Exception as exc:
                # Persist no provider body and do not expose an echoed key.
                safe = str(exc).replace(key, "[REDACTED]") if key else str(exc)
                failures[name] = f"{type(exc).__name__}: {safe}"
                announce(f"[FUSION FAILED] {name}: {failures[name]}")
    key = ""
    if failures:
        raise FusionReviewError(
            f"有 {len(failures)} 个文件失败；有效单文件缓存已保留，可直接续传。")
    merged = merge_results(metadata, tasks, results)
    atomic_json(Path(output_path), merged)
    return {**plan, "cached": len(tasks) - len(pending), "pending": 0,
            "output_ready": True, "output": str(Path(output_path).resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按文件调用 MiMo Pro 生成无损 ASR/OCR 全文融合校对；默认仅规划。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--execute", action="store_true",
                        help="显式允许调用 API；缺省时绝不调用")
    parser.add_argument("--no-resume", action="store_true",
                        help="忽略已有缓存并重新请求")
    parser.add_argument("--endpoint", help="仅接受小米官方允许清单中的接口")
    args = parser.parse_args(argv)
    # Planning mode does not even inspect the credential environment variable.
    key_for_redaction = os.environ.get("MIMO_API_KEY", "") if args.execute else ""
    try:
        result = run_pipeline(
            args.input.resolve(), args.cache_dir.resolve(), args.output.resolve(),
            execute=args.execute, workers=args.workers,
            resume=not args.no_resume, endpoint=args.endpoint,
            progress=lambda message: print(message, flush=True),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (OSError, ValueError, FusionReviewError, KeyError) as exc:
        message = str(exc).replace(key_for_redaction, "[REDACTED]") if key_for_redaction else str(exc)
        print(f"ERROR {type(exc).__name__}: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
