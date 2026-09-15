#!/usr/bin/env python3
"""Strict, read-only QA for supplemental video scene OCR evidence.

The final mode audits the merged 67-source document.  ``--completed-cache-audit``
audits the immutable per-source ``result.json`` files that already exist while
shards are still running; it deliberately does not interpret an incomplete
shard document as complete coverage.

No OCR or scene detection is performed here.  The audit binds every source to
the authoritative manifests, every result to the exact v2 configuration, and
every OCR frame to its detection record, JPEG bytes, and per-frame cache.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_pipeline.ocr import OCR_PARAMS, _fingerprint, _read_video_info
from scripts import ocr_video_scene_changes as scene


class SceneQAError(RuntimeError):
    """A scene-OCR artifact violates an evidence or coverage invariant."""


_SHARD_RE = re.compile(r"shard-(\d+)-of-(\d+)\Z")


def _fail(message: str) -> None:
    raise SceneQAError(message)


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail(f"{field}必须是大于等于{minimum}的整数")
    return value


def _number(value: Any, field: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(float(value))):
        _fail(f"{field}必须是有限数值")
    result = float(value)
    if minimum is not None and result < minimum:
        _fail(f"{field}小于{minimum}")
    if maximum is not None and result > maximum:
        _fail(f"{field}大于{maximum}")
    return result


def _sha(value: Any, field: str) -> str:
    try:
        return scene._sha(value, field)
    except scene.SceneOCRError as exc:
        raise SceneQAError(str(exc)) from exc


def _read(path: Path) -> dict[str, Any]:
    try:
        return scene.read_json(path)
    except scene.SceneOCRError as exc:
        raise SceneQAError(str(exc)) from exc


def _source_expected_metadata(
    asset_manifest: Path, embedded_manifest: Path, sources: list[scene.VideoSource]
) -> dict[str, dict[str, Any]]:
    asset_doc = _read(asset_manifest)
    embedded_doc = _read(embedded_manifest)
    by_sha: dict[str, dict[str, Any]] = {}
    for item in asset_doc.get("assets", []):
        if isinstance(item, dict) and item.get("kind") == "video":
            sha = _sha(item.get("source_sha256"), "asset.source_sha256")
            by_sha[sha] = {
                "size_bytes": _integer(item.get("size_bytes"), "asset.size_bytes"),
            }
    for item in embedded_doc.get("files", []):
        if isinstance(item, dict):
            sha = _sha(item.get("sha256"), "embedded.sha256")
            by_sha[sha] = {
                "size_bytes": _integer(item.get("size_bytes"), "embedded.size_bytes"),
            }
    expected = {source.source_sha256 for source in sources}
    if set(by_sha) != expected:
        _fail("权威manifest中的视频SHA/大小元数据与67个来源全集不一致")
    return by_sha


def _expected_source_config(
    source: scene.VideoSource,
    record_config: dict[str, Any],
    existing: scene.ExistingSecondFrames,
) -> dict[str, Any]:
    base = scene.build_config()
    expected_keys = set(base) | {
        "original_resolution_ocr", "source_width", "source_height",
        "rapidocr_params", "existing_second_image_hash_set_sha256",
        "existing_second_frame_count",
    }
    if set(record_config) != expected_keys:
        missing = sorted(expected_keys - set(record_config))
        extra = sorted(set(record_config) - expected_keys)
        _fail(f"{source.source_name} config字段不精确：缺失{missing}，额外{extra}")
    for key, value in base.items():
        if record_config.get(key) != value:
            _fail(f"{source.source_name} config.{key}偏离当前v2定义")
    width = _integer(record_config.get("source_width"), "config.source_width", minimum=1)
    height = _integer(record_config.get("source_height"), "config.source_height", minimum=1)
    if record_config.get("original_resolution_ocr") is not scene.requires_original_resolution(source):
        _fail(f"{source.source_name}原始分辨率OCR策略不符")
    expected_params = dict(OCR_PARAMS)
    expected_params["Global.text_score"] = 0.0
    expected_params["Global.max_side_len"] = max(
        int(expected_params["Global.max_side_len"]), width, height)
    if record_config.get("rapidocr_params") != expected_params:
        _fail(f"{source.source_name} RapidOCR参数不符")
    digest = hashlib.sha256("\n".join(sorted(existing.hashes)).encode()).hexdigest()
    if record_config.get("existing_second_image_hash_set_sha256") != digest:
        _fail(f"{source.source_name}整秒帧SHA集合摘要不符")
    if record_config.get("existing_second_frame_count") != existing.frame_count:
        _fail(f"{source.source_name}整秒帧计数不符")
    return record_config


def _validate_line(line: Any, label: str) -> None:
    if not isinstance(line, dict) or set(line) != {"text", "confidence", "box"}:
        _fail(f"{label} OCR行字段不精确")
    if not isinstance(line["text"], str):
        _fail(f"{label}.text不是字符串")
    _number(line["confidence"], f"{label}.confidence", minimum=0.0, maximum=1.0)
    box = line["box"]
    if not isinstance(box, list) or len(box) != 4:
        _fail(f"{label}.box必须有4个点")
    for point_index, point in enumerate(box):
        if not isinstance(point, list) or len(point) != 2:
            _fail(f"{label}.box[{point_index}]必须是二维点")
        _number(point[0], f"{label}.box[{point_index}][0]")
        _number(point[1], f"{label}.box[{point_index}][1]")


def _validate_frame(
    frame: Any,
    detected: Any,
    *,
    source: scene.VideoSource,
    signature: str,
    config: dict[str, Any],
    existing_hashes: frozenset[str],
    duration: float,
    previous_time: Fraction | None,
    expected_frames_directory: Path,
) -> tuple[Fraction, int]:
    label = f"{source.source_name}.frame"
    if not isinstance(frame, dict):
        _fail(f"{label}不是对象")
    required = {
        "source_sha256", "config_signature", "pts", "time_base",
        "timestamp_seconds", "selection_reasons", "scene_change_score",
        "text_edge_change_score", "comparison_image_sha256",
        "frame_image_sha256", "image_path", "width", "height",
        "original_resolution_ocr", "lines",
    }
    if set(frame) != required:
        _fail(f"{label}字段不精确")
    if frame["source_sha256"] != source.source_sha256:
        _fail(f"{label}源SHA错绑")
    if frame["config_signature"] != signature:
        _fail(f"{label}配置签名错绑")
    pts = _integer(frame["pts"], f"{label}.pts")
    time_base = frame["time_base"]
    if not isinstance(time_base, dict) or set(time_base) != {"numerator", "denominator"}:
        _fail(f"{label}.time_base字段非法")
    numerator = _integer(time_base["numerator"], f"{label}.time_base.numerator", minimum=1)
    denominator = _integer(time_base["denominator"], f"{label}.time_base.denominator", minimum=1)
    exact_time = Fraction(pts * numerator, denominator)
    timestamp = _number(frame["timestamp_seconds"], f"{label}.timestamp_seconds", minimum=0.0)
    if float(exact_time) != timestamp:
        _fail(f"{label} PTS/time_base与timestamp不一致")
    if timestamp >= duration:
        _fail(f"{label}时间戳超出视频时长")
    if previous_time is not None and exact_time <= previous_time:
        _fail(f"{label}时间戳未严格递增")

    reasons = frame["selection_reasons"]
    allowed = {
        ("video_start",), ("video_end",),
        ("stable_after_strong_scene_change",),
    }
    if not isinstance(reasons, list) or tuple(reasons) not in allowed:
        _fail(f"{label}候选原因非法")
    scene_score = _number(
        frame["scene_change_score"], f"{label}.scene_change_score",
        minimum=0.0, maximum=1.0)
    _number(frame["text_edge_change_score"], f"{label}.text_edge_change_score",
            minimum=0.0, maximum=1.0)
    if reasons == ["stable_after_strong_scene_change"]:
        if scene_score < float(config["scene_change_threshold"]):
            _fail(f"{label}稳定场景候选未达到强切换阈值")
    elif scene_score != 0.0:
        _fail(f"{label}视频边界帧的场景分数必须为0")

    comparison_sha = _sha(frame["comparison_image_sha256"], f"{label}.comparison_sha")
    frame_sha = _sha(frame["frame_image_sha256"], f"{label}.frame_sha")
    if comparison_sha in existing_hashes:
        _fail(f"{label}本应被整秒帧SHA排除")
    if not config["original_resolution_ocr"] and comparison_sha != frame_sha:
        _fail(f"{label}非原图策略下比较图与OCR图SHA不一致")
    image_value = frame["image_path"]
    if not isinstance(image_value, str) or not image_value:
        _fail(f"{label}.image_path非法")
    image_path = Path(image_value)
    if not image_path.is_absolute() or not image_path.is_file():
        _fail(f"{label}候选JPEG不存在或不是绝对路径")
    if image_path.parent.resolve() != expected_frames_directory.resolve():
        _fail(f"{label}候选JPEG逸出源SHA/配置签名绑定目录")
    expected_name = (
        f"pts_{pts}_{numerator}_{denominator}_{frame_sha[:16]}.jpg")
    if image_path.name != expected_name:
        _fail(f"{label}候选JPEG文件名与PTS/SHA不符")
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != frame_sha:
        _fail(f"{label}候选JPEG字节SHA不符")
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        _fail(f"{label}候选JPEG无法解码")
    width = _integer(frame["width"], f"{label}.width", minimum=1)
    height = _integer(frame["height"], f"{label}.height", minimum=1)
    if (image.shape[1], image.shape[0]) != (width, height):
        _fail(f"{label}JPEG尺寸与记录不符")
    original = frame["original_resolution_ocr"]
    if not isinstance(original, bool) or original is not config["original_resolution_ocr"]:
        _fail(f"{label}分辨率策略标记不符")
    if original:
        if (width, height) != (config["source_width"], config["source_height"]):
            _fail(f"{label}要求原图OCR但尺寸被缩放")
    elif max(width, height) > int(config["ocr_maximum_dimension"]):
        _fail(f"{label}OCR图尺寸超过配置上限")

    lines = frame["lines"]
    if not isinstance(lines, list):
        _fail(f"{label}.lines必须是数组")
    for line_index, line in enumerate(lines):
        _validate_line(line, f"{label}.lines[{line_index}]")
    if not isinstance(detected, dict):
        _fail(f"{label}没有对应检测记录")
    without_lines = {key: value for key, value in frame.items() if key != "lines"}
    if detected != without_lines:
        _fail(f"{label}与detection.json候选记录不一致")
    frame_cache = image_path.with_suffix(".json")
    cache = _read(frame_cache)
    if (set(cache) != {"signature", "frame_image_sha256", "frame"}
            or cache["signature"] != signature
            or cache["frame_image_sha256"] != frame_sha
            or cache["frame"] != frame):
        _fail(f"{label}逐帧OCR缓存错绑或内容不一致")
    return exact_time, len(lines)


def _validate_source_record(
    record: dict[str, Any],
    result_path: Path,
    source: scene.VideoSource,
    expected_metadata: dict[str, Any],
    existing_work: Path,
    *,
    verify_source_bytes: bool,
) -> tuple[int, int]:
    required_record_keys = {
        "status", "asset_id", "source_type", "source_path", "source_name",
        "source_sha256", "source_size_bytes", "year", "parent_source",
        "parent_slide", "archive_member", "duration_seconds",
        "config_signature", "config", "raw_candidate_frame_count",
        "repeated_candidate_count", "excluded_existing_second_frame_count",
        "new_candidate_frame_count", "risk_gate", "new_ocr_frame_count",
        "per_frame_cache_hits", "complete_cache_hit", "frames",
        "decode_coverage_verified",
    }
    if set(record) != required_record_keys:
        _fail(f"{source.source_name}来源结果字段不精确")
    if record.get("status") != "complete" or record.get("decode_coverage_verified") is not True:
        _fail(f"{source.source_name}不是完整解码成功状态")
    identity = {
        "asset_id": source.asset_id,
        "source_type": source.source_type,
        "source_path": str(source.source_path),
        "source_name": source.source_name,
        "source_sha256": source.source_sha256,
        "year": source.year,
        "parent_source": source.parent_source,
        "parent_slide": source.parent_slide,
        "archive_member": source.archive_member,
    }
    for key, value in identity.items():
        if record.get(key) != value:
            _fail(f"{source.source_name}.{key}与权威manifest不符")
    stat_size = source.source_path.stat().st_size
    if (record.get("source_size_bytes") != stat_size
            or stat_size != expected_metadata["size_bytes"]):
        _fail(f"{source.source_name}源文件大小与manifest/result不一致")
    if verify_source_bytes:
        fingerprint = _fingerprint(source.source_path)
        if fingerprint["sha256"] != source.source_sha256:
            _fail(f"{source.source_name}源文件字节SHA与manifest不符")

    existing = scene.existing_second_frame_hashes(source, existing_work)
    config = record.get("config")
    if not isinstance(config, dict):
        _fail(f"{source.source_name}.config缺失")
    _expected_source_config(source, config, existing)
    signature = _sha(record.get("config_signature"), "config_signature")
    if signature != scene._config_signature(source.source_sha256, config):
        _fail(f"{source.source_name}配置签名无法由源SHA/config重算")
    if result_path.parent.name != signature or result_path.parent.parent.name != source.source_sha256:
        _fail(f"{source.source_name}result.json目录未按源SHA/配置签名绑定")

    duration = _number(record.get("duration_seconds"), "duration_seconds", minimum=0.0)
    if duration <= 0:
        _fail(f"{source.source_name}视频时长必须为正")
    second_result = _read(
        existing_work / source.source_sha256[:16] / "ocr.json")
    if float(second_result.get("duration_seconds")) != duration:
        _fail(f"{source.source_name}场景OCR与整秒OCR时长不一致")
    if verify_source_bytes:
        info = _read_video_info(source.source_path)
        if (float(info.duration) != duration
                or info.width != config["source_width"]
                or info.height != config["source_height"]):
            _fail(f"{source.source_name}媒体元数据与场景OCR记录不一致")

    raw = _integer(record.get("raw_candidate_frame_count"), "raw_candidate_frame_count")
    repeated = _integer(record.get("repeated_candidate_count"), "repeated_candidate_count")
    excluded = _integer(
        record.get("excluded_existing_second_frame_count"),
        "excluded_existing_second_frame_count")
    candidates = _integer(record.get("new_candidate_frame_count"), "new_candidate_frame_count")
    ocr_count = _integer(record.get("new_ocr_frame_count"), "new_ocr_frame_count")
    hits = _integer(record.get("per_frame_cache_hits"), "per_frame_cache_hits")
    if raw != excluded + candidates:
        _fail(f"{source.source_name}原始候选数 != 排除整秒帧数 + 新候选数")
    frames = record.get("frames")
    if not isinstance(frames, list) or candidates != ocr_count or ocr_count != len(frames):
        _fail(f"{source.source_name}候选/OCR/frames计数不一致")
    if hits > len(frames):
        _fail(f"{source.source_name}逐帧缓存命中数超过帧数")
    if not isinstance(record.get("complete_cache_hit"), bool):
        _fail(f"{source.source_name}.complete_cache_hit必须是布尔值")

    risk = record.get("risk_gate")
    expected_risk = scene.assess_candidate_risk(
        candidates, duration, existing.frame_count,
        float(config["maximum_candidate_rate_per_second"]))
    if risk != expected_risk or risk.get("rejected") is not False or risk.get("reasons") != []:
        _fail(f"{source.source_name}候选率风险门禁记录不符或未通过")

    detection_path = result_path.parent / "detection.json"
    detection = _read(detection_path)
    required_detection_keys = {
        "status", "source_sha256", "config_signature", "duration_seconds",
        "raw_candidate_frame_count", "repeated_candidate_count",
        "excluded_existing_second_frame_count", "new_candidate_frame_count",
        "risk_gate", "detection_cache_hit", "frames",
        "decode_coverage_verified",
    }
    if set(detection) != required_detection_keys:
        _fail(f"{source.source_name} detection.json字段不精确")
    detection_counts = {
        "status": "detection_complete",
        "source_sha256": source.source_sha256,
        "config_signature": signature,
        "duration_seconds": duration,
        "raw_candidate_frame_count": raw,
        "repeated_candidate_count": repeated,
        "excluded_existing_second_frame_count": excluded,
        "new_candidate_frame_count": candidates,
        "risk_gate": risk,
        "decode_coverage_verified": True,
    }
    for key, value in detection_counts.items():
        if detection.get(key) != value:
            _fail(f"{source.source_name} detection.json的{key}与result不一致")
    detected_frames = detection.get("frames")
    if not isinstance(detected_frames, list) or len(detected_frames) != candidates:
        _fail(f"{source.source_name} detection.json候选数组计数不符")

    previous_time: Fraction | None = None
    line_count = 0
    for index, (frame, detected) in enumerate(zip(frames, detected_frames)):
        previous_time, lines = _validate_frame(
            frame, detected, source=source, signature=signature, config=config,
            existing_hashes=existing.hashes, duration=duration,
            previous_time=previous_time,
            expected_frames_directory=result_path.parent / "frames")
        line_count += lines
    return len(frames), line_count


def _locate_results(cache_root: Path) -> dict[str, Path]:
    located: dict[str, Path] = {}
    for path in sorted(cache_root.glob(
            "shard-*-of-*/video-scene-ocr-cache/*/*/result.json")):
        document = _read(path)
        sha = _sha(document.get("source_sha256"), f"{path}.source_sha256")
        if sha in located:
            _fail(f"同一源SHA存在多个完整result.json：{sha}")
        located[sha] = path.resolve()
    return located


def _validate_cache_shard_assignment(
    located: dict[str, Path], sources: list[scene.VideoSource], existing_work: Path
) -> None:
    durations = scene.source_durations(sources, existing_work)
    shards, _ = scene.greedy_duration_shards(sources, durations, 4)
    expected = {
        source.source_sha256: index
        for index, shard_sources in enumerate(shards)
        for source in shard_sources
    }
    for sha, path in located.items():
        shard_dir = path.parents[3].name
        match = _SHARD_RE.fullmatch(shard_dir)
        if match is None or int(match.group(2)) != 4:
            _fail(f"result.json不在合法的4分片目录：{path}")
        if int(match.group(1)) != expected.get(sha):
            _fail(f"视频SHA位于错误分片目录：{sha}")


def _validate_merged_top(document: dict[str, Any], sources: list[scene.VideoSource]) -> None:
    exact = {
        "schema_version": scene.SCHEMA_VERSION,
        "processor_version": scene.PROCESSOR_VERSION,
        "status": "complete",
        "mode": "ocr",
        "source_count": scene.SOURCE_COUNT,
        "completed_source_count": scene.SOURCE_COUNT,
        "failed_source_count": 0,
        "config": scene.build_config(),
        "failures": [],
        "coverage_verified": True,
        "shard": None,
    }
    for key, value in exact.items():
        if document.get(key) != value:
            _fail(f"合并文档顶层字段{key}不符")
    records = document.get("sources")
    if (not isinstance(records, list) or len(records) != scene.SOURCE_COUNT
            or [item.get("source_sha256") for item in records
                if isinstance(item, dict)]
            != [source.source_sha256 for source in sources]):
        _fail("合并文档sources不是权威67源的精确顺序")
    provenance = document.get("merged_shards")
    if not isinstance(provenance, list) or len(provenance) != 4:
        _fail("合并文档必须绑定4个分片来源")
    if [item.get("shard_index") for item in provenance if isinstance(item, dict)] != [0, 1, 2, 3]:
        _fail("merged_shards分片序号不完整或无序")
    for item in provenance:
        if not isinstance(item, dict) or set(item) != {"shard_index", "path", "sha256", "source_count"}:
            _fail("merged_shards条目字段不精确")
        path = Path(item["path"])
        if not path.is_absolute() or not path.is_file():
            _fail("merged_shards绑定的分片文件不存在")
        if hashlib.sha256(path.read_bytes()).hexdigest() != _sha(item["sha256"], "merged_shards.sha256"):
            _fail("merged_shards绑定的分片文件SHA已变化")


def audit(args: argparse.Namespace) -> dict[str, Any]:
    asset_manifest = args.asset_manifest.resolve()
    embedded_manifest = args.embedded_manifest.resolve()
    existing_work = args.existing_work.resolve()
    cache_root = args.cache_root.resolve()
    try:
        sources = scene.discover_sources(asset_manifest, embedded_manifest)
    except scene.SceneOCRError as exc:
        raise SceneQAError(str(exc)) from exc
    metadata = _source_expected_metadata(asset_manifest, embedded_manifest, sources)
    by_sha = {source.source_sha256: source for source in sources}
    located = _locate_results(cache_root)
    _validate_cache_shard_assignment(located, sources, existing_work)

    if args.completed_cache_audit:
        selected_shas = [source.source_sha256 for source in sources
                         if source.source_sha256 in located]
        if not selected_shas:
            _fail("没有找到任何完整的每来源result.json")
        mode = "completed_cache_snapshot"
        coverage_complete = len(selected_shas) == scene.SOURCE_COUNT
        records = [_read(located[sha]) for sha in selected_shas]
        verify_source_bytes = args.verify_source_bytes
    else:
        merged_path = args.scene_result.resolve(strict=True)
        document = _read(merged_path)
        _validate_merged_top(document, sources)
        selected_shas = [source.source_sha256 for source in sources]
        missing_cache = [sha for sha in selected_shas if sha not in located]
        if missing_cache:
            _fail(f"合并文档有{len(missing_cache)}个来源找不到底层完整result.json")
        records = document["sources"]
        mode = "formal_merged"
        coverage_complete = True
        # Formal QA always hashes source bytes and reopens media metadata.
        verify_source_bytes = True

    total_frames = 0
    total_lines = 0
    for sha, record in zip(selected_shas, records):
        if not isinstance(record, dict) or record.get("source_sha256") != sha:
            _fail(f"来源记录顺序/SHA错绑：{sha}")
        frames, lines = _validate_source_record(
            record, located[sha], by_sha[sha], metadata[sha], existing_work,
            verify_source_bytes=verify_source_bytes)
        # The per-source cache is canonical; a resumed shard may differ only in
        # its truthful cache-hit diagnostic.
        cached_record = _read(located[sha])
        comparable_record = dict(record)
        comparable_cache = dict(cached_record)
        comparable_record.pop("complete_cache_hit", None)
        comparable_cache.pop("complete_cache_hit", None)
        if comparable_record != comparable_cache:
            _fail(f"合并/分片来源记录与底层完整缓存不一致：{sha}")
        total_frames += frames
        total_lines += lines
    return {
        "schema_version": "video-scene-ocr-qa-v1",
        "status": "pass",
        "mode": mode,
        "audited_source_count": len(selected_shas),
        "authoritative_source_count": scene.SOURCE_COUNT,
        "coverage_complete": coverage_complete,
        "source_byte_hash_verified": verify_source_bytes,
        "audited_ocr_frame_count": total_frames,
        "audited_ocr_line_count": total_lines,
        "anomaly_count": 0,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="严格审计v2视频场景OCR证据")
    parser.add_argument("--asset-manifest", type=Path,
                        default=ROOT / ".work/first-principles-2026/asset_manifest.json")
    parser.add_argument("--embedded-manifest", type=Path,
                        default=ROOT / ".work/embedded-office/manifest.json")
    parser.add_argument("--existing-work", type=Path,
                        default=ROOT / ".work/single-video")
    parser.add_argument("--cache-root", type=Path,
                        default=ROOT / ".work/first-principles-2026/scene-ocr-shards")
    parser.add_argument("--scene-result", type=Path,
                        default=ROOT / ".work/first-principles-2026/video_scene_ocr.json")
    parser.add_argument("--completed-cache-audit", action="store_true",
                        help="只审计当前已完成的每来源缓存，不声称67源覆盖完成")
    parser.add_argument("--verify-source-bytes", action="store_true",
                        help="缓存快照模式额外重算源文件SHA；正式合并模式始终执行")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        report = audit(parse_args(argv))
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, SceneQAError,
            scene.SceneOCRError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
