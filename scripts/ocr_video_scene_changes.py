#!/usr/bin/env python3
"""Supplement one-second OCR with conservative scene/text-change frames.

This program does not revisit the 71,862 existing one-second OCR frames.  It
decodes each source once at a small detector resolution, confirms a stable
frame after each strong scene change, rejects candidates
whose standardized JPEG SHA-256 already exists in the one-second cache, and
runs lossless-record RapidOCR only on the remaining frames.

Recognition records are deliberately raw: every RapidOCR string (including a
single character, a duplicate, or a low-confidence result) is retained with
its confidence and box.  Decode and cache-integrity failures are fatal and are
also written to the partial output; they can never become an empty success.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, Iterable, Iterator

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_pipeline.ocr import (
    OCR_PARAMS,
    OCRProcessingError,
    _decoded_frames,
    _fingerprint,
    _new_engine,
    _read_video_info,
    _recognize_lines,
)


SCHEMA_VERSION = "video-scene-ocr-v2"
PROCESSOR_VERSION = "scene-change-lossless-v2"
TOP_LEVEL_VIDEO_COUNT = 63
EMBEDDED_VIDEO_COUNT = 4
SOURCE_COUNT = TOP_LEVEL_VIDEO_COUNT + EMBEDDED_VIDEO_COUNT
EXISTING_SECOND_FRAME_COUNT = 71_862
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

DEFAULT_CONFIG: dict[str, Any] = {
    "detector_max_dimension": 320,
    "scene_change_threshold": 0.10,
    "stability_threshold": 0.025,
    "stability_confirmation_frames": 3,
    "minimum_event_gap_seconds": 0.75,
    "text_edge_trigger_enabled": False,
    "maximum_candidate_rate_per_second": 1.0,
    "comparison_maximum_dimension": 1600,
    "ocr_maximum_dimension": 1600,
    "jpeg_quality": 95,
    "selection": "first_last_and_stable_frame_after_strong_scene_change",
    "confidence_filter": None,
    "text_correction": False,
    "text_deduplication": False,
    "drop_single_character": False,
}


class SceneOCRError(RuntimeError):
    """Input, decode, OCR, or evidence-integrity failure."""


@dataclass(frozen=True)
class VideoSource:
    asset_id: str
    source_path: Path
    source_sha256: str
    source_name: str
    year: int
    source_type: str
    parent_source: str | None = None
    parent_slide: int | None = None
    archive_member: str | None = None


@dataclass(frozen=True)
class Candidate:
    pts: int
    time_base: Fraction
    timestamp: Fraction
    image: np.ndarray
    reasons: tuple[str, ...]
    scene_change_score: float
    text_edge_change_score: float


@dataclass(frozen=True)
class ExistingSecondFrames:
    hashes: frozenset[str]
    frame_count: int


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise SceneOCRError(f"无法读取有效JSON：{path}") from exc
    if not isinstance(value, dict):
        raise SceneOCRError(f"JSON顶层必须是对象：{path}")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value.lower()) is None:
        raise SceneOCRError(f"{field}必须是64位SHA-256")
    return value.lower()


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SceneOCRError(f"{field}必须是非空字符串")
    return value


def _resolve_source(path_value: str, root: Path) -> Path:
    path = Path(path_value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_file():
        raise SceneOCRError(f"视频源不存在：{resolved}")
    return resolved


def discover_sources(
    asset_manifest: Path,
    embedded_manifest: Path,
    *,
    root: Path = ROOT,
    expected_top_level: int = TOP_LEVEL_VIDEO_COUNT,
    expected_embedded: int = EMBEDDED_VIDEO_COUNT,
) -> list[VideoSource]:
    """Read the two authoritative manifests without scanning arbitrary files."""
    root = Path(root).resolve()
    manifest = read_json(asset_manifest)
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise SceneOCRError("asset_manifest.assets必须是数组")
    top_level: list[VideoSource] = []
    for index, item in enumerate(assets, 1):
        if not isinstance(item, dict):
            raise SceneOCRError(f"asset_manifest.assets第{index}项必须是对象")
        if item.get("kind") != "video":
            continue
        year = item.get("year")
        if not isinstance(year, int) or isinstance(year, bool):
            raise SceneOCRError(f"assets第{index}项year非法")
        source_path_value = _string(item.get("source_path"), f"assets[{index}].source_path")
        top_level.append(VideoSource(
            asset_id=_string(item.get("asset_id"), f"assets[{index}].asset_id"),
            source_path=_resolve_source(source_path_value, root),
            source_sha256=_sha(item.get("source_sha256"), f"assets[{index}].source_sha256"),
            source_name=_string(item.get("source_name"), f"assets[{index}].source_name"),
            year=year,
            source_type="top_level",
        ))
    if len(top_level) != expected_top_level:
        raise SceneOCRError(
            f"顶层视频必须恰好为{expected_top_level}个，实际{len(top_level)}个")

    embedded_document = read_json(embedded_manifest)
    embedded_files = embedded_document.get("files")
    if not isinstance(embedded_files, list) or len(embedded_files) != expected_embedded:
        actual = len(embedded_files) if isinstance(embedded_files, list) else "非法"
        raise SceneOCRError(f"内嵌媒体必须恰好为{expected_embedded}个，实际{actual}个")
    if embedded_document.get("file_count") != len(embedded_files):
        raise SceneOCRError("embedded manifest的file_count与files不一致")
    embedded: list[VideoSource] = []
    for index, item in enumerate(embedded_files, 1):
        if not isinstance(item, dict):
            raise SceneOCRError(f"embedded files第{index}项必须是对象")
        sha = _sha(item.get("sha256"), f"embedded[{index}].sha256")
        source_path_value = _string(item.get("output_path"), f"embedded[{index}].output_path")
        slide = item.get("slide")
        if not isinstance(slide, int) or isinstance(slide, bool) or slide < 1:
            raise SceneOCRError(f"embedded[{index}].slide必须是正整数")
        source_path = _resolve_source(source_path_value, root)
        embedded.append(VideoSource(
            asset_id=f"embedded-video/{sha}",
            source_path=source_path,
            source_sha256=sha,
            source_name=source_path.name,
            year=2025,
            source_type="embedded",
            parent_source=_string(item.get("parent_source"), f"embedded[{index}].parent_source"),
            parent_slide=slide,
            archive_member=_string(item.get("member"), f"embedded[{index}].member"),
        ))

    sources = top_level + embedded
    ids = [item.asset_id for item in sources]
    shas = [item.source_sha256 for item in sources]
    if len(ids) != len(set(ids)) or len(shas) != len(set(shas)):
        raise SceneOCRError("67个视频源的asset_id或源SHA存在重复")
    return sources


def requires_original_resolution(source: VideoSource) -> bool:
    """The two specified 2025 text-dense top-level videos keep native pixels."""
    return (
        source.source_type == "top_level"
        and source.year == 2025
        and ("《接力》" in source.source_name or "镇荣甲线" in source.source_name)
    )


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


def _atomic_json(destination: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        destination,
        (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(),
    )


def _resize_maximum(image: np.ndarray, maximum: int | None) -> np.ndarray:
    if maximum is None or max(image.shape[:2]) <= maximum:
        return image
    scale = maximum / max(image.shape[:2])
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def _jpeg(image: np.ndarray, quality: int) -> bytes:
    success, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not success:
        raise SceneOCRError("候选帧JPEG编码失败")
    return encoded.tobytes()


def _detector_planes(image: np.ndarray, maximum: int) -> tuple[np.ndarray, np.ndarray]:
    small = _resize_maximum(image, maximum)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    # Canny differences react to subtitle glyph changes even when the full-frame
    # luminance delta is small.  No OCR text is inferred or discarded here.
    edges = cv2.Canny(gray, 48, 144)
    return gray, edges


def _change_scores(
    previous: tuple[np.ndarray, np.ndarray],
    current: tuple[np.ndarray, np.ndarray],
) -> tuple[float, float]:
    previous_gray, previous_edges = previous
    current_gray, current_edges = current
    if previous_gray.shape != current_gray.shape:
        raise SceneOCRError("视频解码过程中画面尺寸发生变化")
    scene = float(cv2.absdiff(previous_gray, current_gray).mean() / 255.0)
    text_edges = float(cv2.absdiff(previous_edges, current_edges).mean() / 255.0)
    return scene, text_edges


def _candidate(
    timestamp: Fraction,
    frame: Any,
    image: np.ndarray,
    reasons: tuple[str, ...],
    scene_score: float,
    text_score: float,
) -> Candidate:
    if frame.pts is None or frame.time_base is None:
        raise SceneOCRError("候选帧缺少PTS或time_base")
    return Candidate(
        pts=int(frame.pts),
        time_base=Fraction(frame.time_base),
        timestamp=timestamp,
        image=image,
        reasons=reasons,
        scene_change_score=scene_score,
        text_edge_change_score=text_score,
    )


def iter_scene_candidates(
    decoded: Iterable[tuple[Fraction, Any]],
    config: dict[str, Any],
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[Candidate]:
    """Yield boundaries and one confirmed-stable frame after each strong cut.

    Canny differences remain diagnostic only.  They can never independently
    create a candidate.  The pre-cut frame is intentionally omitted because it
    is already represented by complete one-second OCR.
    """
    maximum = int(config["detector_max_dimension"])
    scene_threshold = float(config["scene_change_threshold"])
    stability_threshold = float(config["stability_threshold"])
    confirmation_frames = int(config["stability_confirmation_frames"])
    if confirmation_frames not in {2, 3}:
        raise SceneOCRError("stability_confirmation_frames必须是2或3")
    if config.get("text_edge_trigger_enabled") is not False:
        raise SceneOCRError("v2禁止由普通Canny文字边缘变化独立触发")
    gap = Fraction(str(config["minimum_event_gap_seconds"]))
    previous: tuple[Fraction, Any, np.ndarray, tuple[np.ndarray, np.ndarray]] | None = None
    first_emitted = False
    last_event: Fraction | None = None
    pending_scene_score: float | None = None
    pending_text_score = 0.0
    stable_frames = 0
    decoded_count = 0
    for timestamp, frame in decoded:
        image = frame.to_ndarray(format="bgr24")
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
            raise SceneOCRError("解码帧不是有效BGR图像")
        planes = _detector_planes(image, maximum)
        decoded_count += 1
        if not first_emitted:
            yield _candidate(timestamp, frame, image, ("video_start",), 0.0, 0.0)
            first_emitted = True
        if previous is not None:
            scene_score, text_score = _change_scores(previous[3], planes)
            gap_open = last_event is None or timestamp - last_event >= gap
            if scene_score >= scene_threshold and gap_open:
                pending_scene_score = max(pending_scene_score or 0.0, scene_score)
                pending_text_score = max(pending_text_score, text_score)
                stable_frames = 0
            elif pending_scene_score is not None:
                pending_text_score = max(pending_text_score, text_score)
                if scene_score <= stability_threshold:
                    stable_frames += 1
                    if stable_frames >= confirmation_frames:
                        yield _candidate(
                            timestamp, frame, image,
                            ("stable_after_strong_scene_change",),
                            pending_scene_score, pending_text_score)
                        last_event = timestamp
                        pending_scene_score = None
                        pending_text_score = 0.0
                        stable_frames = 0
                else:
                    # A dissolve or multi-frame transition has not settled yet.
                    stable_frames = 0
        previous = (timestamp, frame, image, planes)
        if progress is not None and decoded_count % 500 == 0:
            progress({"stage": "detect", "decoded_frames": decoded_count,
                      "timestamp_seconds": float(timestamp)})
    if previous is None:
        raise SceneOCRError("视频未解码出任何帧")
    yield _candidate(previous[0], previous[1], previous[2], ("video_end",), 0.0, 0.0)


def recognize_raw_lines(engine: Any, image: np.ndarray) -> list[dict[str, Any]]:
    """Return RapidOCR output unchanged in cardinality, wording and order."""
    try:
        return _recognize_lines(engine, image)
    except OCRProcessingError as exc:
        raise SceneOCRError(str(exc)) from exc


def _source_cache_directory(work_dir: Path, source_sha: str, signature: str) -> Path:
    return Path(work_dir) / "video-scene-ocr-cache" / source_sha / signature


def _config_signature(source_sha: str, config: dict[str, Any]) -> str:
    payload = {"processor_version": PROCESSOR_VERSION, "source_sha256": source_sha,
               "config": config}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def existing_second_frame_hashes(
    source: VideoSource, existing_work: Path
) -> ExistingSecondFrames:
    """Read saved SHA values; never OCR or re-encode the 1-second frames."""
    source_work = Path(existing_work) / source.source_sha256[:16]
    result_path = source_work / "ocr.json"
    result = read_json(result_path)
    fingerprint = result.get("source_fingerprint")
    if not isinstance(fingerprint, dict) or fingerprint.get("sha256") != source.source_sha256:
        raise SceneOCRError(f"整秒OCR源SHA绑定失败：{source.source_name}")
    frames = result.get("frames")
    if (not result.get("coverage_verified") or not isinstance(frames, list)
            or result.get("expected_frame_count") != len(frames)):
        raise SceneOCRError(f"整秒OCR覆盖未通过：{source.source_name}")
    signature = _string(result.get("cache_signature"), "ocr.cache_signature")
    cache_dir = source_work / "ocr_cache" / signature
    hashes: set[str] = set()
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("index"), int):
            raise SceneOCRError(f"整秒OCR帧索引非法：{source.source_name}")
        cache_path = cache_dir / f"frame_{frame['index']:06d}.json"
        cached = read_json(cache_path)
        if cached.get("signature") != signature:
            raise SceneOCRError(f"整秒OCR帧缓存签名错误：{cache_path}")
        image_sha = _sha(cached.get("image_sha256"), f"{cache_path}.image_sha256")
        hashes.add(image_sha)
    return ExistingSecondFrames(frozenset(hashes), len(frames))


def validate_existing_inventory(sources: Iterable[VideoSource], existing_work: Path) -> dict[str, int]:
    """Fast dry-run check of source bindings and declared one-second coverage."""
    source_count = 0
    frame_count = 0
    for source in sources:
        result_path = Path(existing_work) / source.source_sha256[:16] / "ocr.json"
        result = read_json(result_path)
        fingerprint = result.get("source_fingerprint")
        frames = result.get("frames")
        if (not isinstance(fingerprint, dict)
                or fingerprint.get("sha256") != source.source_sha256
                or not result.get("coverage_verified")
                or not isinstance(frames, list)
                or result.get("expected_frame_count") != len(frames)):
            raise SceneOCRError(f"整秒OCR清单不完整或源SHA不符：{source.source_name}")
        source_count += 1
        frame_count += len(frames)
    return {"source_count": source_count, "existing_second_frame_count": frame_count}


def source_durations(
    sources: Iterable[VideoSource], existing_work: Path
) -> dict[str, float]:
    """Read SHA-bound durations from the already verified one-second OCR."""
    durations: dict[str, float] = {}
    for source in sources:
        result_path = Path(existing_work) / source.source_sha256[:16] / "ocr.json"
        result = read_json(result_path)
        fingerprint = result.get("source_fingerprint")
        duration = result.get("duration_seconds")
        if (not isinstance(fingerprint, dict)
                or fingerprint.get("sha256") != source.source_sha256
                or not isinstance(duration, (int, float))
                or isinstance(duration, bool) or not math.isfinite(float(duration))
                or duration <= 0):
            raise SceneOCRError(f"无法取得SHA绑定的视频时长：{source.source_name}")
        durations[source.source_sha256] = float(duration)
    return durations


def greedy_duration_shards(
    sources: list[VideoSource], durations: dict[str, float], shard_count: int
) -> tuple[list[list[VideoSource]], list[float]]:
    """Deterministic longest-processing-time-first greedy assignment."""
    if (not isinstance(shard_count, int) or isinstance(shard_count, bool)
            or not 1 <= shard_count <= len(sources)):
        raise SceneOCRError("shard_count必须在1与视频源数量之间")
    if set(durations) != {source.source_sha256 for source in sources}:
        raise SceneOCRError("分片时长表与视频SHA全集不一致")
    for sha, duration in durations.items():
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise SceneOCRError(f"分片时长非法：{sha}")
    indexed = list(enumerate(sources))
    indexed.sort(key=lambda item: (-durations[item[1].source_sha256], item[0]))
    assignments: list[list[tuple[int, VideoSource]]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for original_index, source in indexed:
        target = min(range(shard_count), key=lambda value: (
            totals[value], len(assignments[value]), value))
        assignments[target].append((original_index, source))
        totals[target] += durations[source.source_sha256]
    shards = [
        [source for _, source in sorted(items, key=lambda item: item[0])]
        for items in assignments
    ]
    flattened = [source.source_sha256 for shard in shards for source in shard]
    expected = [source.source_sha256 for source in sources]
    if len(flattened) != len(set(flattened)) or set(flattened) != set(expected):
        raise AssertionError("greedy shard assignment lost or duplicated a source")
    return shards, totals


def _cached_candidate(
    cache_path: Path,
    image_path: Path,
    *,
    signature: str,
    frame_image_sha: str,
) -> dict[str, Any] | None:
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        frame = cached["frame"]
        if (cached["signature"] != signature
                or cached["frame_image_sha256"] != frame_image_sha
                or frame["frame_image_sha256"] != frame_image_sha
                or frame["image_path"] != str(image_path)
                or not isinstance(frame["lines"], list)
                or hashlib.sha256(image_path.read_bytes()).hexdigest() != frame_image_sha):
            return None
        for line in frame["lines"]:
            if (not isinstance(line.get("text"), str)
                    or not isinstance(line.get("confidence"), (int, float))
                    or not math.isfinite(float(line["confidence"]))
                    or not 0 <= float(line["confidence"]) <= 1
                    or not isinstance(line.get("box"), list)
                    or len(line["box"]) != 4):
                return None
        return frame
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def _validated_complete_cache(path: Path, signature: str) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("status") != "complete" or result.get("config_signature") != signature:
            return None
        frames = result.get("frames")
        if not isinstance(frames, list):
            return None
        for frame in frames:
            image_path = Path(frame["image_path"])
            if (not image_path.is_file()
                    or hashlib.sha256(image_path.read_bytes()).hexdigest()
                    != frame["frame_image_sha256"]):
                return None
        result["complete_cache_hit"] = True
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _validated_detection_cache(path: Path, signature: str) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if (result.get("status") != "detection_complete"
                or result.get("config_signature") != signature
                or not isinstance(result.get("frames"), list)):
            return None
        for frame in result["frames"]:
            image_path = Path(frame["image_path"])
            if (not image_path.is_file()
                    or hashlib.sha256(image_path.read_bytes()).hexdigest()
                    != frame["frame_image_sha256"]):
                return None
        result["detection_cache_hit"] = True
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def assess_candidate_risk(
    candidate_count: int,
    duration_seconds: float,
    existing_second_frame_count: int,
    maximum_rate: float,
) -> dict[str, Any]:
    """Gate OCR until the complete detection pass proves candidate volume sane."""
    if duration_seconds <= 0 or existing_second_frame_count < 1 or maximum_rate <= 0:
        raise SceneOCRError("候选风险门槛参数非法")
    rate = candidate_count / duration_seconds
    reasons: list[str] = []
    if rate > maximum_rate:
        reasons.append(f"候选率{rate:.6f}帧/秒超过{maximum_rate:.6f}")
    if candidate_count > existing_second_frame_count:
        reasons.append(
            f"候选数{candidate_count}超过已有整秒帧数{existing_second_frame_count}")
    return {
        "rejected": bool(reasons),
        "candidate_rate_per_second": rate,
        "maximum_candidate_rate_per_second": maximum_rate,
        "existing_second_frame_count": existing_second_frame_count,
        "reasons": reasons,
    }


def process_source(
    source: VideoSource,
    work_dir: Path,
    existing_work: Path,
    config: dict[str, Any],
    *,
    engine_factory: Callable[[dict[str, Any]], Any] = _new_engine,
    existing_hashes: set[str] | None = None,
    existing_frame_count: int | None = None,
    detect_only: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Detect completely, apply the risk gate, then (and only then) run OCR."""
    fingerprint = _fingerprint(source.source_path)
    if fingerprint["sha256"] != source.source_sha256:
        raise SceneOCRError(f"视频源SHA变化：{source.source_name}")
    try:
        info = _read_video_info(source.source_path)
    except Exception as exc:
        # This deliberately changes only the exception type, never suppresses it.
        raise SceneOCRError(f"视频元数据读取失败：{source.source_name}：{exc}") from exc

    full_resolution = requires_original_resolution(source)
    source_config = dict(config)
    source_config["original_resolution_ocr"] = full_resolution
    source_config["source_width"] = info.width
    source_config["source_height"] = info.height
    source_config["rapidocr_params"] = dict(OCR_PARAMS)
    source_config["rapidocr_params"]["Global.text_score"] = 0.0
    source_config["rapidocr_params"]["Global.max_side_len"] = max(
        int(source_config["rapidocr_params"]["Global.max_side_len"]), info.width, info.height)

    if existing_hashes is None:
        existing = existing_second_frame_hashes(source, existing_work)
        existing_hashes = set(existing.hashes)
        existing_frame_count = existing.frame_count
    elif existing_frame_count is None:
        existing_frame_count = math.ceil(float(info.duration))
    existing_hash_digest = hashlib.sha256(
        "\n".join(sorted(existing_hashes)).encode()).hexdigest()
    source_config["existing_second_image_hash_set_sha256"] = existing_hash_digest
    source_config["existing_second_frame_count"] = existing_frame_count
    signature = _config_signature(source.source_sha256, source_config)
    cache_dir = _source_cache_directory(work_dir, source.source_sha256, signature)
    complete_path = cache_dir / "result.json"
    complete = None if detect_only else _validated_complete_cache(complete_path, signature)
    if complete is not None:
        return complete

    detection_path = cache_dir / "detection.json"
    detection = _validated_detection_cache(detection_path, signature)
    if detection is None:
        detected_frames: list[dict[str, Any]] = []
        raw_candidate_count = 0
        repeated_candidate_count = 0
        excluded_existing_count = 0
        seen_candidates: set[tuple[int, int, int]] = set()
        try:
            candidates = iter_scene_candidates(
                _decoded_frames(source.source_path, info), source_config, progress=progress)
            for candidate in candidates:
                candidate_key = (
                    candidate.pts, candidate.time_base.numerator,
                    candidate.time_base.denominator)
                if candidate_key in seen_candidates:
                    repeated_candidate_count += 1
                    continue
                seen_candidates.add(candidate_key)
                raw_candidate_count += 1
                comparison = _resize_maximum(
                    candidate.image, int(source_config["comparison_maximum_dimension"]))
                comparison_payload = _jpeg(comparison, int(source_config["jpeg_quality"]))
                comparison_sha = hashlib.sha256(comparison_payload).hexdigest()
                if comparison_sha in existing_hashes:
                    excluded_existing_count += 1
                    continue
                ocr_image = candidate.image if full_resolution else _resize_maximum(
                    candidate.image, int(source_config["ocr_maximum_dimension"]))
                frame_payload = _jpeg(ocr_image, int(source_config["jpeg_quality"]))
                frame_sha = hashlib.sha256(frame_payload).hexdigest()
                stem = (f"pts_{candidate.pts}_{candidate.time_base.numerator}_"
                        f"{candidate.time_base.denominator}_{frame_sha[:16]}")
                image_path = (cache_dir / "frames" / f"{stem}.jpg").resolve()
                if (not image_path.is_file()
                        or hashlib.sha256(image_path.read_bytes()).hexdigest() != frame_sha):
                    _atomic_bytes(image_path, frame_payload)
                detected_frames.append({
                    "source_sha256": source.source_sha256,
                    "config_signature": signature,
                    "pts": candidate.pts,
                    "time_base": {
                        "numerator": candidate.time_base.numerator,
                        "denominator": candidate.time_base.denominator,
                    },
                    "timestamp_seconds": float(candidate.timestamp),
                    "selection_reasons": list(candidate.reasons),
                    "scene_change_score": candidate.scene_change_score,
                    "text_edge_change_score": candidate.text_edge_change_score,
                    "comparison_image_sha256": comparison_sha,
                    "frame_image_sha256": frame_sha,
                    "image_path": str(image_path),
                    "width": int(ocr_image.shape[1]),
                    "height": int(ocr_image.shape[0]),
                    "original_resolution_ocr": full_resolution,
                })
        except OCRProcessingError as exc:
            raise SceneOCRError(f"视频解码失败：{source.source_name}：{exc}") from exc
        risk = assess_candidate_risk(
            len(detected_frames), float(info.duration), int(existing_frame_count),
            float(source_config["maximum_candidate_rate_per_second"]))
        detection = {
            "status": "detection_complete",
            "source_sha256": source.source_sha256,
            "config_signature": signature,
            "duration_seconds": float(info.duration),
            "raw_candidate_frame_count": raw_candidate_count,
            "repeated_candidate_count": repeated_candidate_count,
            "excluded_existing_second_frame_count": excluded_existing_count,
            "new_candidate_frame_count": len(detected_frames),
            "risk_gate": risk,
            "detection_cache_hit": False,
            "frames": detected_frames,
            "decode_coverage_verified": True,
        }
        _atomic_json(detection_path, detection)

    if detect_only:
        return {
            "status": "detection_complete",
            "asset_id": source.asset_id,
            "source_name": source.source_name,
            "source_sha256": source.source_sha256,
            "duration_seconds": float(info.duration),
            "config_signature": signature,
            "raw_candidate_frame_count": detection["raw_candidate_frame_count"],
            "excluded_existing_second_frame_count": detection[
                "excluded_existing_second_frame_count"],
            "new_candidate_frame_count": detection["new_candidate_frame_count"],
            "new_ocr_frame_count": 0,
            "risk_gate": detection["risk_gate"],
            "detection_cache_hit": detection.get("detection_cache_hit", False),
            "decode_coverage_verified": True,
            "frames": [],
        }

    if detection["risk_gate"]["rejected"]:
        raise SceneOCRError(
            f"候选率风险门槛拒绝OCR：{source.source_name}："
            + "；".join(detection["risk_gate"]["reasons"]))

    engine = None
    frames: list[dict[str, Any]] = []
    per_frame_cache_hits = 0
    for detected in detection["frames"]:
        image_path = Path(detected["image_path"])
        frame_cache_path = image_path.with_suffix(".json")
        record = _cached_candidate(
            frame_cache_path, image_path, signature=signature,
            frame_image_sha=detected["frame_image_sha256"])
        if record is None:
            ocr_image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            if ocr_image is None:
                raise SceneOCRError(f"无法读取候选帧证据：{image_path}")
            if engine is None:
                engine = engine_factory(source_config["rapidocr_params"])
            record = dict(detected)
            record["lines"] = recognize_raw_lines(engine, ocr_image)
            _atomic_json(frame_cache_path, {
                "signature": signature,
                "frame_image_sha256": detected["frame_image_sha256"],
                "frame": record,
            })
        else:
            per_frame_cache_hits += 1
        frames.append(record)
        if progress is not None and (len(frames) == 1 or len(frames) % 25 == 0):
            progress({"stage": "ocr", "new_frames_completed": len(frames),
                      "candidate_frames_seen": detection["new_candidate_frame_count"],
                      "timestamp_seconds": detected["timestamp_seconds"]})

    result = {
        "status": "complete",
        "asset_id": source.asset_id,
        "source_type": source.source_type,
        "source_path": str(source.source_path),
        "source_name": source.source_name,
        "source_sha256": source.source_sha256,
        "source_size_bytes": fingerprint["size_bytes"],
        "year": source.year,
        "parent_source": source.parent_source,
        "parent_slide": source.parent_slide,
        "archive_member": source.archive_member,
        "duration_seconds": float(info.duration),
        "config_signature": signature,
        "config": source_config,
        "raw_candidate_frame_count": detection["raw_candidate_frame_count"],
        "repeated_candidate_count": detection["repeated_candidate_count"],
        "excluded_existing_second_frame_count": detection[
            "excluded_existing_second_frame_count"],
        "new_candidate_frame_count": detection["new_candidate_frame_count"],
        "risk_gate": detection["risk_gate"],
        "new_ocr_frame_count": len(frames),
        "per_frame_cache_hits": per_frame_cache_hits,
        "complete_cache_hit": False,
        "frames": frames,
        "decode_coverage_verified": True,
    }
    _atomic_json(complete_path, result)
    return result


def _output_document(
    sources: list[VideoSource],
    records: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    limited: bool,
    detect_only: bool = False,
    shard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    complete = not failures and not limited and len(records) == len(sources)
    if shard is not None:
        status = "shard_complete" if complete else "shard_partial"
    elif detect_only and not failures:
        status = "detection_complete"
    else:
        status = "complete" if complete else "partial"
    return {
        "schema_version": SCHEMA_VERSION,
        "processor_version": PROCESSOR_VERSION,
        "status": status,
        "mode": "detect_only" if detect_only else ("shard" if shard else "ocr"),
        "source_count": len(sources),
        "completed_source_count": len(records),
        "failed_source_count": len(failures),
        "config": config,
        "sources": records,
        "failures": failures,
        "coverage_verified": complete,
        "shard": shard,
    }


def run_pipeline(
    sources: list[VideoSource],
    *,
    work_dir: Path,
    existing_work: Path,
    output: Path,
    config: dict[str, Any],
    limit: int | None = None,
    detect_only: bool = False,
    shard_metadata: dict[str, Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    selected = sources if limit is None else sources[:limit]
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, source in enumerate(selected, 1):
        if progress is not None:
            progress({"stage": "file_start", "file_index": index,
                      "file_count": len(selected), "source_name": source.source_name})
        try:
            record = process_source(
                source, work_dir, existing_work, config,
                detect_only=detect_only, progress=progress)
            records.append(record)
            if progress is not None:
                progress({"stage": "file_complete", "file_index": index,
                          "file_count": len(selected), "source_name": source.source_name,
                          "new_candidate_frames": record["new_candidate_frame_count"],
                          "new_ocr_frames": record["new_ocr_frame_count"]})
        except Exception as exc:
            failure = {"asset_id": source.asset_id, "source_name": source.source_name,
                       "source_sha256": source.source_sha256,
                       "error_type": type(exc).__name__, "error": str(exc)}
            failures.append(failure)
            document = _output_document(
                sources, records, failures, config,
                limited=len(selected) != len(sources), detect_only=detect_only,
                shard=shard_metadata)
            if not detect_only:
                _atomic_json(output, document)
            raise SceneOCRError(
                f"第{index}/{len(selected)}个视频失败"
                f"{'，已写入partial输出' if not detect_only else ''}："
                f"{source.source_name}：{exc}"
            ) from exc
        document = _output_document(
            sources, records, failures, config,
            limited=len(selected) != len(sources), detect_only=detect_only,
            shard=shard_metadata)
        if not detect_only:
            _atomic_json(output, document)
    return document


def merge_shards(
    shard_paths: list[Path],
    output: Path,
    expected_sources: list[VideoSource],
) -> dict[str, Any]:
    """Strictly merge a complete, non-overlapping shard set into one result."""
    if not shard_paths:
        raise SceneOCRError("--merge-shards至少需要一个分片JSON")
    expected_order = [source.source_sha256 for source in expected_sources]
    expected_set = set(expected_order)
    if len(expected_order) != SOURCE_COUNT or len(expected_set) != SOURCE_COUNT:
        raise SceneOCRError(f"权威视频SHA全集必须恰好包含{SOURCE_COUNT}项")

    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in shard_paths:
        resolved = Path(path).resolve(strict=True)
        if resolved == Path(output).resolve():
            raise SceneOCRError("合并输出不得同时作为分片输入")
        documents.append((resolved, read_json(resolved)))

    first_config = documents[0][1].get("config")
    if not isinstance(first_config, dict):
        raise SceneOCRError("分片缺少config对象")
    canonical_config = json.dumps(first_config, sort_keys=True, allow_nan=False)
    shard_count: int | None = None
    seen_indexes: set[int] = set()
    records_by_sha: dict[str, dict[str, Any]] = {}
    provenance: list[dict[str, Any]] = []
    for path, document in documents:
        if (document.get("schema_version") != SCHEMA_VERSION
                or document.get("processor_version") != PROCESSOR_VERSION
                or document.get("status") != "shard_complete"
                or document.get("mode") != "shard"
                or document.get("coverage_verified") is not True
                or document.get("failed_source_count") != 0
                or document.get("failures") != []):
            raise SceneOCRError(f"分片不是当前v2完整成功状态：{path}")
        try:
            current_config = json.dumps(
                document.get("config"), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SceneOCRError(f"分片config不可比较：{path}") from exc
        if current_config != canonical_config:
            raise SceneOCRError("分片config不一致，拒绝合并")

        shard = document.get("shard")
        if not isinstance(shard, dict):
            raise SceneOCRError(f"分片元数据缺失：{path}")
        index = shard.get("shard_index")
        count = shard.get("shard_count")
        assigned = shard.get("assigned_source_sha256")
        if (not isinstance(index, int) or isinstance(index, bool)
                or not isinstance(count, int) or isinstance(count, bool)
                or count < 1 or not 0 <= index < count
                or shard.get("global_source_count") != SOURCE_COUNT
                or not isinstance(assigned, list)
                or not all(isinstance(value, str) and SHA256_RE.fullmatch(value)
                           for value in assigned)
                or shard.get("assigned_source_count") != len(assigned)):
            raise SceneOCRError(f"分片范围或分配元数据非法：{path}")
        if shard_count is None:
            shard_count = count
        elif shard_count != count:
            raise SceneOCRError("分片声明的shard_count不一致")
        if index in seen_indexes:
            raise SceneOCRError(f"重复的shard_index：{index}")
        seen_indexes.add(index)

        records = document.get("sources")
        if (not isinstance(records, list) or document.get("source_count") != len(records)
                or document.get("completed_source_count") != len(records)
                or [item.get("source_sha256") for item in records
                    if isinstance(item, dict)] != assigned):
            raise SceneOCRError(f"分片sources与assigned_source_sha256不一致：{path}")
        for record in records:
            if not isinstance(record, dict):
                raise SceneOCRError(f"分片源记录非法：{path}")
            sha = _sha(record.get("source_sha256"), "sources.source_sha256")
            if sha in records_by_sha:
                raise SceneOCRError(f"分片间出现重复视频SHA：{sha}")
            risk = record.get("risk_gate")
            frames = record.get("frames")
            if (record.get("status") != "complete"
                    or record.get("decode_coverage_verified") is not True
                    or not isinstance(risk, dict) or risk.get("rejected") is not False
                    or risk.get("reasons") != []
                    or not isinstance(frames, list)
                    or record.get("new_ocr_frame_count") != len(frames)
                    or record.get("new_candidate_frame_count") != len(frames)):
                raise SceneOCRError(f"视频源未完整OCR或风险门槛未通过：{sha}")
            for frame in frames:
                if (not isinstance(frame, dict)
                        or frame.get("source_sha256") != sha):
                    raise SceneOCRError(f"候选帧源SHA绑定失败：{sha}")
                frame_sha = _sha(
                    frame.get("frame_image_sha256"), f"{sha}.frame_image_sha256")
                image_value = frame.get("image_path")
                if not isinstance(image_value, str):
                    raise SceneOCRError(f"候选帧图片路径缺失：{sha}")
                image_path = Path(image_value)
                if (not image_path.is_file()
                        or hashlib.sha256(image_path.read_bytes()).hexdigest() != frame_sha):
                    raise SceneOCRError(f"候选帧图片不存在或SHA不符：{image_path}")
            records_by_sha[sha] = record
        provenance.append({
            "shard_index": index,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_count": len(records),
        })

    if shard_count is None or len(documents) != shard_count:
        raise SceneOCRError("分片文件数量与shard_count不一致，存在缺失")
    if seen_indexes != set(range(shard_count)):
        raise SceneOCRError("shard_index集合不完整")
    actual_set = set(records_by_sha)
    missing = expected_set - actual_set
    unexpected = actual_set - expected_set
    if missing or unexpected or len(records_by_sha) != SOURCE_COUNT:
        raise SceneOCRError(
            f"67个视频SHA全集不一致：缺失{len(missing)}，额外{len(unexpected)}")

    merged = {
        "schema_version": SCHEMA_VERSION,
        "processor_version": PROCESSOR_VERSION,
        "status": "complete",
        "mode": "ocr",
        "source_count": SOURCE_COUNT,
        "completed_source_count": SOURCE_COUNT,
        "failed_source_count": 0,
        "config": first_config,
        "sources": [records_by_sha[sha] for sha in expected_order],
        "failures": [],
        "coverage_verified": True,
        "shard": None,
        "merged_shards": sorted(provenance, key=lambda item: item["shard_index"]),
    }
    _atomic_json(Path(output).resolve(), merged)
    return merged


def build_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config["rapidocr_engine"] = "RapidOCR"
    config["rapidocr_version"] = importlib.metadata.version("rapidocr")
    config["rapidocr_models"] = {
        "detection": "PP-OCRv6/ch/small/onnxruntime",
        "recognition": "PP-OCRv6/ch/small/onnxruntime",
        "classification": "PP-OCRv4/ch/mobile/onnxruntime",
    }
    return config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="逐帧低分辨率检测场景/文字变化，仅对整秒OCR未覆盖的新帧运行RapidOCR")
    parser.add_argument("--asset-manifest", type=Path,
                        default=ROOT / ".work/first-principles-2026/asset_manifest.json")
    parser.add_argument("--embedded-manifest", type=Path,
                        default=ROOT / ".work/embedded-office/manifest.json")
    parser.add_argument("--existing-work", type=Path,
                        default=ROOT / ".work/single-video")
    parser.add_argument("--work-dir", type=Path,
                        default=ROOT / ".work/first-principles-2026")
    parser.add_argument("--output", type=Path,
                        help="普通/合并默认video_scene_ocr.json；分片运行必须显式指定")
    parser.add_argument("--dry-run", action="store_true",
                        help="只核对67个源与已有整秒OCR，不解码、不OCR、不写输出")
    parser.add_argument("--detect-only", action="store_true",
                        help="完整解码并只统计候选；必须与--limit N合用，不调用OCR、不写目标JSON")
    parser.add_argument("--limit", type=int,
                        help="仅作小样运行；输出明确标记partial")
    parser.add_argument("--shard-index", type=int,
                        help="当前分片序号，范围0..shard-count-1")
    parser.add_argument("--shard-count", type=int,
                        help="总分片数；与--shard-index同时使用")
    parser.add_argument("--merge-shards", type=Path, nargs="+",
                        help="严格合并一组完整分片JSON，不解码、不OCR")
    return parser.parse_args(argv)


def _print_progress(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        default_output = ROOT / ".work/first-principles-2026/video_scene_ocr.json"
        output = (args.output or default_output).resolve()
        if args.limit is not None and args.limit < 1:
            raise SceneOCRError("--limit必须是正整数")
        if args.detect_only and args.limit is None:
            raise SceneOCRError("--detect-only必须显式指定--limit N")
        if args.detect_only and args.dry_run:
            raise SceneOCRError("--detect-only与--dry-run不能同时使用")
        has_shard_index = args.shard_index is not None
        has_shard_count = args.shard_count is not None
        if has_shard_index != has_shard_count:
            raise SceneOCRError("--shard-index与--shard-count必须同时指定")
        if args.merge_shards and any((args.dry_run, args.detect_only,
                                      args.limit is not None, has_shard_index)):
            raise SceneOCRError("--merge-shards不得与运行/分片参数混用")
        if has_shard_index and (args.limit is not None or args.detect_only or args.dry_run):
            raise SceneOCRError("正式分片不得与--limit、--detect-only或--dry-run混用")
        if has_shard_index and args.output is None:
            raise SceneOCRError("分片运行必须用--output显式指定独立shard-X.json")

        sources = discover_sources(
            args.asset_manifest.resolve(), args.embedded_manifest.resolve())
        if args.merge_shards:
            merged = merge_shards(args.merge_shards, output, sources)
            print(json.dumps({
                "status": merged["status"],
                "source_count": merged["source_count"],
                "merged_shard_count": len(merged["merged_shards"]),
                "coverage_verified": merged["coverage_verified"],
                "output": str(output),
            }, ensure_ascii=False, indent=2))
            return 0

        inventory = validate_existing_inventory(sources, args.existing_work.resolve())
        if inventory["source_count"] != SOURCE_COUNT:
            raise SceneOCRError(f"整秒OCR视频源应为{SOURCE_COUNT}个")
        if inventory["existing_second_frame_count"] != EXISTING_SECOND_FRAME_COUNT:
            raise SceneOCRError(
                "整秒OCR帧总数不是71862，拒绝在未知底稿上补帧")
        if args.dry_run:
            print(json.dumps({"dry_run": True, **inventory,
                              "full_resolution_sources": [
                                  item.source_name for item in sources
                                  if requires_original_resolution(item)
                              ], "would_write": str(output)},
                             ensure_ascii=False, indent=2))
            return 0

        shard_metadata = None
        work_dir = args.work_dir.resolve()
        if has_shard_index:
            if (args.shard_count < 1 or args.shard_count > SOURCE_COUNT
                    or not 0 <= args.shard_index < args.shard_count):
                raise SceneOCRError("分片序号必须满足0 <= shard-index < shard-count <= 67")
            durations = source_durations(sources, args.existing_work.resolve())
            shards, totals = greedy_duration_shards(sources, durations, args.shard_count)
            sources = shards[args.shard_index]
            shard_metadata = {
                "shard_index": args.shard_index,
                "shard_count": args.shard_count,
                "global_source_count": SOURCE_COUNT,
                "assigned_source_count": len(sources),
                "assigned_source_sha256": [item.source_sha256 for item in sources],
                "assigned_duration_seconds": totals[args.shard_index],
                "assignment_strategy": "longest_processing_time_first_greedy_v1",
            }
            work_dir = (work_dir / "scene-ocr-shards"
                        / f"shard-{args.shard_index}-of-{args.shard_count}")
        result = run_pipeline(
            sources, work_dir=work_dir,
            existing_work=args.existing_work.resolve(), output=output,
            config=build_config(), limit=args.limit, detect_only=args.detect_only,
            shard_metadata=shard_metadata, progress=_print_progress)
        if args.detect_only:
            print(json.dumps({
                "detect_only": True,
                "processed_source_count": result["completed_source_count"],
                "sources": [{
                    "source_name": item["source_name"],
                    "duration_seconds": item["duration_seconds"],
                    "raw_candidate_frame_count": item["raw_candidate_frame_count"],
                    "excluded_existing_second_frame_count": item[
                        "excluded_existing_second_frame_count"],
                    "new_candidate_frame_count": item["new_candidate_frame_count"],
                    "candidate_rate_per_second": item["risk_gate"][
                        "candidate_rate_per_second"],
                    "risk_rejected": item["risk_gate"]["rejected"],
                    "detection_cache_hit": item["detection_cache_hit"],
                } for item in result["sources"]],
                "ocr_calls": 0,
                "output_written": False,
            }, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({key: result[key] for key in (
            "status", "source_count", "completed_source_count",
            "failed_source_count", "coverage_verified")}, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"complete", "shard_complete"} else 2
    except (OSError, SceneOCRError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
