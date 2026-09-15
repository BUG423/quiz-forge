"""Select and independently inspect representative frames for the 2025 corpus.

RapidOCR still covers every second.  This stage deliberately sends a compact,
source-bound selection to MiMo vision so visual checking remains tractable while
retaining the older reviewed report's key-frame anchors.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

from .runner import atomic_json, read_json
from .vision import review_images


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value).casefold())


def _legacy_seconds(label: str) -> float | None:
    match = re.search(r"(\d+):(\d+):(\d+(?:\.\d+)?)", str(label))
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _frame_quality(frame: dict[str, Any]) -> tuple[float, int, int]:
    lines = frame.get("lines", [])
    useful = [line for line in lines if len(_text_key(line.get("text", ""))) >= 2]
    characters = sum(len(_text_key(line.get("text", ""))) for line in useful)
    confidence = sum(float(line.get("confidence", 0)) for line in useful)
    return confidence + min(characters, 100) / 100.0, characters, -int(frame["index"])


def select_frames(manifest: dict, ocr: dict, legacy: dict | None) -> list[dict]:
    frames = ocr.get("frames", [])
    if not frames:
        raise ValueError("逐秒OCR结果为空，不能选择视觉复核帧")
    duration = float(manifest["duration_seconds"])
    # Normal videos receive up to 48 frames; silent videos get denser visual
    # coverage because the image track is then the only semantic source.
    limit = 72 if not manifest.get("has_audio") else 48
    limit = min(limit, len(frames))
    anchors: list[float] = [0.0, max(0.0, duration - 1.0)]
    if legacy:
        for block in legacy.get("ocr_blocks", []):
            seconds = _legacy_seconds(block.get("label", ""))
            if seconds is not None and 0 <= seconds < duration:
                anchors.append(seconds)
    # Fill long gaps even when the prior report selected few frames.
    interval = max(45.0, duration / max(1, limit - 8))
    anchors.extend(float(value) for value in range(0, math.ceil(duration), max(1, round(interval))))

    selected: dict[int, dict] = {}
    for anchor in anchors:
        center = max(0, min(len(frames) - 1, round(anchor)))
        candidates = frames[max(0, center - 2):min(len(frames), center + 3)]
        best = max(candidates, key=_frame_quality)
        selected[int(best["index"])] = best

    # Add visually dense, novel OCR states until the cap is reached.  Repeated
    # subtitles/watermarks collapse to the same normalized signature.
    ranked: list[tuple[tuple[float, int, int], str, dict]] = []
    for frame in frames:
        lines = [_text_key(line.get("text", "")) for line in frame.get("lines", [])]
        signature = "|".join(value for value in lines if len(value) >= 2)
        if signature:
            ranked.append((_frame_quality(frame), signature, frame))
    seen_signatures = {
        "|".join(_text_key(line.get("text", "")) for line in frame.get("lines", [])
                 if len(_text_key(line.get("text", ""))) >= 2)
        for frame in selected.values()
    }
    for _, signature, frame in sorted(ranked, reverse=True):
        if len(selected) >= limit:
            break
        if signature in seen_signatures:
            continue
        # Do not cluster additions within five seconds of an existing choice.
        if any(abs(int(frame["index"]) - index) < 5 for index in selected):
            continue
        selected[int(frame["index"])] = frame
        seen_signatures.add(signature)
    return [selected[index] for index in sorted(selected)][:limit]


def _valid_vision(path: Path, manifest: dict) -> bool:
    try:
        value = read_json(path)
        frames = value.get("frames", [])
        return (
            value.get("model") == "mimo-v2.5"
            and value.get("source_sha256") == manifest["sha256"]
            and value.get("coverage", {}).get("complete_for_supplied_frames") is True
            and bool(frames)
            and len(frames) == value.get("coverage", {}).get("reviewed_frame_count")
            and len({frame.get("frame_index") for frame in frames}) == len(frames)
            and all(Path(frame.get("image_path", "")).is_file() for frame in frames)
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="2025视频代表帧MiMo视觉复核")
    parser.add_argument("--inventory", type=Path, default=Path(".work/batch-2025/inventory.json"))
    parser.add_argument("--legacy", type=Path, default=Path(".work/batch-2025/legacy_reference.json"))
    parser.add_argument("--work-root", type=Path, default=Path(".work/single-video"))
    parser.add_argument("--batch-dir", type=Path, default=Path(".work/batch-2025"))
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--select-only", action="store_true",
                        help="只固化代表帧清单，不调用外部视觉接口")
    args = parser.parse_args()
    try:
        inventory = read_json(args.inventory.resolve())
        legacy_data = read_json(args.legacy.resolve())
        legacy = {item["source_name"]: item for item in legacy_data.get("files", [])}
        tasks = []
        for manifest in inventory["videos"]:
            work = args.work_root.resolve() / manifest["sha256"][:16]
            ocr_path, vision_path = work / "ocr.json", work / "vision.json"
            if not ocr_path.is_file():
                raise ValueError(f"缺少逐秒OCR：{manifest['source_name']}")
            if not _valid_vision(vision_path, manifest):
                tasks.append((manifest, work, read_json(ocr_path)))
        key = os.environ.get("MIMO_API_KEY")
        if tasks and not args.select_only and not key:
            if not sys.stdin.isatty():
                raise ValueError("需要MIMO_API_KEY或终端隐藏输入")
            key = getpass.getpass("MiMo API key (hidden): ").strip()
        state = {"stage": "VISION", "status": "running", "started_at": _now(),
                 "total": len(tasks), "completed": [], "failed": {}}
        state_path = args.batch_dir.resolve() / "vision-state.json"
        atomic_json(state_path, state)
        for sequence, (manifest, work, ocr) in enumerate(tasks, 1):
            name = manifest["source_name"]
            try:
                frames = select_frames(manifest, ocr, legacy.get(name))
                atomic_json(work / "vision_selection.json", {
                    "source_sha256": manifest["sha256"],
                    "strategy": "legacy-anchors-periodic-and-novel-ocr-v1",
                    "selected_frames": [frame["index"] for frame in frames],
                })
                if args.select_only:
                    state["completed"].append(name)
                    atomic_json(state_path, state)
                    print(f"[VISION SELECTED {sequence}/{len(tasks)}] {name} · {len(frames)}帧", flush=True)
                    continue
                print(f"[VISION {sequence}/{len(tasks)}] {name} · {len(frames)}帧", flush=True)
                result = review_images(
                    frames, work, key or "", source_sha256=manifest["sha256"],
                    batch_size=args.batch_size,
                    progress=lambda value, n=name: print(
                        f"[VISION] {n} {value['completed_frames']}/{value['expected_frame_count']}",
                        flush=True),
                )
                atomic_json(work / "vision.json", result)
                if not _valid_vision(work / "vision.json", manifest):
                    raise ValueError("视觉结果未通过源指纹/覆盖校验")
                state["completed"].append(name)
                atomic_json(state_path, state)
            except Exception as exc:
                state["failed"][name] = f"{type(exc).__name__}: {exc}"
                atomic_json(state_path, state)
                print(f"[VISION FAILED] {name}: {type(exc).__name__}: {exc}", flush=True)
        state.update(status="complete" if not state["failed"] else "failed", ended_at=_now())
        atomic_json(state_path, state)
        if state["failed"]:
            raise RuntimeError(f"VISION有{len(state['failed'])}个文件失败；可直接重跑续传")
        print(f"VISION COMPLETE {len(tasks)} files", flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
