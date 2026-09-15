"""Resumable 2025 corpus recognition using MiMo ASR and one-second RapidOCR.

Recognition, model-assisted fusion and Word export remain separate stages.  This
module only inventories the explicit 2025 directory and produces source-bound
ASR/OCR evidence.  It deliberately ignores the ZIP container because its 11
members already exist beside it and are verified separately by the courseware
stage.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable

import av

from .runner import atomic_json, read_json

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ogv", ".wmv"}
COURSEWARE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".pptx", ".docx"}
TOKEN_PLAN_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
PRINT_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def discover(root: Path) -> tuple[list[Path], list[Path]]:
    """Use only direct children: extracted courseware is already beside videos."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("2025输入必须是明确目录")
    videos = sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),
                    key=lambda p: p.name.casefold())
    courseware = sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in COURSEWARE_SUFFIXES),
                        key=lambda p: (p.suffix.lower(), p.name.casefold()))
    if not videos:
        raise ValueError("2025目录中没有视频")
    return videos, courseware


def inspect_video(path: Path) -> dict[str, Any]:
    path = path.resolve(strict=True)
    digest = _sha256(path)
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError(f"不含视频流：{path.name}")
        video = container.streams.video[0]
        origin = float(container.start_time or 0) / av.time_base
        video_start = float((video.start_time or 0) * video.time_base) - origin
        duration = (video_start + float(video.duration * video.time_base)
                    if video.duration is not None else float(container.duration or 0) / av.time_base)
        if duration <= 0:
            raise ValueError(f"视频时长无效：{path.name}")
        audio = container.streams.audio[0] if container.streams.audio else None
        if audio is None:
            audio_start = audio_duration = audio_end = None
        else:
            audio_start = float((audio.start_time or 0) * audio.time_base) - origin
            audio_duration = (float(audio.duration * audio.time_base)
                              if audio.duration is not None else duration)
            audio_end = audio_start + audio_duration
    return {
        "schema_version": "2025-batch-1", "source_path": str(path), "source_name": path.name,
        "sha256": digest, "size_bytes": path.stat().st_size,
        "duration_seconds": duration, "timeline_origin_seconds": origin,
        "width": video.width, "height": video.height, "has_audio": audio is not None,
        "audio_start_seconds": audio_start, "audio_duration_seconds": audio_duration,
        "audio_end_seconds": audio_end, "asr_model": "mimo-v2.5-asr",
        "ocr_interval_seconds": 1.0, "expected_frame_count": math.ceil(duration),
    }


def _valid_asr(path: Path, manifest: dict) -> bool:
    try:
        value = read_json(path)
        if not manifest["has_audio"]:
            return (value.get("source_sha256") == manifest["sha256"]
                    and value.get("skipped_reason") == "no_audio_stream")
        return (value.get("model") == "mimo-v2.5-asr"
                and value.get("source_sha256") == manifest["sha256"]
                and value.get("configuration", {}).get("endpoint") == TOKEN_PLAN_ENDPOINT
                and value.get("coverage", {}).get("complete") is True
                and value.get("coverage", {}).get("sample_count")
                    == value.get("coverage", {}).get("covered_sample_count")
                and bool(value.get("segments"))
                and all(item.get("finish_reason") == "stop" for item in value["segments"]))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _valid_ocr(path: Path, manifest: dict) -> bool:
    try:
        value = read_json(path)
        frames = value.get("frames", [])
        return (value.get("engine") == "RapidOCR"
                and value.get("sample_interval_seconds") == 1.0
                and value.get("coverage_verified") is True
                and value.get("source_fingerprint", {}).get("sha256") == manifest["sha256"]
                and value.get("expected_frame_count") == manifest["expected_frame_count"]
                and len(frames) == manifest["expected_frame_count"]
                and [f.get("timestamp_seconds") for f in frames]
                    == list(range(manifest["expected_frame_count"]))
                and all(Path(f.get("image_path", "")).is_file() for f in frames))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _say(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def build_inventory(root: Path, work_root: Path) -> dict:
    videos, courseware = discover(root)
    manifests = []
    for index, source in enumerate(videos, 1):
        _say(f"[INVENTORY {index}/{len(videos)}] {source.name}")
        manifest = inspect_video(source)
        item_work = work_root / manifest["sha256"][:16]
        item_work.mkdir(parents=True, exist_ok=True)
        old_manifest = item_work / "manifest.json"
        if old_manifest.is_file() and read_json(old_manifest).get("sha256") != manifest["sha256"]:
            raise ValueError(f"工作目录源指纹冲突：{source.name}")
        atomic_json(old_manifest, manifest)
        manifests.append(manifest)
    result = {
        "schema_version": "2025-batch-1", "created_at": _now(), "root": str(root.resolve()),
        "video_count": len(manifests), "courseware_count": len(courseware),
        "audio_video_count": sum(m["has_audio"] for m in manifests),
        "silent_video_count": sum(not m["has_audio"] for m in manifests),
        "total_video_seconds": sum(m["duration_seconds"] for m in manifests),
        "videos": manifests,
        "courseware": [{"source_path": str(p.resolve()), "source_name": p.name,
                        "sha256": _sha256(p), "size_bytes": p.stat().st_size,
                        "extension": p.suffix.lower()} for p in courseware],
    }
    return result


def _load_or_inventory(root: Path, work_root: Path, batch_dir: Path) -> dict:
    inventory_path = batch_dir / "inventory.json"
    current = build_inventory(root, work_root)
    if inventory_path.is_file():
        previous = read_json(inventory_path)
        if [(v["source_name"], v["sha256"]) for v in previous.get("videos", [])] != [
                (v["source_name"], v["sha256"]) for v in current["videos"]]:
            raise ValueError("2025视频集合已变化；请检查后再继续")
    atomic_json(inventory_path, current)
    return current


def _run_parallel(label: str, tasks: list[dict], workers: int,
                  operation: Callable[[dict, int, int], None], batch_dir: Path,
                  *, state_tag: str = "") -> None:
    suffix = f"-{state_tag}" if state_tag else ""
    state_path = batch_dir / f"{label.lower()}{suffix}-state.json"
    state = {"stage": label, "status": "running", "started_at": _now(),
             "total": len(tasks), "completed": [], "failed": {}}
    atomic_json(state_path, state)
    lock = threading.Lock()

    def one(item: dict, sequence: int) -> None:
        operation(item, sequence, len(tasks))
        with lock:
            state["completed"].append(item["source_name"])
            atomic_json(state_path, state)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=label.lower()) as executor:
        futures = {executor.submit(one, item, index): item
                   for index, item in enumerate(tasks, 1)}
        for future in as_completed(futures):
            item = futures[future]
            try:
                future.result()
            except Exception as exc:
                with lock:
                    state["failed"][item["source_name"]] = f"{type(exc).__name__}: {exc}"
                    atomic_json(state_path, state)
                _say(f"[{label} FAILED] {item['source_name']}: {type(exc).__name__}: {exc}")
    state.update(status="complete" if not state["failed"] else "failed", ended_at=_now())
    atomic_json(state_path, state)
    if state["failed"]:
        raise RuntimeError(f"{label}有{len(state['failed'])}个文件失败；可直接重跑续传")


def run_asr(inventory: dict, work_root: Path, batch_dir: Path, key: str, workers: int) -> None:
    from .asr import transcribe

    tasks = [m for m in inventory["videos"] if not _valid_asr(
        work_root / m["sha256"][:16] / "asr.json", m)]

    def operation(manifest: dict, sequence: int, total: int) -> None:
        item_work = work_root / manifest["sha256"][:16]
        output = item_work / "asr.json"
        _say(f"[ASR {sequence}/{total}] {manifest['source_name']}")
        if not manifest["has_audio"]:
            atomic_json(output, {"model": "mimo-v2.5-asr", "source_sha256": manifest["sha256"],
                                 "has_audio": False, "skipped_reason": "no_audio_stream",
                                 "segments": [], "coverage": {"complete": True, "segment_count": 0}})
            _say(f"[ASR SKIP NO AUDIO] {manifest['source_name']}")
            return
        last = {"completed": 0, "total": 0}

        def progress(value: dict) -> None:
            last.update(completed=value["completed"], total=value["total"])
            if value["completed"] % 10 == 0 or value["completed"] == value["total"]:
                _say(f"[ASR {sequence}/{total}] {manifest['source_name']} "
                     f"{value['completed']}/{value['total']} segments")

        result = transcribe(
            Path(manifest["source_path"]), item_work, key,
            chunk_seconds=55.0, progress=progress, endpoint=TOKEN_PLAN_ENDPOINT,
        )
        atomic_json(output, result)
        if not _valid_asr(output, manifest):
            raise ValueError("ASR完成结果未通过源指纹/覆盖校验")
        _say(f"[ASR DONE {sequence}/{total}] {manifest['source_name']}")

    _run_parallel("ASR", tasks, workers, operation, batch_dir)


def run_ocr(inventory: dict, work_root: Path, batch_dir: Path, workers: int,
            maximum_dimension: int, *, shard_index: int = 0, shard_count: int = 1) -> None:
    from .ocr import recognize

    tasks = [m for index, m in enumerate(inventory["videos"])
             if index % shard_count == shard_index and not _valid_ocr(
                 work_root / m["sha256"][:16] / "ocr.json", m)]

    def operation(manifest: dict, sequence: int, total: int) -> None:
        item_work = work_root / manifest["sha256"][:16]
        output = item_work / "ocr.json"
        _say(f"[OCR {sequence}/{total}] {manifest['source_name']}")

        def progress(value: dict) -> None:
            completed, expected = value["completed_frames"], value["expected_frame_count"]
            if completed % 300 == 0 or completed == expected:
                _say(f"[OCR {sequence}/{total}] {manifest['source_name']} {completed}/{expected}")

        result = recognize(Path(manifest["source_path"]), item_work,
                           maximum_dimension=maximum_dimension, progress=progress)
        atomic_json(output, result)
        if not _valid_ocr(output, manifest):
            raise ValueError("OCR完成结果未通过源指纹/逐秒覆盖校验")
        _say(f"[OCR DONE {sequence}/{total}] {manifest['source_name']}")

    tag = f"shard-{shard_index + 1}-of-{shard_count}" if shard_count > 1 else ""
    _run_parallel("OCR", tasks, workers, operation, batch_dir, state_tag=tag)


def status(inventory: dict, work_root: Path) -> dict:
    videos = inventory["videos"]
    return {
        "videos": len(videos),
        "asr_complete": sum(_valid_asr(work_root / m["sha256"][:16] / "asr.json", m) for m in videos),
        "ocr_complete": sum(_valid_ocr(work_root / m["sha256"][:16] / "ocr.json", m) for m in videos),
        "expected_ocr_frames": sum(m["expected_frame_count"] for m in videos),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="2025全部视频断点式MiMo ASR与逐秒RapidOCR")
    parser.add_argument("--root", type=Path, default=Path("data/2025"))
    parser.add_argument("--work-root", type=Path, default=Path(".work/single-video"))
    parser.add_argument("--batch-dir", type=Path, default=Path(".work/batch-2025"))
    parser.add_argument("--stage", choices=("inventory", "asr", "ocr", "status"), required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--ocr-maximum-dimension", type=int, default=1600)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0,
                        help="从0开始；按完整视频清单的稳定序号分片")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("--workers 必须在1—4之间")
    if not 1 <= args.shard_count <= 8 or not 0 <= args.shard_index < args.shard_count:
        parser.error("分片参数必须满足 1 ≤ shard-count ≤ 8 且 0 ≤ shard-index < shard-count")
    args.work_root, args.batch_dir = args.work_root.resolve(), args.batch_dir.resolve()
    args.work_root.mkdir(parents=True, exist_ok=True)
    args.batch_dir.mkdir(parents=True, exist_ok=True)
    try:
        inventory = _load_or_inventory(args.root, args.work_root, args.batch_dir)
        if args.stage == "asr":
            key = os.environ.get("MIMO_API_KEY")
            if not key:
                if not sys.stdin.isatty():
                    raise ValueError("需要MIMO_API_KEY或终端隐藏输入")
                key = getpass.getpass("MiMo API key (hidden): ").strip()
            run_asr(inventory, args.work_root, args.batch_dir, key, args.workers)
            del key
        elif args.stage == "ocr":
            run_ocr(inventory, args.work_root, args.batch_dir, args.workers,
                    args.ocr_maximum_dimension, shard_index=args.shard_index,
                    shard_count=args.shard_count)
        result = status(inventory, args.work_root)
        atomic_json(args.batch_dir / "status.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
