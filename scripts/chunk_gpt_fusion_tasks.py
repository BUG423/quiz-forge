#!/usr/bin/env python3
"""Mechanically split GPT evidence tasks into reversible reading chunks.

The program does not call a model and never creates fused prose.  It preserves
the ordered ASR/OCR text-event stream exactly.  The only compression allowed is
run-length encoding of adjacent video frames whose OCR text arrays are exactly
equal; every physical frame keeps its own locator and non-text metadata.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = ROOT / ".work" / "first-principles-2026" / "gpt-tasks"
DEFAULT_OUTPUT_DIR = ROOT / ".work" / "first-principles-2026" / "gpt-reading-chunks"
DEFAULT_MIN_CHARS = 40_000
DEFAULT_TARGET_CHARS = 50_000
DEFAULT_MAX_CHARS = 60_000
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
TASK_KEYS = {
    "asset_id", "source_name", "source_sha256", "evidence_sha256",
    "ASR 结果", "OCR 结果", "task_constraints",
}


class ChunkingError(RuntimeError):
    """Raised before output when a task is invalid or not reversible."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChunkingError(f"无法读取有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ChunkingError(f"JSON 顶层必须是对象：{path}")
    return value


def valid_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
        raise ChunkingError(f"{field} 必须是 64 位 SHA-256")
    return value.lower()


def _as_list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChunkingError(f"{location} 必须是数组")
    return value


def _as_dict(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChunkingError(f"{location} 必须是对象")
    return value


def _without_text(record: dict[str, Any], field: str, location: str) -> tuple[dict[str, Any], str]:
    metadata = copy.deepcopy(record)
    text = metadata.pop(field, None)
    if not isinstance(text, str):
        raise ChunkingError(f"{location}.{field} 必须是字符串")
    return metadata, text


def _line_record(
    record: dict[str, Any], line_fields: Iterable[str], location: str,
    *, scalar_text_fields: Iterable[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Separate text from an atomic record without changing either's order."""
    metadata = copy.deepcopy(record)
    texts: list[dict[str, Any]] = []
    for field in scalar_text_fields:
        if field not in metadata:
            continue
        value = metadata.pop(field)
        if not isinstance(value, str):
            raise ChunkingError(f"{location}.{field} 必须是字符串")
        texts.append({"path": [field], "text": value})
    for field in line_fields:
        if field not in metadata:
            continue
        lines = _as_list(metadata[field], f"{location}.{field}")
        for line_index, line in enumerate(lines):
            item = _as_dict(line, f"{location}.{field}[{line_index}]")
            if "text" not in item:
                raise ChunkingError(f"{location}.{field}[{line_index}].text 缺失")
            value = item.pop("text")
            if not isinstance(value, str):
                raise ChunkingError(f"{location}.{field}[{line_index}].text 必须是字符串")
            texts.append({"path": [field, line_index, "text"], "text": value})
    return metadata, texts


def _locator(record: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: copy.deepcopy(record[field]) for field in fields if field in record}


def _append_event(
    output: list[dict[str, Any]], *, channel: str, record_kind: str,
    origin: dict[str, Any], locator: dict[str, Any], texts: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    output.append({
        "event_index": len(output),
        "channel": channel,
        "record_kind": record_kind,
        "origin": copy.deepcopy(origin),
        "locator": copy.deepcopy(locator),
        "texts": copy.deepcopy(texts),
        "metadata": copy.deepcopy(metadata),
    })


def _extract_mimo_segments(
    wrapper: dict[str, Any], output: list[dict[str, Any]], origin: dict[str, Any],
    location: str,
) -> None:
    result = wrapper.get("result")
    if result is None:
        return
    document = _as_dict(result, f"{location}.result")
    segments = _as_list(document.get("segments"), f"{location}.result.segments")
    for ordinal, segment_value in enumerate(segments):
        segment = _as_dict(segment_value, f"{location}.segments[{ordinal}]")
        metadata, text = _without_text(segment, "text", f"{location}.segments[{ordinal}]")
        _append_event(
            output, channel="ASR", record_kind="asr_segment", origin=origin,
            locator={
                "segment_ordinal": ordinal,
                **_locator(segment, (
                    "index", "start_seconds", "end_seconds", "sample_start", "sample_end",
                )),
            },
            texts=[{"path": ["text"], "text": text}], metadata=metadata,
        )


def _extract_asr(asr: dict[str, Any], output: list[dict[str, Any]]) -> None:
    if isinstance(asr.get("result"), dict):
        _extract_mimo_segments(asr, output, {"scope": "asset"}, "ASR 结果")
    embedded = asr.get("embedded_media", [])
    if embedded is None:
        embedded = []
    for media_ordinal, media_value in enumerate(_as_list(embedded, "ASR 结果.embedded_media")):
        media = _as_dict(media_value, f"ASR 结果.embedded_media[{media_ordinal}]")
        wrapper = _as_dict(
            media.get("asr_result"), f"ASR 结果.embedded_media[{media_ordinal}].asr_result",
        )
        origin = {
            "scope": "embedded_media", "media_ordinal": media_ordinal,
            **_locator(media, ("source_sha256", "source_path", "slide", "member")),
        }
        _extract_mimo_segments(wrapper, output, origin, f"ASR embedded_media[{media_ordinal}]")


def _extract_video_frames(
    wrapper: dict[str, Any], output: list[dict[str, Any]], origin: dict[str, Any],
    location: str,
) -> None:
    frames = _as_list(wrapper.get("frames", []), f"{location}.frames")
    for frame_ordinal, frame_value in enumerate(frames):
        frame = _as_dict(frame_value, f"{location}.frames[{frame_ordinal}]")
        metadata, texts = _line_record(
            frame, ("lines",), f"{location}.frames[{frame_ordinal}]",
        )
        _append_event(
            output, channel="OCR", record_kind="ocr_video_frame", origin=origin,
            locator={
                "frame_ordinal": frame_ordinal,
                **_locator(frame, (
                    "frame_source", "merged_order", "index", "pts", "time_base",
                    "timestamp_seconds", "actual_timestamp_seconds", "frame_image_sha256",
                )),
            }, texts=texts, metadata=metadata,
        )


def _extract_page(
    page: dict[str, Any], output: list[dict[str, Any]], origin: dict[str, Any],
    location: str, record_kind: str,
) -> None:
    metadata, texts = _line_record(page, ("raw_ocr_lines", "native_lines"), location)
    _append_event(
        output, channel="OCR", record_kind=record_kind, origin=origin,
        locator=_locator(page, ("page", "part", "part_count", "index")),
        texts=texts, metadata=metadata,
    )


def _extract_native_document(
    values: Any, output: list[dict[str, Any]], origin: dict[str, Any], location: str,
) -> None:
    if values is None:
        return
    for ordinal, value in enumerate(_as_list(values, location)):
        item = _as_dict(value, f"{location}[{ordinal}]")
        metadata, text = _without_text(item, "text", f"{location}[{ordinal}]")
        _append_event(
            output, channel="OCR", record_kind="ocr_native_document_item", origin=origin,
            locator={"item_ordinal": ordinal, **_locator(item, ("index", "kind", "page"))},
            texts=[{"path": ["text"], "text": text}], metadata=metadata,
        )


def _extract_native_content(
    values: Any, output: list[dict[str, Any]], origin: dict[str, Any], location: str,
) -> None:
    if values is None:
        return
    for content_ordinal, value in enumerate(_as_list(values, location)):
        record = _as_dict(value, f"{location}[{content_ordinal}]")
        # A workbook sheet is an attachment record.  Sheet name and string cell
        # values are evidence text; all other fields remain verbatim metadata.
        metadata = copy.deepcopy(record)
        texts: list[dict[str, Any]] = []
        if "name" in metadata:
            name = metadata.pop("name")
            if not isinstance(name, str):
                raise ChunkingError(f"{location}[{content_ordinal}].name 必须是字符串")
            texts.append({"path": ["name"], "text": name})
        cells = metadata.get("cells")
        if cells is not None:
            for cell_ordinal, cell_value in enumerate(_as_list(cells, f"{location}.cells")):
                cell = _as_dict(cell_value, f"{location}.cells[{cell_ordinal}]")
                if isinstance(cell.get("value"), str):
                    text = cell.pop("value")
                    texts.append({"path": ["cells", cell_ordinal, "value"], "text": text})
        _append_event(
            output, channel="OCR", record_kind="ocr_attachment_native_content",
            origin=origin, locator={"content_ordinal": content_ordinal},
            texts=texts, metadata=metadata,
        )


def _extract_courseware(courseware: dict[str, Any], output: list[dict[str, Any]]) -> None:
    base_origin = {"scope": "courseware"}
    for page_ordinal, page_value in enumerate(_as_list(courseware.get("pages", []), "OCR pages")):
        page = _as_dict(page_value, f"OCR pages[{page_ordinal}]")
        _extract_page(
            page, output, {**base_origin, "page_ordinal": page_ordinal},
            f"OCR pages[{page_ordinal}]", "ocr_courseware_page",
        )
    _extract_native_document(
        courseware.get("native_document_text"), output, base_origin,
        "OCR courseware.native_document_text",
    )
    objects = _as_list(courseware.get("embedded_objects", []), "OCR embedded_objects")
    for object_ordinal, object_value in enumerate(objects):
        embedded_object = _as_dict(object_value, f"OCR embedded_objects[{object_ordinal}]")
        object_origin = {
            "scope": "attachment", "attachment_ordinal": object_ordinal,
            **_locator(embedded_object, ("member", "member_sha256", "source_pages", "status")),
        }
        frames = embedded_object.get("raw_ocr_frames", [])
        if frames is None:
            frames = []
        for frame_ordinal, frame_value in enumerate(_as_list(frames, "OCR attachment frames")):
            frame = _as_dict(frame_value, f"OCR attachment frames[{frame_ordinal}]")
            _extract_page(
                frame, output, {**object_origin, "frame_ordinal": frame_ordinal},
                f"OCR attachment frames[{frame_ordinal}]", "ocr_attachment_frame",
            )
        _extract_native_content(
            embedded_object.get("native_content"), output, object_origin,
            f"OCR embedded_objects[{object_ordinal}].native_content",
        )


def _extract_ocr(ocr: dict[str, Any], output: list[dict[str, Any]]) -> None:
    if isinstance(ocr.get("frames"), list):
        _extract_video_frames(ocr, output, {"scope": "asset"}, "OCR 结果")
    courseware = ocr.get("courseware_record")
    if courseware is not None:
        _extract_courseware(_as_dict(courseware, "OCR 结果.courseware_record"), output)
    embedded = ocr.get("embedded_media", [])
    if embedded is None:
        embedded = []
    for media_ordinal, media_value in enumerate(_as_list(embedded, "OCR 结果.embedded_media")):
        media = _as_dict(media_value, f"OCR embedded_media[{media_ordinal}]")
        nested = _as_dict(media.get("ocr_result"), f"OCR embedded_media[{media_ordinal}].ocr_result")
        origin = {
            "scope": "embedded_media", "media_ordinal": media_ordinal,
            **_locator(media, ("source_sha256", "source_path", "slide", "member")),
        }
        _extract_video_frames(nested, output, origin, f"OCR embedded_media[{media_ordinal}]")


def extract_text_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered, lossless evidence-text event stream for one task."""
    output: list[dict[str, Any]] = []
    _extract_asr(_as_dict(task.get("ASR 结果"), "ASR 结果"), output)
    _extract_ocr(_as_dict(task.get("OCR 结果"), "OCR 结果"), output)
    return output


def event_texts(events: Iterable[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    for event in events:
        for item in event["texts"]:
            output.append(item["text"])
    return output


def event_character_count(event: dict[str, Any]) -> int:
    return sum(len(item["text"]) for item in event["texts"])


def _frame_timestamp(locator: dict[str, Any]) -> Any:
    return locator.get("actual_timestamp_seconds", locator.get("timestamp_seconds"))


def _make_frame_run(events: list[dict[str, Any]]) -> dict[str, Any]:
    first, last = events[0], events[-1]
    return {
        "record_type": "video_frame_run",
        "channel": "OCR",
        "record_kind": "ocr_video_frame",
        "origin": copy.deepcopy(first["origin"]),
        "texts": copy.deepcopy(first["texts"]),
        "frame_count": len(events),
        "time_range_seconds": {
            "first": _frame_timestamp(first["locator"]),
            "last": _frame_timestamp(last["locator"]),
        },
        "frames": [{
            "event_index": event["event_index"],
            "locator": copy.deepcopy(event["locator"]),
            "metadata": copy.deepcopy(event["metadata"]),
        } for event in events],
    }


def run_length_encode(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Encode only adjacent, same-source video frames with exactly equal text."""
    records: list[dict[str, Any]] = []
    index = 0
    while index < len(events):
        event = events[index]
        if event["record_kind"] != "ocr_video_frame":
            records.append({"record_type": "event", "event": copy.deepcopy(event)})
            index += 1
            continue
        end = index + 1
        while end < len(events):
            candidate = events[end]
            if (candidate["record_kind"] != "ocr_video_frame"
                    or candidate["origin"] != event["origin"]
                    or candidate["texts"] != event["texts"]):
                break
            end += 1
        if end - index >= 2:
            records.append(_make_frame_run(events[index:end]))
        else:
            records.append({"record_type": "event", "event": copy.deepcopy(event)})
        index = end
    return records


def expand_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in records:
        if record.get("record_type") == "event":
            output.append(copy.deepcopy(_as_dict(record.get("event"), "encoded event")))
        elif record.get("record_type") == "video_frame_run":
            frames = _as_list(record.get("frames"), "video_frame_run.frames")
            if record.get("frame_count") != len(frames) or len(frames) < 2:
                raise ChunkingError("视频帧游程 frame_count 无效")
            for frame in frames:
                item = _as_dict(frame, "video_frame_run.frames[]")
                output.append({
                    "event_index": item["event_index"],
                    "channel": record["channel"],
                    "record_kind": record["record_kind"],
                    "origin": copy.deepcopy(record["origin"]),
                    "locator": copy.deepcopy(item["locator"]),
                    "texts": copy.deepcopy(record["texts"]),
                    "metadata": copy.deepcopy(item["metadata"]),
                })
        else:
            raise ChunkingError("未知编码记录类型")
    return output


def record_character_count(record: dict[str, Any]) -> int:
    if record["record_type"] == "event":
        return event_character_count(record["event"])
    per_frame = sum(len(item["text"]) for item in record["texts"])
    return per_frame * record["frame_count"]


def _split_frame_run(record: dict[str, Any], max_chars: int) -> list[dict[str, Any]]:
    if record.get("record_type") != "video_frame_run":
        return [record]
    per_frame = sum(len(item["text"]) for item in record["texts"])
    if per_frame == 0 or record_character_count(record) <= max_chars:
        return [record]
    frames_per_record = max(1, max_chars // per_frame)
    output = []
    frames = record["frames"]
    for start in range(0, len(frames), frames_per_record):
        frame_slice = frames[start:start + frames_per_record]
        synthetic_events = [{
            "event_index": item["event_index"],
            "channel": record["channel"],
            "record_kind": record["record_kind"],
            "origin": copy.deepcopy(record["origin"]),
            "locator": copy.deepcopy(item["locator"]),
            "texts": copy.deepcopy(record["texts"]),
            "metadata": copy.deepcopy(item["metadata"]),
        } for item in frame_slice]
        if len(synthetic_events) == 1:
            output.append({"record_type": "event", "event": synthetic_events[0]})
        else:
            output.append(_make_frame_run(synthetic_events))
    return output


def chunk_records(
    records: list[dict[str, Any]], *, min_chars: int = DEFAULT_MIN_CHARS,
    target_chars: int = DEFAULT_TARGET_CHARS, max_chars: int = DEFAULT_MAX_CHARS,
) -> list[list[dict[str, Any]]]:
    if not (0 < min_chars <= target_chars <= max_chars):
        raise ChunkingError("分块字符阈值必须满足 0 < min <= target <= max")
    prepared: list[dict[str, Any]] = []
    for record in records:
        prepared.extend(_split_frame_run(record, max_chars))
    if not prepared:
        return [[]]
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for record in prepared:
        weight = record_character_count(record)
        combined = current_chars + weight
        if current and current_chars >= min_chars:
            current_distance = abs(target_chars - current_chars)
            combined_distance = abs(target_chars - combined)
            if combined > max_chars or (combined >= target_chars and current_distance <= combined_distance):
                chunks.append(current)
                current, current_chars = [], 0
        current.append(record)
        current_chars += weight
    if current:
        chunks.append(current)
    return chunks


def _safe_task_path(input_dir: Path, filename: Any) -> Path:
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ChunkingError(f"task_file 路径不安全：{filename!r}")
    return input_dir / filename


def _validate_task(
    task: dict[str, Any], entry: dict[str, Any], order: int,
) -> tuple[str, str, str, str]:
    if set(task) != TASK_KEYS:
        raise ChunkingError(f"任务 {order} 字段集合不符合纯证据任务 schema")
    asset_id = task.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise ChunkingError(f"任务 {order} asset_id 无效")
    source_name = task.get("source_name")
    if not isinstance(source_name, str) or not source_name:
        raise ChunkingError(f"任务 {order} source_name 无效")
    source_sha = valid_sha(task.get("source_sha256"), f"{asset_id}.source_sha256")
    evidence_sha = valid_sha(task.get("evidence_sha256"), f"{asset_id}.evidence_sha256")
    task_sha = canonical_sha256(task)
    for key, actual in (
        ("asset_id", asset_id), ("source_name", source_name),
        ("source_sha256", source_sha), ("evidence_sha256", evidence_sha),
        ("task_sha256", task_sha),
    ):
        expected = entry.get(key)
        if key.endswith("sha256") and isinstance(expected, str):
            expected = expected.lower()
        if expected != actual:
            raise ChunkingError(f"任务 {order} 的 {key} 与索引绑定失败")
    constraints = _as_dict(task.get("task_constraints"), f"{asset_id}.task_constraints")
    final_review = constraints.get("final_review")
    if not isinstance(final_review, str) or not final_review.strip():
        raise ChunkingError(f"{asset_id} 缺少文件级最终复核要求")
    return asset_id, source_name, source_sha, evidence_sha


def _chunk_payload(
    *, asset_id: str, source_name: str, source_sha: str, evidence_sha: str,
    task_sha: str, chunk_index: int, chunk_count: int,
    records: list[dict[str, Any]], stream_sha: str, text_stream_sha: str,
    final_review: str,
) -> dict[str, Any]:
    event_indexes = [event["event_index"] for event in expand_records(records)]
    payload = {
        "schema_version": "gpt-reading-chunk/v1",
        "asset_id": asset_id,
        "source_name": source_name,
        "source_sha256": source_sha,
        "evidence_sha256": evidence_sha,
        "task_sha256": task_sha,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "event_index_range": {
            "first": event_indexes[0] if event_indexes else None,
            "last": event_indexes[-1] if event_indexes else None,
        },
        "original_text_character_count": sum(record_character_count(r) for r in records),
        "file_event_stream_sha256": stream_sha,
        "file_text_stream_sha256": text_stream_sha,
        "records": copy.deepcopy(records),
        "file_level_final_review": final_review,
        "provisional_only": True,
    }
    payload["chunk_sha256"] = canonical_sha256(payload)
    return payload


def write_chunk_directory(
    output_dir: Path, files: list[tuple[str, dict[str, Any]]], index: dict[str, Any],
) -> None:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ChunkingError(f"分块目录已存在，拒绝覆盖：{output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        for filename, payload in files:
            with (staged / filename).open("wb") as handle:
                handle.write(canonical_bytes(payload) + b"\n")
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


def build_reading_chunks(
    *, input_dir: Path = DEFAULT_INPUT_DIR, output_dir: Path | None = DEFAULT_OUTPUT_DIR,
    expected_files: int = 91, min_chars: int = DEFAULT_MIN_CHARS,
    target_chars: int = DEFAULT_TARGET_CHARS, max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    index = read_json(input_dir / "index.json")
    if index.get("status") != "ready":
        raise ChunkingError("只接受 status=ready 的 gpt-tasks 索引")
    tasks = index.get("tasks")
    if (index.get("file_count") != expected_files or not isinstance(tasks, list)
            or len(tasks) != expected_files):
        actual = len(tasks) if isinstance(tasks, list) else "invalid"
        raise ChunkingError(f"gpt-tasks 必须恰好 {expected_files} 项，实际 {actual}")

    output_files: list[tuple[str, dict[str, Any]]] = []
    asset_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_task_files: set[str] = set()
    for order, entry_value in enumerate(tasks, 1):
        entry = _as_dict(entry_value, f"index.tasks[{order - 1}]")
        if entry.get("order") != order:
            raise ChunkingError("gpt-tasks 未保持连续目录顺序")
        task_file = entry.get("task_file")
        if task_file in seen_task_files:
            raise ChunkingError(f"task_file 重复：{task_file}")
        path = _safe_task_path(input_dir, task_file)
        task = read_json(path)
        asset_id, source_name, source_sha, evidence_sha = _validate_task(task, entry, order)
        if asset_id in seen_ids:
            raise ChunkingError(f"asset_id 重复：{asset_id}")
        seen_ids.add(asset_id)
        seen_task_files.add(task_file)
        task_sha = entry["task_sha256"].lower()

        events = extract_text_events(task)
        records = run_length_encode(events)
        if expand_records(records) != events:
            raise ChunkingError(f"游程编码展开与原事件不一致：{asset_id}")
        stream_sha = canonical_sha256(events)
        text_stream = event_texts(events)
        text_stream_sha = canonical_sha256(text_stream)
        groups = chunk_records(
            records, min_chars=min_chars, target_chars=target_chars, max_chars=max_chars,
        )
        if expand_records(record for group in groups for record in group) != events:
            raise ChunkingError(f"分块展开与原事件不一致：{asset_id}")
        if canonical_sha256(event_texts(expand_records(
                record for group in groups for record in group))) != text_stream_sha:
            raise ChunkingError(f"分块文字流与原证据逐字不一致：{asset_id}")

        final_review = task["task_constraints"]["final_review"]
        chunk_entries = []
        for chunk_index, group in enumerate(groups, 1):
            payload = _chunk_payload(
                asset_id=asset_id, source_name=source_name, source_sha=source_sha,
                evidence_sha=evidence_sha, task_sha=task_sha, chunk_index=chunk_index,
                chunk_count=len(groups), records=group, stream_sha=stream_sha,
                text_stream_sha=text_stream_sha, final_review=final_review,
            )
            filename = f"{order:04d}-{source_sha[:16]}-chunk-{chunk_index:04d}.json"
            output_files.append((filename, payload))
            chunk_entries.append({
                "chunk_index": chunk_index,
                "chunk_file": filename,
                "chunk_sha256": payload["chunk_sha256"],
                "asset_id": asset_id,
                "source_sha256": source_sha,
                "evidence_sha256": evidence_sha,
                "task_sha256": task_sha,
                "original_text_character_count": payload["original_text_character_count"],
                "event_index_range": payload["event_index_range"],
            })
        asset_entries.append({
            "order": order,
            "catalog_position": entry.get("catalog_position"),
            "catalog_suborder": entry.get("catalog_suborder"),
            "asset_id": asset_id,
            "source_name": source_name,
            "source_sha256": source_sha,
            "evidence_sha256": evidence_sha,
            "task_sha256": task_sha,
            "task_file": task_file,
            "event_count": len(events),
            "original_text_character_count": sum(len(text) for text in text_stream),
            "event_stream_sha256": stream_sha,
            "text_stream_sha256": text_stream_sha,
            "chunk_count": len(groups),
            "chunks": chunk_entries,
            "file_level_final_review": final_review,
        })

    output_index = {
        "schema_version": "gpt-reading-chunk-index/v1",
        "status": "ready",
        "source_task_index_sha256": canonical_sha256(index),
        "file_count": len(asset_entries),
        "chunk_count": len(output_files),
        "chunk_character_policy": {
            "minimum": min_chars, "target": target_chars, "maximum": max_chars,
            "atomic_records_are_never_split": True,
        },
        "run_length_policy": {
            "scope": "adjacent video frames from the same source only",
            "equality": "exact ordered OCR text array equality",
            "approximate_deduplication": False,
            "confidence_filtering": False,
            "text_correction": False,
            "expansion_verified": True,
        },
        "file_level_final_review_required": True,
        "assets": asset_entries,
    }
    if output_dir is not None:
        write_chunk_directory(output_dir, output_files, output_index)
    return {"index": output_index, "chunk_files": output_files}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--target-chars", type=int, default=DEFAULT_TARGET_CHARS)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_reading_chunks(
            input_dir=args.input_dir, output_dir=args.output_dir, expected_files=91,
            min_chars=args.min_chars, target_chars=args.target_chars, max_chars=args.max_chars,
        )
    except ChunkingError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc),
                          "output_written": False}, ensure_ascii=False, indent=2))
        return 2
    index = result["index"]
    print(json.dumps({
        "status": index["status"], "file_count": index["file_count"],
        "chunk_count": index["chunk_count"],
        "output_dir": str(args.output_dir.resolve()),
        "generated_fusion_prose": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
