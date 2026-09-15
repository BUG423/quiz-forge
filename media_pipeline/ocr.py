"""Complete one-second video sampling and resumable, local RapidOCR inference.

Sampling uses presentation timestamps, never frame-number/fps arithmetic.  Each
integer second in [0, video duration) is assigned the nearest decoded frame;
ties choose the earlier frame.  The decoder is exhausted even after the last
sample, so a prematurely truncated video cannot be reported as complete.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import av
import cv2


SCHEMA_VERSION = 1
SAMPLE_INTERVAL_SECONDS = 1.0
SAMPLING_STRATEGY = "nearest_pts_ties_choose_earlier"
OCR_PARAMS = {
    # Scores are retained for review, including text below RapidOCR's 0.5 default.
    "Global.text_score": 0.0,
    "Global.log_level": "warning",
    "Global.max_side_len": 4096,
    "Global.use_det": True,
    "Global.use_cls": True,
    "Global.use_rec": True,
    # min preserves full resolution; low-resolution subtitles are enlarged.
    "Det.limit_side_len": 736,
    "Det.limit_type": "min",
    "EngineConfig.onnxruntime.intra_op_num_threads": 2,
    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    "EngineConfig.onnxruntime.use_cuda": False,
}


class OCRProcessingError(RuntimeError):
    """OCR, media coverage or evidence failure; never an empty success."""


@dataclass(frozen=True)
class VideoInfo:
    duration: Fraction
    origin: Fraction
    frame_period: Fraction
    width: int
    height: int


def sample_times(duration_seconds: float | Fraction) -> list[float]:
    """Include a fractional last second (271.04 seconds -> 272 samples)."""
    duration = Fraction(str(duration_seconds))
    if duration <= 0:
        raise OCRProcessingError("Video duration must be positive")
    return [float(index) for index in range(math.ceil(duration))]


def _fingerprint(source: Path) -> dict:
    before = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OCRProcessingError("Source changed while fingerprinting")
    return {"sha256": digest.hexdigest(), "size_bytes": after.st_size}


def _read_video_info(source: Path) -> VideoInfo:
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise OCRProcessingError("Source has no video stream")
        stream = container.streams.video[0]
        time_base = Fraction(stream.time_base)
        stream_start = Fraction(stream.start_time or 0) * time_base
        origin = (
            Fraction(container.start_time, av.time_base)
            if container.start_time is not None else stream_start
        )
        if stream.duration is not None:
            duration = stream_start + Fraction(stream.duration) * time_base - origin
        elif container.duration is not None:
            duration = Fraction(container.duration, av.time_base)
        else:
            raise OCRProcessingError("Video duration is unavailable")
        if duration <= 0:
            raise OCRProcessingError("Video duration is not positive")
        rate = stream.average_rate or stream.base_rate
        period = 1 / Fraction(rate) if rate else Fraction(1, 25)
        return VideoInfo(duration, origin, period, stream.width, stream.height)


def _decoded_frames(source: Path, info: VideoInfo) -> Iterator[tuple[Fraction, Any]]:
    """Yield PTS relative to container start, sharing the ASR timeline."""
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        stream.thread_count = 2
        previous_time = None
        decoded_end = None
        frame_count = 0
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise OCRProcessingError("Decoded frame has no presentation timestamp")
            timestamp = Fraction(frame.pts) * Fraction(frame.time_base) - info.origin
            if previous_time is not None and timestamp < previous_time:
                raise OCRProcessingError("Decoded frame timestamps moved backwards")
            if timestamp < 0:
                raise OCRProcessingError("Decoded frame predates the video start timestamp")
            frame_duration = (
                Fraction(frame.duration) * Fraction(frame.time_base)
                if frame.duration and frame.duration > 0
                else info.frame_period
            )
            decoded_end = timestamp + frame_duration
            previous_time = timestamp
            frame_count += 1
            yield timestamp, frame
        if not frame_count or decoded_end is None:
            raise OCRProcessingError("No video frames decoded")
        # Permit at most one nominal frame of metadata rounding, not missing seconds.
        # Legacy WMV containers in the supplied PPTX report a stream duration
        # up to roughly 0.2 s beyond the final decoded frame.  The last integer
        # second is still represented, so allow only that format a 0.25 s
        # metadata-rounding window; all other formats keep the one-frame rule.
        tolerance = max(
            info.frame_period,
            Fraction(1, 4) if source.suffix.lower() == ".wmv" else Fraction(1, 1000),
        )
        if decoded_end + tolerance < info.duration:
            raise OCRProcessingError(
                f"Incomplete video decode: {float(decoded_end):.6f}s of "
                f"{float(info.duration):.6f}s"
            )


def _nearest_frames(
    decoded: Iterable[tuple[Fraction, Any]], targets: list[float]
) -> Iterator[tuple[int, float, Fraction, Any]]:
    """Streaming nearest-neighbour selection with earlier-frame tie breaking."""
    target_index = 0
    previous = None
    for current in decoded:
        current_time, _ = current
        while target_index < len(targets) and current_time >= targets[target_index]:
            target = Fraction(str(targets[target_index]))
            selected = current
            if previous is not None and target - previous[0] <= current_time - target:
                selected = previous
            yield target_index, targets[target_index], selected[0], selected[1]
            target_index += 1
        previous = current
    if previous is None:
        raise OCRProcessingError("No frames available for sampling")
    while target_index < len(targets):
        yield target_index, targets[target_index], previous[0], previous[1]
        target_index += 1


def _new_engine(params: dict):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    # RapidOCR's configuration update requires Enum objects for these fields.
    configured = dict(params)
    for section in ("Det", "Rec"):
        configured[f"{section}.engine_type"] = EngineType.ONNXRUNTIME
        configured[f"{section}.ocr_version"] = OCRVersion.PPOCRV6
        configured[f"{section}.model_type"] = ModelType.SMALL
    return RapidOCR(params=configured)


def _recognize_lines(engine, image) -> list[dict]:
    output = engine(image, text_score=0.0)
    if output is None or not all(hasattr(output, key) for key in ("txts", "scores", "boxes")):
        raise OCRProcessingError("RapidOCR returned an invalid response")
    texts, scores, boxes = output.txts, output.scores, output.boxes
    if texts is None and scores is None and boxes is None:
        return []
    if any(value is None for value in (texts, scores, boxes)):
        raise OCRProcessingError("RapidOCR returned incomplete recognition fields")
    if not (len(texts) == len(scores) == len(boxes)):
        raise OCRProcessingError("RapidOCR returned mismatched text/score/box counts")
    lines = []
    for text, score, box in zip(texts, scores, boxes):
        score = float(score)
        coordinates = [[float(point[0]), float(point[1])] for point in box]
        if (
            not isinstance(text, str)
            or not math.isfinite(score)
            or not 0 <= score <= 1
            or len(coordinates) != 4
            or not all(math.isfinite(value) for point in coordinates for value in point)
        ):
            raise OCRProcessingError("RapidOCR returned malformed recognition data")
        lines.append({"text": text, "confidence": score, "box": coordinates})
    return lines


def _atomic_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(destination: Path, payload: dict) -> None:
    _atomic_bytes(
        destination,
        (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"),
    )


def _cached_frame(cache_path: Path, image_path: Path, signature: str, index: int,
                  target: float, actual: Fraction) -> dict | None:
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached["signature"] != signature:
            return None
        result = cached["frame"]
        if (result["index"] != index or result["timestamp_seconds"] != target
                or abs(result["actual_timestamp_seconds"] - float(actual)) > 1e-9
                or result["image_path"] != str(image_path)
                or not isinstance(result["lines"], list)):
            return None
        for line in result["lines"]:
            if (not isinstance(line["text"], str)
                    or not isinstance(line["confidence"], (int, float))
                    or not 0 <= line["confidence"] <= 1
                    or len(line["box"]) != 4
                    or any(len(point) != 2 for point in line["box"])
                    or any(not math.isfinite(value) for point in line["box"] for value in point)):
                return None
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != cached["image_sha256"]:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def recognize(source: Path, work_dir: Path, *, maximum_dimension: int | None = None,
              progress: Callable[[dict], None] | None = None) -> dict:
    """Recognize every one-second sample and preserve full-frame JPEG evidence.

    Per-frame cache identity includes the source's complete SHA-256, engine
    version, OCR parameters, sampling strategy and media metadata.  Cache reuse
    still decodes the entire source and checks selected PTS and evidence hashes.
    Exceptions propagate; only a successfully exhausted decoder returns a result.
    """
    source, work_dir = Path(source).resolve(), Path(work_dir).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if (maximum_dimension is not None
            and (not isinstance(maximum_dimension, int) or isinstance(maximum_dimension, bool)
                 or maximum_dimension < 640)):
        raise ValueError("maximum_dimension 必须为空或大于等于640的整数")
    source_stat = source.stat()
    fingerprint = _fingerprint(source)
    info = _read_video_info(source)
    targets = sample_times(info.duration)
    params = dict(OCR_PARAMS)
    # Preserve the full input even when it exceeds the normal 4096 px cap.
    params["Global.max_side_len"] = max(params["Global.max_side_len"], info.width, info.height)
    config = {
        "schema_version": SCHEMA_VERSION,
        "source_fingerprint": fingerprint,
        "engine": "RapidOCR",
        "engine_version": importlib.metadata.version("rapidocr"),
        "det_model": "PP-OCRv6/ch/small/onnxruntime",
        "rec_model": "PP-OCRv6/ch/small/onnxruntime",
        "cls_model": "PP-OCRv4/ch/mobile/onnxruntime",
        "params": params,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "sampling_strategy": SAMPLING_STRATEGY,
        "duration_seconds": float(info.duration),
        "timeline_origin_seconds": float(info.origin),
        "width": info.width,
        "height": info.height,
        "preprocess_maximum_dimension": maximum_dimension,
        "jpeg_quality": 95,
    }
    signature = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    cache_dir = work_dir / "ocr_cache" / signature
    image_dir = work_dir / "ocr_frames" / signature
    _atomic_json(cache_dir / "config.json", {"signature": signature, **config})
    engine = None
    results = []
    cache_hits = 0
    for index, target, actual, frame in _nearest_frames(_decoded_frames(source, info), targets):
        stem = f"frame_{index:06d}"
        image_path, cache_path = image_dir / f"{stem}.jpg", cache_dir / f"{stem}.json"
        result = _cached_frame(cache_path, image_path, signature, index, target, actual)
        cached = result is not None
        if cached:
            cache_hits += 1
        else:
            if engine is None:
                engine = _new_engine(params)
            image = frame.to_ndarray(format="bgr24")
            if maximum_dimension is not None and max(image.shape[:2]) > maximum_dimension:
                scale = maximum_dimension / max(image.shape[:2])
                image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            lines = _recognize_lines(engine, image)
            success, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not success:
                raise OCRProcessingError(f"Could not save evidence at {target}s")
            encoded = encoded.tobytes()
            _atomic_bytes(image_path, encoded)
            result = {
                "index": index,
                "timestamp_seconds": target,
                "actual_timestamp_seconds": float(actual),
                "image_path": str(image_path),
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "lines": lines,
            }
            _atomic_json(cache_path, {
                "signature": signature,
                "image_sha256": hashlib.sha256(encoded).hexdigest(),
                "frame": result,
            })
        results.append(result)
        if progress is not None and (len(results) == 1 or len(results) % 10 == 0 or len(results) == len(targets)):
            progress({
                "stage": "ocr", "completed_frames": len(results),
                "expected_frame_count": len(targets), "timestamp_seconds": target,
                "cached": cached,
            })
    final_stat = source.stat()
    if (source_stat.st_size, source_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
        raise OCRProcessingError("Source changed during video recognition")
    if len(results) != len(targets):
        raise OCRProcessingError(f"Incomplete sampling: {len(results)}/{len(targets)} frames")
    return {
        "engine": "RapidOCR",
        "engine_version": config["engine_version"],
        "models": {key: config[key] for key in ("det_model", "rec_model", "cls_model")},
        "duration_seconds": float(info.duration),
        "timeline_origin_seconds": float(info.origin),
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "sampling_strategy": SAMPLING_STRATEGY,
        "expected_frame_count": len(targets),
        "frames": results,
        "empty_frame_count": sum(not item["lines"] for item in results),
        "cache_hits": cache_hits,
        "source_fingerprint": fingerprint,
        "cache_signature": signature,
        "config": config,
        "coverage_verified": True,
    }
