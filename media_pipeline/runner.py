"""Explicit single-video orchestration; recognition and review are separate stages."""
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
import tempfile
import threading
from typing import Any

import av

ROOT = Path(__file__).resolve().parent.parent
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ogv"}
PRINT_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def inspect_source(source: Path) -> dict:
    source = source.resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError("必须明确指定一个视频文件；本入口不接受目录或批处理。")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ValueError("文件不含视频流。")
        if not container.streams.audio:
            raise ValueError("本次双路测试需要有音轨的视频。")
        video = container.streams.video[0]
        audio = container.streams.audio[0]
        timeline_origin = float(container.start_time or 0) / av.time_base
        video_start = float((video.start_time or 0) * video.time_base) - timeline_origin
        duration = video_start + float(video.duration * video.time_base) if video.duration else float(container.duration or 0) / av.time_base
        if duration <= 0:
            raise ValueError("无法确认视频时长。")
        audio_start = float((audio.start_time or 0) * audio.time_base) - timeline_origin
        audio_duration = float(audio.duration * audio.time_base) if audio.duration else duration
        return {
            "schema_version": "1.0", "source_path": str(source), "source_name": source.name,
            "sha256": digest.hexdigest(), "size_bytes": source.stat().st_size,
            "duration_seconds": duration, "timeline_origin_seconds": timeline_origin,
            "width": video.width, "height": video.height,
            "audio_start_seconds": audio_start, "audio_duration_seconds": audio_duration,
            "audio_end_seconds": audio_start + audio_duration,
            "asr_model": "mimo-v2.5-asr", "ocr_interval_seconds": 1.0,
            "expected_frame_count": math.ceil(duration),
        }


def validate_results(manifest: dict, asr: dict, ocr: dict) -> dict:
    """Reject missing seconds, discontinuous audio and fabricated complete status."""
    errors: list[str] = []
    source_hash = manifest.get("sha256")
    if not source_hash or asr.get("source_sha256") != source_hash or ocr.get("source_fingerprint", {}).get("sha256") != source_hash:
        errors.append("ASR/OCR 源文件指纹与本视频不匹配")
    coverage = asr.get("coverage", {})
    if coverage.get("complete") is not True or coverage.get("sample_count", 0) <= 0 or coverage.get("covered_sample_count") != coverage.get("sample_count"):
        errors.append("ASR 未验证全部音频样本覆盖")
    if ocr.get("coverage_verified") is not True:
        errors.append("OCR 未验证完整视频解码")
    if ocr.get("engine") != "RapidOCR" or ocr.get("sample_interval_seconds") != 1.0:
        errors.append("OCR 引擎或逐秒采样配置不匹配")
    frames = ocr.get("frames", [])
    expected = manifest["expected_frame_count"]
    if len(frames) != expected or ocr.get("expected_frame_count") != expected:
        errors.append(f"OCR 帧数不完整：应 {expected}，实际 {len(frames)}")
    targets = [frame.get("timestamp_seconds") for frame in frames]
    if targets != list(range(expected)):
        errors.append("OCR 目标秒存在遗漏、重复或乱序")
    previous_actual = -float("inf")
    for frame in frames:
        actual = frame.get("actual_timestamp_seconds")
        if actual is None or not math.isfinite(actual) or actual < previous_actual:
            errors.append("OCR 实际帧时间缺失或非顺序")
            break
        if abs(actual - frame["timestamp_seconds"]) > 1.0:
            errors.append("OCR 实际帧离目标秒超过一秒")
            break
        previous_actual = actual
        if "lines" not in frame or not Path(frame.get("image_path", "")).is_file():
            errors.append("OCR 帧记录或证据图片缺失")
            break
    if asr.get("model") != "mimo-v2.5-asr":
        errors.append("ASR 返回模型与指定模型不符")
    segments = asr.get("segments", [])
    if not segments:
        errors.append("ASR 未返回任何音频段")
    else:
        start = segments[0]["start_seconds"]
        end = segments[-1]["end_seconds"]
        if abs(start - manifest["audio_start_seconds"]) > 0.1:
            errors.append("ASR 未覆盖音轨开头")
        if abs(end - manifest["audio_end_seconds"]) > 0.1:
            errors.append("ASR 未覆盖音轨结尾")
        for index, segment in enumerate(segments):
            if segment["end_seconds"] <= segment["start_seconds"]:
                errors.append("ASR 音频段长度无效")
            if index and abs(segments[index - 1]["end_seconds"] - segment["start_seconds"]) > 0.001:
                errors.append("ASR 段落存在时间间隙或重叠")
            if not isinstance(segment.get("text"), str):
                errors.append("ASR 文本字段缺失")
            if segment.get("finish_reason") != "stop":
                errors.append("ASR 存在未正常完成的响应")
    result = {
        "checked_at": now(), "passed": not errors, "errors": errors,
        "expected_ocr_frames": expected, "actual_ocr_frames": len(frames),
        "ocr_text_frames": sum(bool(frame.get("lines")) for frame in frames),
        "asr_segment_count": len(segments),
    }
    if errors:
        raise ValueError("；".join(errors))
    return result


def progress(stage: str, message: Any) -> None:
    with PRINT_LOCK:
        print(f"[{stage}] {message}", flush=True)


def recognize(source: Path, work_dir: Path, manifest: dict, key: str) -> None:
    from .asr import transcribe
    from .ocr import recognize as recognize_ocr

    state = {"status": "running", "started_at": now(), "stages": {"asr": "running", "ocr": "running"}}
    atomic_json(work_dir / "state.json", state)
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="single-video") as executor:
        futures = {
            executor.submit(transcribe, source, work_dir, key, progress=lambda msg: progress("ASR", msg)): "asr",
            executor.submit(recognize_ocr, source, work_dir, progress=lambda msg: progress("OCR", msg)): "ocr",
        }
        for future in as_completed(futures):
            stage = futures[future]
            try:
                result = future.result()
                atomic_json(work_dir / f"{stage}.json", result)
                state["stages"][stage] = "complete"
                progress(stage.upper(), "识别完成")
            except Exception as exc:
                # Providers must not expose request bodies. Redact credentials again here.
                safe_message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
                state["stages"][stage] = "failed"
                errors.append(f"{stage}: {type(exc).__name__}: {safe_message}")
                progress(stage.upper(), errors[-1])
            atomic_json(work_dir / "state.json", state)
    if errors:
        state.update(status="failed", errors=errors, ended_at=now())
        atomic_json(work_dir / "state.json", state)
        raise RuntimeError("识别未全部通过，请查看不含密钥的 state.json。")
    try:
        validation = validate_results(manifest, read_json(work_dir / "asr.json"), read_json(work_dir / "ocr.json"))
    except ValueError as exc:
        state.update(status="failed_validation", errors=[str(exc)], ended_at=now())
        atomic_json(work_dir / "state.json", state)
        raise
    atomic_json(work_dir / "validation.json", validation)
    state.update(status="awaiting_review", ended_at=now())
    atomic_json(work_dir / "state.json", state)
    progress("CHECK", f"完整性检查通过，结果等待复核：{work_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单视频：MiMo ASR + 每秒 RapidOCR；不执行批量。")
    parser.add_argument("source", type=Path, help="一个明确的视频路径")
    parser.add_argument("--work-root", type=Path, default=ROOT / ".work" / "single-video")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "单视频测试")
    parser.add_argument("--report-only", action="store_true", help="只读取缓存和复核记录导出，不调用 API")
    parser.add_argument("--review-file", type=Path, help="人工复核 JSON；导出必须提供")
    args = parser.parse_args(argv)
    try:
        manifest = inspect_source(args.source)
        work_dir = args.work_root.resolve() / manifest["sha256"][:16]
        work_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = work_dir / "manifest.json"
        if args.report_only:
            previous = read_json(manifest_path)
            if previous["sha256"] != manifest["sha256"]:
                raise ValueError("源文件与缓存指纹不匹配。")
        else:
            atomic_json(manifest_path, manifest)
            key = os.environ.get("MIMO_API_KEY")
            if not key:
                if not sys.stdin.isatty():
                    raise ValueError("请提供 MIMO_API_KEY 环境变量，或在交互终端隐藏输入密钥。")
                key = getpass.getpass("MiMo API key (hidden): ").strip()
            if not key:
                raise ValueError("MiMo API 密钥为空。")
            recognize(Path(manifest["source_path"]), work_dir, manifest, key)
            del key
        if args.review_file:
            from .report import build_report

            asr, ocr = read_json(work_dir / "asr.json"), read_json(work_dir / "ocr.json")
            validation = validate_results(manifest, asr, ocr)
            review = read_json(args.review_file)
            if review.get("source_sha256") != manifest["sha256"]:
                raise ValueError("复核记录必须包含与源视频一致的 source_sha256。")
            if not review.get("reviewer") or not review.get("reviewed_at"):
                raise ValueError("复核记录缺少复核者或复核时间。")
            output_path = args.output_dir.resolve() / f"{Path(manifest['source_name']).stem}_OCR-ASR校对.docx"
            build_report(manifest, asr, ocr, review, output_path)
            atomic_json(work_dir / "validation.json", validation)
            atomic_json(work_dir / "review.json", review)
            atomic_json(work_dir / "state.json", {"status": "complete", "ended_at": now(), "report": str(output_path)})
            print(f"REPORT {output_path}", flush=True)
        elif args.report_only:
            raise ValueError("导出需要 --review-file，避免将未复核识别结果标成校对版。")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
