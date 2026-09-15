"""Merge independently inspected frame ranges into a traceable review artifact."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from .runner import atomic_json, read_json
from .report import _apply_review


def merge_reviews(work: Path, base: Path, parts: list[Path]) -> dict:
    review = read_json(base)
    manifest = read_json(work / "manifest.json")
    if review.get("source_sha256") != manifest["sha256"]:
        raise ValueError("Base review belongs to a different video")
    review.setdefault("ocr_corrections", [])
    review.setdefault("ocr_supplements", [])
    for path in parts:
        part = read_json(path)
        if part.get("source_sha256", manifest["sha256"]) != manifest["sha256"]:
            raise ValueError(f"Review fragment belongs to a different video: {path.name}")
        for key in ("ocr_corrections", "ocr_supplements", "notes", "unresolved", "checked_frames"):
            review.setdefault(key, []).extend(part.get(key, []))
    review["checked_frames"] = sorted(set(review.get("checked_frames", [])))
    review["reviewed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    fixed_texts = {item["text"] for item in review.get("persistent_texts", [])}
    restored = {(item["frame_index"], item["new"]) for item in review["ocr_corrections"]
                if item["new"] in fixed_texts}
    for item in review["ocr_corrections"]:
        # Explicitly reviewed watermark duplicates only; never classify name merges this way.
        if item["new"] == "" and "水印" in item["reason"]:
            candidates = [text for text in fixed_texts if (item["frame_index"], text) in restored]
            if len(candidates) == 1:
                item["persistent_text"] = candidates[0]
    count = len(review["ocr_corrections"])
    review["notes"].append(
        "已确认的固定栏目标语和水印集中显示并保留出现时间；其余变化字幕按时间顺序整理。"
        f"所有原始逐帧结果与完整{count}条OCR修改记录保存在本次任务缓存中。"
    )
    _apply_review(read_json(work / "asr.json"), read_json(work / "ocr.json"), review)
    atomic_json(work / "review.json", review)
    return review


def main() -> None:
    parser = argparse.ArgumentParser(description="合并同一视频的并行读图复核记录")
    parser.add_argument("work", type=Path)
    parser.add_argument("base", type=Path)
    parser.add_argument("parts", type=Path, nargs="+")
    args = parser.parse_args()
    review = merge_reviews(args.work, args.base, args.parts)
    print(json.dumps({"checked_frames": len(review["checked_frames"]),
                      "asr_corrections": len(review.get("asr_corrections", [])),
                      "ocr_corrections": len(review["ocr_corrections"]),
                      "ocr_supplements": len(review["ocr_supplements"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
