from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import av


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入模型代码前锁定离线模式。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from server import transcribe_local


DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
TRANSCRIPT_DIR = OUTPUT_DIR / "逐个转写"
METADATA_DIR = OUTPUT_DIR / "转写元数据"
STATUS_JSON = OUTPUT_DIR / "transcription_status.json"
STATUS_TXT = OUTPUT_DIR / "视频转写进度.txt"
TASK_LIST = OUTPUT_DIR / "视频转写任务清单.txt"
COMBINED_TXT = OUTPUT_DIR / "全部转写结果.txt"
MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".webm", ".ogg", ".ogv", ".oga", ".aac", ".flac", ".wma", ".avi"}

stop_requested = False


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def relative_key(path: Path) -> str:
    return path.relative_to(DATA_DIR).as_posix()


def output_path_for(source: Path, root: Path, suffix: str) -> Path:
    relative = source.relative_to(DATA_DIR)
    return root / relative.parent / f"{relative.stem}{suffix}"


def media_info(path: Path) -> tuple[float, bool]:
    with av.open(str(path)) as container:
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        has_audio = any(stream.type == "audio" for stream in container.streams)
    return duration, has_audio


def load_status() -> dict[str, dict[str, Any]]:
    if not STATUS_JSON.is_file():
        return {}
    try:
        payload = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        return payload.get("items", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_status(items: dict[str, dict[str, Any]], media: list[Path]) -> None:
    counts: dict[str, int] = {}
    for item in items.values():
        status = item.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1

    payload = {
        "updated_at": now_text(),
        "total": len(media),
        "counts": counts,
        "items": items,
    }
    atomic_write_json(STATUS_JSON, payload)

    labels = {
        "pending": "等待处理",
        "processing": "正在转写",
        "completed": "转写完成，待人工复核",
        "no_audio": "无音轨",
        "error": "处理失败",
        "interrupted": "处理被中断",
    }
    completed = counts.get("completed", 0) + counts.get("no_audio", 0)
    lines = [
        "视频转写进度",
        f"更新时间：{payload['updated_at']}",
        f"总任务：{len(media)}",
        f"已处理：{completed}",
        f"成功转写：{counts.get('completed', 0)}",
        f"无音轨：{counts.get('no_audio', 0)}",
        f"失败：{counts.get('error', 0)}",
        f"正在处理：{counts.get('processing', 0)}",
        f"等待处理：{counts.get('pending', 0)}",
        "",
        "序号\t状态\t视频时长（分钟）\t处理耗时（分钟）\t文件",
    ]
    for index, source in enumerate(media, 1):
        item = items[relative_key(source)]
        lines.append(
            f"{index}\t{labels.get(item['status'], item['status'])}\t"
            f"{item.get('duration_seconds', 0) / 60:.2f}\t"
            f"{item.get('processing_seconds', 0) / 60:.2f}\t{relative_key(source)}"
        )
        if item.get("error"):
            lines.append(f"\t错误：{item['error']}")
    atomic_write_text(STATUS_TXT, "\n".join(lines) + "\n")


def write_task_list(media: list[Path], items: dict[str, dict[str, Any]]) -> None:
    total_seconds = sum(item.get("duration_seconds", 0) for item in items.values())
    audio_seconds = sum(item.get("duration_seconds", 0) for item in items.values() if item.get("has_audio"))
    lines = [
        "视频转写任务清单",
        f"视频总数：{len(media)}",
        f"视频总时长：{total_seconds / 3600:.2f} 小时",
        f"有音轨视频时长：{audio_seconds / 3600:.2f} 小时",
        f"无音轨视频数：{sum(not item.get('has_audio') for item in items.values())}",
        "",
        "序号\t时长（分钟）\t音轨\t相对路径",
    ]
    for index, source in enumerate(media, 1):
        item = items[relative_key(source)]
        lines.append(
            f"{index}\t{item['duration_seconds'] / 60:.2f}\t"
            f"{'有' if item['has_audio'] else '无'}\t{relative_key(source)}"
        )
    atomic_write_text(TASK_LIST, "\n".join(lines) + "\n")


def write_combined(media: list[Path], items: dict[str, dict[str, Any]]) -> None:
    sections: list[str] = []
    for index, source in enumerate(media, 1):
        item = items[relative_key(source)]
        if item["status"] not in {"completed", "no_audio"}:
            continue
        transcript_path = output_path_for(source, TRANSCRIPT_DIR, ".txt")
        if not transcript_path.is_file():
            continue
        text = transcript_path.read_text(encoding="utf-8").strip()
        sections.append(
            f"{'=' * 72}\n{index}. {relative_key(source)}\n{'=' * 72}\n\n{text}"
        )
    atomic_write_text(COMBINED_TXT, "\n\n\n".join(sections) + ("\n" if sections else ""))


def initialize(media: list[Path]) -> dict[str, dict[str, Any]]:
    previous = load_status()
    items: dict[str, dict[str, Any]] = {}
    for source in media:
        key = relative_key(source)
        duration, has_audio = media_info(source)
        old = previous.get(key, {})
        transcript_path = output_path_for(source, TRANSCRIPT_DIR, ".txt")
        metadata_path = output_path_for(source, METADATA_DIR, ".json")
        reusable = old.get("status") in {"completed", "no_audio"} and transcript_path.is_file() and metadata_path.is_file()
        items[key] = {
            "source": key,
            "output": transcript_path.relative_to(ROOT).as_posix(),
            "metadata": metadata_path.relative_to(ROOT).as_posix(),
            "duration_seconds": round(duration, 2),
            "has_audio": has_audio,
            "status": old.get("status") if reusable else "pending",
            "processing_seconds": old.get("processing_seconds", 0) if reusable else 0,
            "updated_at": old.get("updated_at") if reusable else now_text(),
        }
    return items


def handle_signal(_signum, _frame) -> None:
    global stop_requested
    stop_requested = True
    print("\n收到停止信号，将在当前模型调用结束后安全退出。", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="完全离线、可断点续跑的批量转写")
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理多少个待处理文件；0 表示全部")
    args = parser.parse_args()

    if not DATA_DIR.is_dir():
        raise SystemExit(f"目录不存在：{DATA_DIR}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    media = sorted(
        (path for path in DATA_DIR.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES),
        key=lambda path: relative_key(path),
    )
    items = initialize(media)
    write_task_list(media, items)
    save_status(items, media)
    write_combined(media, items)

    pending = [source for source in media if items[relative_key(source)]["status"] not in {"completed", "no_audio"}]
    # 无音轨文件先记录，再从短到长处理有声视频，以便尽快产出可检查的结果。
    pending.sort(key=lambda source: (items[relative_key(source)]["has_audio"], items[relative_key(source)]["duration_seconds"]))
    if args.limit > 0:
        pending = pending[: args.limit]
    print(f"待处理 {len(pending)} / {len(media)} 个文件；模型推理强制离线。", flush=True)

    processed_this_run = 0
    for source in pending:
        if stop_requested:
            break
        key = relative_key(source)
        item = items[key]
        transcript_path = output_path_for(source, TRANSCRIPT_DIR, ".txt")
        metadata_path = output_path_for(source, METADATA_DIR, ".json")

        if not item["has_audio"]:
            message = "[该视频不包含音轨，无法进行语音转写。]"
            atomic_write_text(transcript_path, message + "\n")
            atomic_write_json(metadata_path, {**item, "status": "no_audio", "message": message, "local_only": True})
            item.update(status="no_audio", updated_at=now_text())
            save_status(items, media)
            write_combined(media, items)
            print(f"无音轨，已记录：{key}", flush=True)
            continue

        item.update(status="processing", updated_at=now_text(), error="")
        save_status(items, media)
        print(f"开始：{key}（{item['duration_seconds'] / 60:.2f} 分钟）", flush=True)
        started = time.perf_counter()
        try:
            result = transcribe_local(source, source.name)
            processing_seconds = time.perf_counter() - started
            if not result["text"].strip():
                raise RuntimeError("模型没有识别到文字")
            atomic_write_text(transcript_path, result["text"].strip() + "\n")
            metadata = {
                **result,
                "source": key,
                "transcript": transcript_path.relative_to(ROOT).as_posix(),
                "review_status": "待人工复核",
            }
            metadata.pop("text", None)
            atomic_write_json(metadata_path, metadata)
            item.update(
                status="completed",
                processing_seconds=round(processing_seconds, 2),
                updated_at=now_text(),
                low_confidence_count=result["low_confidence_count"],
            )
            processed_this_run += 1
            print(
                f"完成：{key}；耗时 {processing_seconds / 60:.2f} 分钟；"
                f"低置信度片段 {result['low_confidence_count']} 个",
                flush=True,
            )
        except Exception as exc:
            item.update(status="error", error=str(exc), processing_seconds=round(time.perf_counter() - started, 2), updated_at=now_text())
            print(f"失败：{key}；{exc}", flush=True)
        finally:
            save_status(items, media)
            write_combined(media, items)

    for item in items.values():
        if item["status"] == "processing":
            item.update(status="interrupted", updated_at=now_text())
    save_status(items, media)
    write_combined(media, items)
    print(f"本次成功转写 {processed_this_run} 个文件。进度：{STATUS_TXT}", flush=True)


if __name__ == "__main__":
    main()
