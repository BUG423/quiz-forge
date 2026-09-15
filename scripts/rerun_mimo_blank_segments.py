#!/usr/bin/env python3
"""Audit and optionally re-run explicitly approved blank MiMo ASR segments.

The original ``asr.json`` files are immutable.  SenseVoice and Whisper
``speech_detected`` records are collected as candidates, but local ASR output
is not trusted as authority: by default the tool only writes a
``local_hallucination_risk`` audit.  A candidate is sent to MiMo only when one
of its exact review IDs is supplied through ``--approved-review-ids``.  An
approved original interval is re-segmented near quiet points into roughly
12--18 second children and emitted as a standalone patch.

Credentials are accepted only from ``MIMO_API_KEY`` or hidden ``getpass`` and
are never written to cache/output.  Failed requests are not cached and cannot
produce a completed patch entry.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from media_pipeline import asr


MODEL = asr.MODEL
SAMPLE_RATE = asr.SAMPLE_RATE
ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
PROCESSOR_VERSION = "mimo-blank-segment-patch/v1"
SEGMENTATION_VERSION = "quiet-200ms-target15s-range12to18-v1"
DEFAULT_SENSEVOICE_QUEUE = ROOT / ".work/first-principles-2026/blank_asr_review.json"
DEFAULT_WHISPER_QUEUE = ROOT / ".work/first-principles-2026/blank_asr_whisper_review.json"
DEFAULT_OUTPUT = ROOT / ".work/first-principles-2026/mimo_blank_segment_patch.json"
DEFAULT_CACHE = ROOT / ".work/first-principles-2026/mimo_blank_rerun/cache"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


class BlankRerunError(RuntimeError):
    """A validation/request error that prevents a completed patch entry."""


Requester = Callable[[np.ndarray, str, str], dict[str, Any]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_pcm(samples: np.ndarray) -> str:
    return hashlib.sha256(samples.astype("<i2", copy=False).tobytes()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
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


def resolve_path(value: str | Path, *, base: Path = ROOT) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def acquire_api_key(
    *, env: dict[str, str] | None = None, stdin_isatty: bool | None = None,
) -> str:
    environment = os.environ if env is None else env
    key = environment.get("MIMO_API_KEY", "").strip()
    if key:
        return key
    interactive = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    if not interactive:
        raise BlankRerunError("需要 MIMO_API_KEY 环境变量或交互终端隐藏输入密钥。")
    key = getpass.getpass("MiMo API key (hidden): ").strip()
    if not key:
        raise BlankRerunError("MiMo API 密钥为空。")
    return key


def _queue_lists(document: Any) -> Iterable[dict[str, Any]]:
    if isinstance(document, list):
        yield from (item for item in document if isinstance(item, dict))
        return
    if not isinstance(document, dict):
        raise BlankRerunError("审计队列根节点必须是对象或数组。")
    found = False
    for key in ("reviews", "mimo_resegmentation_rerun_queue", "rerun_queue", "queue"):
        values = document.get(key)
        if isinstance(values, list):
            found = True
            yield from (item for item in values if isinstance(item, dict))
    if not found:
        raise BlankRerunError("审计文件不含可识别的队列数组。")


def _read_queue_document(queue_path: Path) -> tuple[Path, dict[str, Any]]:
    queue_path = Path(queue_path).resolve(strict=True)
    try:
        document = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BlankRerunError(f"无法读取审计队列：{queue_path}") from exc
    if not isinstance(document, dict):
        raise BlankRerunError(f"审计队列顶层必须是对象：{queue_path}")
    return queue_path, document


def queue_asr_corpus_sha256(paths: Iterable[Path]) -> str:
    bindings = []
    for path in paths:
        queue_path, document = _read_queue_document(path)
        value = document.get("asr_corpus_sha256")
        if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
            raise BlankRerunError(f"审计队列缺少当前 ASR 语料绑定：{queue_path}")
        bindings.append(value)
    if not bindings or len(set(bindings)) != 1:
        raise BlankRerunError("SenseVoice/Whisper 审计的 ASR 语料绑定不一致。")
    return bindings[0]


def task_key(item: dict[str, Any]) -> tuple[str, int, int]:
    try:
        source_sha = item["source_sha256"]
        sample_start = int(item["sample_start"])
        sample_end = int(item["sample_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BlankRerunError("speech_detected 队列项缺少有效 SHA/PCM 坐标。") from exc
    if (not isinstance(source_sha, str) or len(source_sha) != 64
            or any(character not in "0123456789abcdef" for character in source_sha.lower())
            or sample_start < 0 or sample_end <= sample_start):
        raise BlankRerunError("speech_detected 队列项的 SHA/PCM 坐标非法。")
    return source_sha.lower(), sample_start, sample_end


def load_speech_union(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Load both auditors and de-duplicate the union by exact PCM identity."""
    paths = list(paths)
    queue_asr_corpus_sha256(paths)
    merged: dict[tuple[str, int, int], dict[str, Any]] = {}
    for queue_path in paths:
        queue_path, document = _read_queue_document(queue_path)
        auditor = "whisper" if "whisper" in queue_path.name.casefold() else "sensevoice"
        for item in _queue_lists(document):
            if item.get("judgment") != "speech_detected":
                continue
            identity = task_key(item)
            target = merged.setdefault(identity, {"audit_sources": []})
            for key, value in item.items():
                if key not in target or target[key] in (None, "", []):
                    target[key] = value
            evidence = {
                "auditor": auditor,
                "queue_path": str(queue_path),
                "review_id": item.get("review_id"),
                "reason": item.get("reason"),
            }
            if evidence not in target["audit_sources"]:
                target["audit_sources"].append(evidence)
    return [merged[key] for key in sorted(merged)]


def _chunk_count(sample_count: int) -> int:
    """Choose the count whose average is closest to 15 s, preferring 12--18 s."""
    if sample_count <= 0:
        raise BlankRerunError("空段 PCM 长度必须为正数。")
    maximum = max(1, math.ceil(sample_count / (12 * SAMPLE_RATE)))
    choices = []
    for count in range(1, maximum + 1):
        seconds = sample_count / SAMPLE_RATE / count
        violation = max(0.0, 12.0 - seconds) + max(0.0, seconds - 18.0)
        choices.append((violation, abs(seconds - 15.0), count))
    return min(choices)[2]


def _quiet_boundary(samples: np.ndarray, low: int, high: int, nominal: int) -> int:
    if low > high:
        raise BlankRerunError("静音切分约束不可满足。")
    if low == high:
        return low
    radius = max(1, round(0.1 * SAMPLE_RATE))
    step = max(1, round(0.02 * SAMPLE_RATE))
    search_low = max(low, nominal - 3 * SAMPLE_RATE)
    search_high = min(high, nominal + 3 * SAMPLE_RATE)
    candidates = np.arange(search_low, search_high + 1, step, dtype=np.int64)
    if not candidates.size:
        return max(low, min(high, nominal))
    values = samples.astype(np.float64, copy=False)
    squared = values * values
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    left = np.maximum(0, candidates - radius)
    right = np.minimum(len(samples), candidates + radius)
    power = (cumulative[right] - cumulative[left]) / np.maximum(1, right - left)
    minimum = float(np.min(power))
    quiet = candidates[np.isclose(power, minimum, rtol=1e-12, atol=1e-9)]
    return int(quiet[np.argmin(np.abs(quiet - nominal))])


def split_near_silence(
    full_pcm: np.ndarray, sample_start: int, sample_end: int,
) -> list[tuple[int, int]]:
    """Cover the original coordinates exactly with quiet, roughly 15 s cuts."""
    if (full_pcm.dtype != np.int16 or sample_start < 0 or sample_end > len(full_pcm)
            or sample_end <= sample_start):
        raise BlankRerunError("待切分 PCM 数组或坐标非法。")
    length = sample_end - sample_start
    count = _chunk_count(length)
    if count == 1:
        return [(sample_start, sample_end)]
    minimum, maximum = 12 * SAMPLE_RATE, 18 * SAMPLE_RATE
    boundaries = [sample_start]
    for index in range(1, count):
        previous = boundaries[-1]
        remaining = count - index
        low = max(previous + minimum, sample_end - remaining * maximum)
        high = min(previous + maximum, sample_end - remaining * minimum)
        nominal = sample_start + round(index * length / count)
        if low > high:
            # This occurs only for a short interval whose optimal average lies
            # just outside 12--18 s. Preserve exact proportional coverage.
            boundary = max(previous + 1, min(sample_end - remaining, nominal))
        else:
            boundary = _quiet_boundary(full_pcm, low, high, nominal)
        boundaries.append(boundary)
    boundaries.append(sample_end)
    intervals = list(zip(boundaries, boundaries[1:]))
    if (intervals[0][0] != sample_start or intervals[-1][1] != sample_end
            or sum(end - begin for begin, end in intervals) != length
            or any(left[1] != right[0] for left, right in zip(intervals, intervals[1:]))):
        raise BlankRerunError("静音重切分没有完整覆盖原空段。")
    return intervals


def read_pcm(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as handle:
            if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (
                    SAMPLE_RATE, 1, 2):
                raise BlankRerunError("MiMo 规范化音频不是 16kHz/单声道/s16 WAV。")
            frames = handle.readframes(handle.getnframes())
    except (OSError, wave.Error) as exc:
        raise BlankRerunError(f"无法读取 MiMo 规范化 PCM：{path}") from exc
    return np.frombuffer(frames, dtype="<i2").copy()


def _original_context(task: dict[str, Any]) -> dict[str, Any]:
    source_sha, sample_start, sample_end = task_key(task)
    asr_value = task.get("mimo_asr_path")
    asr_path = (resolve_path(asr_value) if asr_value else
                ROOT / ".work/single-video" / source_sha[:16] / "asr.json")
    if not asr_path.is_file():
        raise BlankRerunError(f"原 MiMo ASR 不存在：{asr_path}")
    try:
        original = json.loads(asr_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BlankRerunError(f"原 MiMo ASR 无法读取：{asr_path}") from exc
    if original.get("source_sha256") != source_sha or original.get("model") != MODEL:
        raise BlankRerunError("原 MiMo ASR 的 source SHA 或模型不匹配。")
    if original.get("configuration", {}).get("endpoint") != ENDPOINT:
        raise BlankRerunError("原 MiMo ASR 不是 token-plan-cn 官方 endpoint 结果。")
    matches = [segment for segment in original.get("segments", [])
               if segment.get("sample_start") == sample_start
               and segment.get("sample_end") == sample_end]
    if len(matches) != 1:
        raise BlankRerunError("PCM 坐标无法唯一对应原 MiMo 分段。")
    segment = matches[0]
    if str(segment.get("text", "")).strip():
        raise BlankRerunError("审计目标不是原 MiMo 空文本段。")
    if segment.get("model") != MODEL or segment.get("finish_reason") != "stop":
        raise BlankRerunError("原空段不是完整的指定 MiMo 模型响应。")
    normalized = resolve_path(original.get("normalized_audio_path", ""))
    if not normalized.is_file():
        raise BlankRerunError("原 MiMo 规范化 WAV 不存在。")
    pcm = read_pcm(normalized)
    coverage = original.get("coverage", {})
    if (coverage.get("complete") is not True
            or coverage.get("sample_count") != len(pcm)
            or coverage.get("covered_sample_count") != len(pcm)
            or sample_end > len(pcm)):
        raise BlankRerunError("原 MiMo PCM 全文件覆盖证据不成立。")
    source_value = task.get("source_path")
    if source_value:
        source = resolve_path(source_value)
        if not source.is_file() or sha256_file(source) != source_sha:
            raise BlankRerunError("队列指向的源文件 SHA 与原 MiMo ASR 不一致。")
    audio_start = float(original.get("audio", {}).get("audio_start_seconds", 0.0))
    return {
        "source_sha256": source_sha,
        "sample_start": sample_start,
        "sample_end": sample_end,
        "original_segment": segment,
        "original_asr_path": str(asr_path.resolve()),
        "normalized_audio_path": str(normalized.resolve()),
        "pcm": pcm,
        "pcm_sha256": sha256_pcm(pcm),
        "audio_start_seconds": audio_start,
    }


def validate_recognized(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BlankRerunError("MiMo 补跑响应不是对象。")
    if value.get("model") != MODEL or value.get("finish_reason") != "stop":
        raise BlankRerunError("MiMo 补跑响应模型或 finish_reason 不合格。")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise BlankRerunError("MiMo 补跑响应缺少请求 ID。")
    if not isinstance(value.get("text"), str):
        raise BlankRerunError("MiMo 补跑响应缺少文本。")
    request_ids = value.get("request_ids", [value["id"]])
    if (not isinstance(request_ids, list) or not request_ids
            or not all(isinstance(item, str) and item for item in request_ids)):
        raise BlankRerunError("MiMo 补跑请求 ID 列表非法。")
    usage = value.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise BlankRerunError("MiMo 补跑 usage 非法。")
    return {
        "model": MODEL,
        "finish_reason": "stop",
        "id": value["id"],
        "request_ids": request_ids,
        "text": value["text"],
        "usage": usage,
        "content_filter_split": bool(value.get("content_filter_split")),
    }


def request_id_summary(request_ids: list[str]) -> dict[str, Any]:
    digest = hashlib.sha256("\n".join(request_ids).encode("utf-8")).hexdigest()
    return {"request_count": len(request_ids), "request_id_sha256": digest,
            "request_id_digest": digest[:24]}


def _cache_identity(
    context: dict[str, Any], original_index: int, begin: int, end: int,
) -> dict[str, Any]:
    return {
        "processor_version": PROCESSOR_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
        "source_sha256": context["source_sha256"],
        "pcm_sha256": context["pcm_sha256"],
        "original_segment_index": original_index,
        "original_sample_start": context["sample_start"],
        "original_sample_end": context["sample_end"],
        "sample_start": begin,
        "sample_end": end,
        "sample_rate": SAMPLE_RATE,
        "model": MODEL,
        "endpoint": ENDPOINT,
    }


def _cached_response(path: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("identity") != identity:
            return None
        return validate_recognized(cached.get("response"))
    except (OSError, ValueError, TypeError, BlankRerunError):
        return None


def process_task(
    task: dict[str, Any], cache_dir: Path, api_key: str, requester: Requester,
    *, progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    context = _original_context(task)
    original = context["original_segment"]
    original_index = int(original["index"])
    intervals = split_near_silence(
        context["pcm"], context["sample_start"], context["sample_end"],
    )
    children = []
    errors = []
    for index, (begin, end) in enumerate(intervals):
        identity = _cache_identity(context, original_index, begin, end)
        cache_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cache_path = cache_dir / context["source_sha256"][:16] / f"{cache_key}.json"
        recognized = _cached_response(cache_path, identity)
        cached = recognized is not None
        try:
            if recognized is None:
                recognized = validate_recognized(
                    requester(context["pcm"][begin:end], api_key, ENDPOINT)
                )
                if api_key and api_key in json.dumps(recognized, ensure_ascii=False):
                    raise BlankRerunError("MiMo 响应异常包含 API 密钥，拒绝保存。")
                atomic_json(cache_path, {"identity": identity, "response": recognized})
            summary = request_id_summary(recognized["request_ids"])
            children.append({
                "index": index,
                "sample_start": begin,
                "sample_end": end,
                "sample_count": end - begin,
                "start_seconds": context["audio_start_seconds"] + begin / SAMPLE_RATE,
                "end_seconds": context["audio_start_seconds"] + end / SAMPLE_RATE,
                "duration_seconds": (end - begin) / SAMPLE_RATE,
                "text": recognized["text"],
                "finish_reason": recognized["finish_reason"],
                "model": recognized["model"],
                **summary,
                "content_filter_split": recognized["content_filter_split"],
                "cached": cached,
            })
            progress(f"    子段 {index + 1}/{len(intervals)} 完成{'（缓存）' if cached else ''}")
        except Exception as exc:
            errors.append({
                "code": "mimo_request_failed", "child_index": index,
                "sample_start": begin, "sample_end": end, "message": str(exc),
            })
            progress(f"    子段 {index + 1}/{len(intervals)} 失败：{exc}")
    covered = sum(item["sample_count"] for item in children)
    complete = (
        not errors and len(children) == len(intervals)
        and intervals[0][0] == context["sample_start"]
        and intervals[-1][1] == context["sample_end"]
        and covered == context["sample_end"] - context["sample_start"]
        and all(left["sample_end"] == right["sample_start"]
                for left, right in zip(children, children[1:]))
    )
    return {
        "review_id": task.get("review_id"),
        "audit_sources": task.get("audit_sources", []),
        "source_path": task.get("source_path"),
        "source_sha256": context["source_sha256"],
        "original_asr_path": context["original_asr_path"],
        "original_segment_index": original_index,
        "original_sample_start": context["sample_start"],
        "original_sample_end": context["sample_end"],
        "original_start_seconds": original.get("start_seconds"),
        "original_end_seconds": original.get("end_seconds"),
        "original_text": original.get("text", ""),
        "new_mimo_text": "\n".join(
            child["text"].strip() for child in children if child["text"].strip()
        ),
        "children": children,
        "coverage": {
            "complete": complete,
            "expected_sample_count": context["sample_end"] - context["sample_start"],
            "covered_sample_count": covered,
            "child_count": len(intervals),
            "completed_child_count": len(children),
        },
        "complete": complete,
        "errors": errors,
    }


def build_patch(
    sensevoice_queue: Path,
    whisper_queue: Path,
    output: Path,
    cache_dir: Path,
    api_key: str | None = None,
    *,
    approved_review_ids: set[str] | frozenset[str] = frozenset(),
    requester: Requester = asr._recognize_complete,
    limit: int = 0,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    queue_paths = [sensevoice_queue, whisper_queue]
    asr_corpus_sha256 = queue_asr_corpus_sha256(queue_paths)
    candidates = load_speech_union(queue_paths)
    approved = {value for value in approved_review_ids if isinstance(value, str) and value}

    def review_ids(task: dict[str, Any]) -> set[str]:
        values = {item.get("review_id") for item in task.get("audit_sources", [])}
        values.add(task.get("review_id"))
        return {value for value in values if isinstance(value, str) and value}

    known_ids = set().union(*(review_ids(task) for task in candidates)) if candidates else set()
    unknown = sorted(approved - known_ids)
    if unknown:
        raise BlankRerunError(f"批准列表含未知 review ID：{unknown[0]}")
    tasks = [task for task in candidates if review_ids(task) & approved]
    if limit:
        tasks = tasks[:limit]
    if tasks and (not isinstance(api_key, str) or not api_key.strip()):
        raise BlankRerunError("有已批准候选时必须通过环境变量或 getpass 提供 MiMo 密钥。")

    candidate_records = [{
        "review_ids": sorted(review_ids(task)),
        "source_path": task.get("source_path"),
        "source_sha256": task.get("source_sha256"),
        "sample_start": task.get("sample_start"),
        "sample_end": task.get("sample_end"),
        "local_judgment": "speech_detected",
        "risk": "local_hallucination_risk",
        "approved": bool(review_ids(task) & approved),
        "action": ("mimo_resegment_and_rerun" if review_ids(task) & approved
                   else "keep_original_mimo_blank"),
        "audit_sources": task.get("audit_sources", []),
    } for task in candidates]
    auditor_counts = {
        auditor: sum(
            any(source.get("auditor") == auditor for source in task.get("audit_sources", []))
            for task in candidates
        )
        for auditor in ("sensevoice", "whisper")
    }
    cross_auditor_intersection = sum(
        {source.get("auditor") for source in task.get("audit_sources", [])}
        >= {"sensevoice", "whisper"}
        for task in candidates
    )
    patches = []
    for index, task in enumerate(tasks, 1):
        progress(
            f"[{index}/{len(tasks)}] {task.get('source_path') or task['source_sha256']} "
            f"PCM {task['sample_start']}:{task['sample_end']}"
        )
        try:
            patches.append(process_task(task, cache_dir, api_key.strip(), requester, progress=progress))
        except Exception as exc:
            patches.append({
                "review_id": task.get("review_id"),
                "audit_sources": task.get("audit_sources", []),
                "source_path": task.get("source_path"),
                "source_sha256": task.get("source_sha256"),
                "original_sample_start": task.get("sample_start"),
                "original_sample_end": task.get("sample_end"),
                "new_mimo_text": "", "children": [],
                "coverage": {"complete": False, "expected_sample_count": None,
                             "covered_sample_count": 0, "child_count": 0,
                             "completed_child_count": 0},
                "complete": False,
                "errors": [{"code": "task_validation_failed", "message": str(exc)}],
            })
        result = {
            "schema_version": "mimo-blank-segment-patch/v1",
            "processor_version": PROCESSOR_VERSION,
            "asr_corpus_sha256": asr_corpus_sha256,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "endpoint": ENDPOINT,
            "model": MODEL,
            "input_queues": [str(Path(sensevoice_queue).resolve()), str(Path(whisper_queue).resolve())],
            "candidate_count": len(candidates),
            "auditor_candidate_counts": auditor_counts,
            "cross_auditor_pcm_intersection_count": cross_auditor_intersection,
            "approved_review_ids": sorted(approved),
            "selected_original_segment_count": len(tasks),
            "completed_original_segment_count": sum(item["complete"] for item in patches),
            "complete": len(patches) == len(tasks) and all(item["complete"] for item in patches),
            "execution_requested": bool(tasks),
            "original_asr_files_modified": False,
            "original_mimo_blank_preserved_count": len(candidates) - len(tasks),
            "candidates": candidate_records,
            "patches": patches,
            "errors": [error for item in patches for error in item.get("errors", [])],
        }
        atomic_json(output, result)
    if not tasks:
        result = {
            "schema_version": "mimo-blank-segment-patch/v1",
            "processor_version": PROCESSOR_VERSION,
            "asr_corpus_sha256": asr_corpus_sha256,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "endpoint": ENDPOINT, "model": MODEL,
            "input_queues": [str(Path(sensevoice_queue).resolve()), str(Path(whisper_queue).resolve())],
            "candidate_count": len(candidates),
            "auditor_candidate_counts": auditor_counts,
            "cross_auditor_pcm_intersection_count": cross_auditor_intersection,
            "approved_review_ids": sorted(approved),
            "selected_original_segment_count": 0, "completed_original_segment_count": 0,
            "complete": True, "execution_requested": False,
            "original_asr_files_modified": False,
            "original_mimo_blank_preserved_count": len(candidates),
            "candidates": candidate_records, "patches": [], "errors": [],
        }
        atomic_json(output, result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对双模型确认含语音的 MiMo 空段重新切分补跑")
    parser.add_argument("--sensevoice-queue", type=Path, default=DEFAULT_SENSEVOICE_QUEUE)
    parser.add_argument("--whisper-queue", type=Path, default=DEFAULT_WHISPER_QUEUE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--approved-review-ids", nargs="*", default=[],
        help="人工明确批准的 review ID；省略或空列表时仅输出候选审计，绝不调用 API",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # The endpoint is deliberately not configurable by environment or CLI.
    if asr._configured_endpoint(ENDPOINT) != ENDPOINT:
        raise SystemExit("固定 MiMo 官方 endpoint 校验失败。")
    try:
        tasks = load_speech_union([args.sensevoice_queue, args.whisper_queue])
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    try:
        approved = set(args.approved_review_ids)
        key = acquire_api_key() if approved else None
        result = build_patch(
            args.sensevoice_queue, args.whisper_queue, args.output, args.cache_dir,
            key, approved_review_ids=approved, limit=args.limit,
        )
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"候选 {result['candidate_count']}；批准 {result['selected_original_segment_count']}；"
        f"完成 {result['completed_original_segment_count']}；输出 {args.output}",
        flush=True,
    )
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
