#!/usr/bin/env python3
"""Populate one disjoint range of an existing video OCR cache.

This is a throughput helper only.  It uses the exact cache configuration and
sampling implementation from ``media_pipeline.ocr``; the normal full-video
run remains responsible for end-to-end decoding, ordering, and coverage QA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2

from media_pipeline import ocr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    args = parser.parse_args()
    source, work_dir = args.source.resolve(strict=True), args.work_dir.resolve(strict=True)
    if args.start < 0 or args.end <= args.start:
        raise ValueError("帧区间必须满足 0 <= start < end")

    cache_roots = [
        path for path in (work_dir / "ocr_cache").iterdir()
        if path.is_dir() and (path / "config.json").is_file()
    ]
    if len(cache_roots) != 1:
        raise RuntimeError(f"需要且只能有一个现有OCR配置，实际{len(cache_roots)}个")
    cache_dir = cache_roots[0]
    config = json.loads((cache_dir / "config.json").read_text(encoding="utf-8"))
    signature = config["signature"]
    image_dir = work_dir / "ocr_frames" / signature
    if config["source_fingerprint"]["sha256"] != ocr._fingerprint(source)["sha256"]:
        raise RuntimeError("源文件SHA-256与现有OCR缓存不一致")

    info = ocr._read_video_info(source)
    targets = ocr.sample_times(info.duration)
    end = min(args.end, len(targets))
    if args.start >= end:
        raise ValueError("起始帧超出视频采样范围")
    maximum_dimension = config.get("preprocess_maximum_dimension")
    engine = None
    created = reused = 0
    for index, target, actual, frame in ocr._nearest_frames(ocr._decoded_frames(source, info), targets):
        if index >= end:
            break
        if index < args.start:
            continue
        stem = f"frame_{index:06d}"
        image_path = image_dir / f"{stem}.jpg"
        cache_path = cache_dir / f"{stem}.json"
        cached = ocr._cached_frame(cache_path, image_path, signature, index, target, actual)
        if cached is not None:
            reused += 1
            continue
        if engine is None:
            engine = ocr._new_engine(config["params"])
        image = frame.to_ndarray(format="bgr24")
        if maximum_dimension is not None and max(image.shape[:2]) > maximum_dimension:
            scale = maximum_dimension / max(image.shape[:2])
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        lines = ocr._recognize_lines(engine, image)
        success, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not success:
            raise ocr.OCRProcessingError(f"Could not save evidence at {target}s")
        payload = encoded.tobytes()
        ocr._atomic_bytes(image_path, payload)
        result = {
            "index": index,
            "timestamp_seconds": target,
            "actual_timestamp_seconds": float(actual),
            "image_path": str(image_path),
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "lines": lines,
        }
        ocr._atomic_json(cache_path, {
            "signature": signature,
            "image_sha256": hashlib.sha256(payload).hexdigest(),
            "frame": result,
        })
        created += 1
        if created % 50 == 0:
            print(f"[{args.start}:{end}] 新增 {created} 帧", flush=True)
    print(json.dumps({"range": [args.start, end], "created": created, "reused": reused}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
