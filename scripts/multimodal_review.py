from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import signal
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import av
import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
WHISPER_METADATA_DIR = OUTPUT_DIR / "转写元数据"
WHISPER_TRANSCRIPT_DIR = OUTPUT_DIR / "逐个转写"
SENSEVOICE_DIR = OUTPUT_DIR / "多模型转写" / "SenseVoice"
OCR_DIR = OUTPUT_DIR / "视频OCR"
NORMALIZED_DIR = OUTPUT_DIR / "规范化数据"
SENSEVOICE_MODEL_DIR = ROOT / "models" / "sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17"
MEDIA_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".webm",
    ".ogg", ".ogv", ".oga", ".aac", ".flac", ".wma", ".avi",
}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".ogv", ".avi"}

# 仅收录已经由双模型上下文或明确行业固定搭配确认的高把握纠错。
# 不做开放式“润色”，以免把人名、数字和专业术语改错。
VERIFIED_CORRECTIONS = (
    ("真确性", "正确性"),
    ("清华原中最早的举火人施谎", "清华园中最早的举火人施滉"),
    ("南京五花台救命", "南京雨花台就义"),
    ("各界青年热烈相应", "各界青年热烈响应"),
    ("个体工商户云叶之兆", "个体工商户营业执照"),
    ("一群将人", "一群匠人"),
    ("电战", "电站"),
    ("假设乌东德电站", "架设乌东德电站"),
    ("送完越赶到大湾区", "送往粤港澳大湾区"),
    ("运输所到人形站到施工平台商体某顾", "运输索道、人行栈道、施工平台、山体锚固"),
    ("一个人起价", "一个人请假"),
    ("工气也比较急", "工期也比较急"),
    ("16万嗯", "16万元"),
    ("顾顾尽", "鼓鼓劲"),
    ("我太慢了这两天我太慢了", "我太忙了，这两天我太忙了"),
    ("即可开挖", "基坑开挖"),
    ("基坑开挖每天进度只有60米", "基坑开挖每天进度只有60厘米"),
    ("灯塔材", "吨塔材"),
    ("加设导线", "架设导线"),
    ("肌肤垂直", "几乎垂直"),
    ("经电不移", "坚定不移"),
    ("提前十五天运工", "提前十五天竣工"),
    ("西电东宋", "西电东送"),
    ("书店线路", "输电线路"),
    ("深岗骄傲何止", "深感骄傲和自豪"),
    ("什么是读品", "什么是毒品"),
    ("情结较轻", "情节较轻"),
    ("接放邻居", "街坊邻居"),
    ("少量踩头", "少量彩头"),
    ("系统内最年轻的账户", "系统内最年轻的干部"),
    ("酒价的事", "酒驾的事"),
    ("未经直班调度源许可", "未经值班调度员许可"),
    ("容冰操作授权", "融冰操作授权"),
    ("三向邮件变压器", "三相油浸变压器"),
    ("预应力追行水泥干", "预应力锥形水泥杆"),
)

stop_requested = False


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding=encoding)
    temporary.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def relative_key(path: Path) -> str:
    return path.relative_to(DATA_DIR).as_posix()


def derived_path(source: Path, root: Path, suffix: str) -> Path:
    relative = source.relative_to(DATA_DIR)
    return root / relative.parent / f"{relative.stem}{suffix}"


def list_media(match: str = "") -> list[Path]:
    media = sorted(
        (path for path in DATA_DIR.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES),
        key=relative_key,
    )
    if match:
        media = [path for path in media if match.casefold() in relative_key(path).casefold()]
    return media


def media_properties(path: Path) -> dict[str, Any]:
    with av.open(str(path)) as container:
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        audio_streams = [stream for stream in container.streams if stream.type == "audio"]
        video_streams = [stream for stream in container.streams if stream.type == "video"]
        result: dict[str, Any] = {
            "duration_seconds": round(duration, 3),
            "has_audio": bool(audio_streams),
            "has_video": bool(video_streams),
        }
        if video_streams:
            stream = video_streams[0]
            result.update(width=stream.width, height=stream.height)
        return result


def handle_signal(_signum: int, _frame: Any) -> None:
    global stop_requested
    stop_requested = True
    print("\n收到停止信号，将在当前文件处理完成后安全退出。", flush=True)


def format_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def clean_text(text: str, terminal: bool = False) -> str:
    """做保守的排版校对，不臆测人名、数字或专业术语。"""
    text = re.sub(r"<\|[^|]+\|>", "", text)
    text = text.replace("\u3000", " ").replace("… …", "……")
    text = re.sub(r"\s+", " ", text).strip()
    if re.search(r"[\u3400-\u9fff]", text):
        text = text.replace(",", "，").replace("?", "？").replace("!", "！").replace(";", "；")
        text = re.sub(r"\s+([，。！？；：、])", r"\1", text)
        text = re.sub(r"([，。！？；：、])\s+", r"\1", text)
        text = re.sub(r"([\u3400-\u9fff])\s+([\u3400-\u9fff])", r"\1\2", text)
        text = re.sub(r"[，、；]+。", "。", text)
        text = re.sub(r"。{2,}", "。", text)
    if terminal and text and text[-1] in "，、,":
        text = text[:-1] + "。"
    if terminal and text and text[-1] not in "。！？!?；;：:…":
        text += "。"
    return text


def comparison_text(text: str) -> str:
    text = clean_text(text).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def proofread_text(text: str) -> tuple[str, list[dict[str, str]]]:
    corrections: list[dict[str, str]] = []
    for source, replacement in VERIFIED_CORRECTIONS:
        if source not in text:
            continue
        text = text.replace(source, replacement)
        corrections.append({"from": source, "to": replacement})
    return clean_text(text, terminal=True), corrections


def similarity(left: str, right: str) -> float | None:
    left_key = comparison_text(left)
    right_key = comparison_text(right)
    if not left_key or not right_key:
        return None
    return round(SequenceMatcher(None, left_key, right_key, autojunk=False).ratio(), 4)


def paragraphize(parts: Iterable[str], max_chars: int = 220) -> str:
    paragraphs: list[str] = []
    current = ""
    for part in parts:
        part = clean_text(part, terminal=True)
        if not part:
            continue
        if current and len(current) + len(part) > max_chars:
            paragraphs.append(current)
            current = ""
        current += part
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def decode_audio(path: Path, sample_rate: int = 16_000) -> np.ndarray:
    chunks: list[np.ndarray] = []
    with av.open(str(path)) as container:
        streams = [stream for stream in container.streams if stream.type == "audio"]
        if not streams:
            return np.empty(0, dtype=np.float32)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
        for frame in container.decode(streams[0]):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().reshape(-1).astype(np.float32, copy=False))
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1).astype(np.float32, copy=False))
    return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)


def quiet_split_points(
    samples: np.ndarray,
    sample_rate: int = 16_000,
    target_seconds: float = 22.0,
    search_seconds: float = 3.0,
) -> list[tuple[int, int]]:
    """在目标长度附近寻找能量最低点，降低切断词语的概率。"""
    if samples.size == 0:
        return []
    target = max(5, round(target_seconds * sample_rate))
    search = max(sample_rate, round(search_seconds * sample_rate))
    energy_window = max(1, round(0.12 * sample_rate))
    chunks: list[tuple[int, int]] = []
    start = 0
    while samples.size - start > target + search:
        center = start + target
        low = max(start + sample_rate * 5, center - search)
        high = min(samples.size - energy_window, center + search)
        candidates = np.arange(low, high + 1, energy_window, dtype=np.int64)
        if candidates.size:
            energies = np.array(
                [np.mean(np.abs(samples[index:index + energy_window])) for index in candidates],
                dtype=np.float32,
            )
            end = int(candidates[int(np.argmin(energies))] + energy_window // 2)
        else:
            end = min(samples.size, center)
        chunks.append((start, end))
        start = end
    if start < samples.size:
        chunks.append((start, samples.size))
    return chunks


def load_sensevoice(num_threads: int):
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError("缺少 sherpa-onnx，请先安装 requirements.txt") from exc
    model = SENSEVOICE_MODEL_DIR / "model.int8.onnx"
    tokens = SENSEVOICE_MODEL_DIR / "tokens.txt"
    if not model.is_file() or not tokens.is_file():
        raise RuntimeError(f"未找到 SenseVoice 本地模型：{SENSEVOICE_MODEL_DIR}")
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model),
        tokens=str(tokens),
        use_itn=True,
        num_threads=max(1, num_threads),
        debug=False,
    )


def sensevoice_transcribe(source: Path, recognizer: Any, chunk_seconds: float) -> dict[str, Any]:
    started = time.perf_counter()
    sample_rate = 16_000
    samples = decode_audio(source, sample_rate)
    if samples.size == 0:
        raise RuntimeError("媒体文件没有可解码的音轨")
    segments: list[dict[str, Any]] = []
    for index, (start_sample, end_sample) in enumerate(
        quiet_split_points(samples, sample_rate, target_seconds=chunk_seconds), 1
    ):
        chunk = samples[start_sample:end_sample]
        rms = float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64))) if chunk.size else 0.0
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, chunk)
        recognizer.decode_stream(stream)
        raw_result = json.loads(str(stream.result))
        text = clean_text(raw_result.get("text", ""), terminal=True)
        if not text:
            continue
        start_seconds = start_sample / sample_rate
        end_seconds = end_sample / sample_rate
        relative_timestamps = raw_result.get("timestamps") or []
        segments.append(
            {
                "index": index,
                "start": round(start_seconds, 3),
                "end": round(end_seconds, 3),
                "text": text,
                "rms": round(rms, 7),
                "token_timestamps": [round(start_seconds + float(value), 3) for value in relative_timestamps],
                "tokens": raw_result.get("tokens") or [],
                "language": raw_result.get("lang") or "auto",
            }
        )
    text = paragraphize(segment["text"] for segment in segments)
    return {
        "source": relative_key(source),
        "model": SENSEVOICE_MODEL_DIR.name,
        "engine": "sherpa-onnx",
        "local_only": True,
        "sample_rate": sample_rate,
        "chunk_target_seconds": chunk_seconds,
        "audio_duration_seconds": round(samples.size / sample_rate, 3),
        "processing_seconds": round(time.perf_counter() - started, 3),
        "generated_at": now_text(),
        "segments": segments,
        "text": text,
    }


def update_batch_status(root: Path, key: str, update: dict[str, Any]) -> None:
    status_path = root / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {"items": {}}
    except (OSError, json.JSONDecodeError):
        status = {"items": {}}
    status.setdefault("items", {})[key] = update
    status["updated_at"] = now_text()
    counts: dict[str, int] = {}
    for item in status["items"].values():
        state = item.get("status", "unknown")
        counts[state] = counts.get(state, 0) + 1
    status["counts"] = counts
    atomic_write_json(status_path, status)


def run_asr(media: list[Path], args: argparse.Namespace) -> None:
    candidates = [
        source for source in media
        if media_properties(source)["has_audio"]
        and (args.force or not derived_path(source, SENSEVOICE_DIR, ".json").is_file())
    ]
    candidates.sort(key=lambda path: media_properties(path)["duration_seconds"])
    if args.limit:
        candidates = candidates[:args.limit]
    if not candidates:
        print("SenseVoice：没有待处理文件。", flush=True)
        return
    recognizer = load_sensevoice(args.asr_threads)
    for sequence, source in enumerate(candidates, 1):
        if stop_requested:
            break
        key = relative_key(source)
        print(f"SenseVoice [{sequence}/{len(candidates)}] {key}", flush=True)
        started = time.perf_counter()
        try:
            result = sensevoice_transcribe(source, recognizer, args.asr_chunk_seconds)
            json_path = derived_path(source, SENSEVOICE_DIR, ".json")
            txt_path = derived_path(source, SENSEVOICE_DIR, ".txt")
            atomic_write_json(json_path, result)
            timestamped = "\n".join(
                f"[{format_timestamp(item['start'])} --> {format_timestamp(item['end'])}] {item['text']}"
                for item in result["segments"]
            )
            atomic_write_text(txt_path, result["text"].strip() + "\n\n--- 带时间戳分段 ---\n" + timestamped + "\n")
            update_batch_status(
                SENSEVOICE_DIR,
                key,
                {
                    "status": "completed",
                    "segments": len(result["segments"]),
                    "processing_seconds": result["processing_seconds"],
                    "updated_at": now_text(),
                },
            )
            print(f"  完成：{len(result['segments'])} 段，耗时 {result['processing_seconds']:.1f} 秒", flush=True)
        except Exception as exc:
            update_batch_status(
                SENSEVOICE_DIR,
                key,
                {
                    "status": "error",
                    "error": str(exc),
                    "processing_seconds": round(time.perf_counter() - started, 3),
                    "updated_at": now_text(),
                },
            )
            print(f"  失败：{exc}", flush=True)


def sample_times(duration: float, interval: float) -> list[float]:
    if duration <= 0:
        return []
    values = {min(duration - 0.05, value) for value in (1.0, 3.0, 6.0, 10.0) if value < duration}
    current = max(15.0, interval)
    while current < duration:
        values.add(current)
        current += interval
    if duration > 5:
        values.add(max(0.0, duration - 1.0))
    return sorted(value for value in values if value >= 0)


def frame_at(container: av.container.InputContainer, stream: Any, seconds: float):
    target_pts = max(0, int(seconds / float(stream.time_base)))
    container.seek(target_pts, stream=stream, any_frame=False, backward=True)
    fallback = None
    for index, frame in enumerate(container.decode(stream)):
        fallback = frame
        frame_seconds = float(frame.pts * stream.time_base) if frame.pts is not None else seconds
        if frame_seconds >= seconds - 0.08 or index >= 120:
            return frame, frame_seconds
    if fallback is None:
        return None, seconds
    frame_seconds = float(fallback.pts * stream.time_base) if fallback.pts is not None else seconds
    return fallback, frame_seconds


def image_change_score(image: np.ndarray, previous: np.ndarray | None) -> tuple[float, np.ndarray]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    fingerprint = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
    if previous is None:
        return 1.0, fingerprint
    difference = float(np.mean(cv2.absdiff(fingerprint, previous)) / 255.0)
    return round(difference, 5), fingerprint


def normalize_ocr_lines(txts: Iterable[str], scores: Iterable[float], minimum_score: float) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text, score in zip(txts, scores):
        text = clean_text(str(text))
        key = comparison_text(text)
        if float(score) < minimum_score or len(key) < 2 or key in seen:
            continue
        seen.add(key)
        lines.append({"text": text, "confidence": round(float(score), 4)})
    return lines


def separate_repeated_ocr_lines(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """把跨大量画面重复出现的水印/栏目条提取为单独字段。"""
    if len(records) < 4:
        return records, []
    occurrences: list[tuple[str, str, int]] = []
    for record_index, record in enumerate(records):
        for line in record["lines"]:
            key = comparison_text(line["text"])
            if len(key) >= 5:
                occurrences.append((key, line["text"], record_index))
    minimum_frames = max(3, math.ceil(len(records) * 0.45))
    repeated_keys: set[str] = set()
    repeated_labels: list[str] = []
    for key, label, _ in occurrences:
        matching_frames = {
            frame_index
            for other_key, _, frame_index in occurrences
            if SequenceMatcher(None, key, other_key, autojunk=False).ratio() >= 0.78
        }
        if len(matching_frames) < minimum_frames:
            continue
        repeated_keys.add(key)
        if not any(
            (similarity(label, existing) or 0.0) >= 0.78
            for existing in repeated_labels
        ):
            repeated_labels.append(label)
    if not repeated_keys:
        return records, []
    filtered: list[dict[str, Any]] = []
    for record in records:
        lines = [
            line for line in record["lines"]
            if not any(
                SequenceMatcher(
                    None,
                    comparison_text(line["text"]),
                    repeated,
                    autojunk=False,
                ).ratio() >= 0.78
                for repeated in repeated_keys
            )
        ]
        if lines:
            filtered.append({**record, "lines": lines, "text": "\n".join(line["text"] for line in lines)})
    return filtered, repeated_labels


def ocr_video(source: Path, engine: Any, args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    properties = media_properties(source)
    whisper_meta = read_json(derived_path(source, WHISPER_METADATA_DIR, ".json"))
    sense_meta = read_json(derived_path(source, SENSEVOICE_DIR, ".json"))
    whisper_segments = (whisper_meta or {}).get("segments") or []
    if not properties["has_audio"]:
        interval = args.ocr_no_audio_interval
    elif not whisper_segments:
        interval = args.ocr_single_asr_interval
    else:
        interval = args.ocr_interval
    times = sample_times(properties["duration_seconds"], interval)
    if whisper_meta and sense_meta:
        compared = choose_consensus_segments(
            whisper_segments,
            sense_meta.get("segments") or [],
        )
        review_times = [
            (float(segment["start"]) + float(segment["end"])) / 2
            for segment in compared
            if (
                segment.get("agreement") is not None
                and float(segment["agreement"]) < 0.60
            )
            or (
                segment.get("whisper_confidence") is not None
                and float(segment["whisper_confidence"]) < 0.65
            )
        ][:args.ocr_review_frame_limit]
        times = sorted({round(value, 2) for value in [*times, *review_times]})
    records: list[dict[str, Any]] = []
    skipped_similar = 0
    previous_fingerprint: np.ndarray | None = None
    previous_text_key = ""
    frame_root = derived_path(source, OCR_DIR / "关键帧", "")
    with av.open(str(source)) as container:
        video_streams = [stream for stream in container.streams if stream.type == "video"]
        if not video_streams:
            raise RuntimeError("媒体文件没有视频轨")
        stream = video_streams[0]
        stream.thread_type = "AUTO"
        for requested_seconds in times:
            frame, actual_seconds = frame_at(container, stream, requested_seconds)
            if frame is None:
                continue
            image = frame.to_ndarray(format="bgr24")
            change_score, fingerprint = image_change_score(image, previous_fingerprint)
            previous_fingerprint = fingerprint
            if change_score < args.ocr_visual_change:
                skipped_similar += 1
                continue
            height, width = image.shape[:2]
            scale = min(1.0, args.ocr_max_dimension / max(height, width))
            ocr_image = (
                cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                if scale < 1.0 else image
            )
            result = engine(ocr_image)
            lines = normalize_ocr_lines(result.txts or (), result.scores or (), args.ocr_min_confidence)
            if not lines:
                continue
            text_key = "|".join(comparison_text(line["text"]) for line in lines)
            text_similarity = similarity(text_key, previous_text_key) if previous_text_key else 0.0
            if text_similarity is not None and text_similarity >= args.ocr_text_similarity:
                continue
            previous_text_key = text_key
            timestamp_label = format_timestamp(actual_seconds).replace(":", "-")
            frame_path = frame_root / f"{timestamp_label}.jpg"
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                atomic_write_bytes(frame_path, encoded.tobytes())
            records.append(
                {
                    "requested_seconds": round(requested_seconds, 3),
                    "timestamp_seconds": round(actual_seconds, 3),
                    "timestamp": format_timestamp(actual_seconds),
                    "visual_change_score": change_score,
                    "frame": frame_path.relative_to(ROOT).as_posix() if ok else "",
                    "lines": lines,
                    "text": "\n".join(line["text"] for line in lines),
                }
            )
    records, repeated_overlay_text = separate_repeated_ocr_lines(records)
    return {
        "source": relative_key(source),
        "engine": "RapidOCR 3.9.2 / PP-OCRv6 small",
        "local_only": True,
        "duration_seconds": properties["duration_seconds"],
        "sample_interval_seconds": interval,
        "sampled_frame_count": len(times),
        "similar_frame_skip_count": skipped_similar,
        "ocr_frame_count": len(records),
        "minimum_confidence": args.ocr_min_confidence,
        "repeated_overlay_text": repeated_overlay_text,
        "processing_seconds": round(time.perf_counter() - started, 3),
        "generated_at": now_text(),
        "frames": records,
    }


def run_ocr(media: list[Path], args: argparse.Namespace) -> None:
    candidates = [
        source for source in media
        if source.suffix.lower() in VIDEO_SUFFIXES
        and (args.force or not derived_path(source, OCR_DIR, ".json").is_file())
    ]
    candidates.sort(key=lambda path: (media_properties(path)["has_audio"], media_properties(path)["duration_seconds"]))
    if args.limit:
        candidates = candidates[:args.limit]
    if not candidates:
        print("OCR：没有待处理文件。", flush=True)
        return
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError("缺少 rapidocr，请先安装 requirements.txt") from exc
    engine = RapidOCR()
    for sequence, source in enumerate(candidates, 1):
        if stop_requested:
            break
        key = relative_key(source)
        print(f"OCR [{sequence}/{len(candidates)}] {key}", flush=True)
        started = time.perf_counter()
        try:
            result = ocr_video(source, engine, args)
            json_path = derived_path(source, OCR_DIR, ".json")
            txt_path = derived_path(source, OCR_DIR, ".txt")
            atomic_write_json(json_path, result)
            sections = []
            for frame in result["frames"]:
                sections.append(f"[{frame['timestamp']}]\n{frame['text']}")
            atomic_write_text(txt_path, "\n\n".join(sections) + ("\n" if sections else ""))
            update_batch_status(
                OCR_DIR,
                key,
                {
                    "status": "completed",
                    "sampled_frames": result["sampled_frame_count"],
                    "ocr_frames": result["ocr_frame_count"],
                    "processing_seconds": result["processing_seconds"],
                    "updated_at": now_text(),
                },
            )
            print(
                f"  完成：抽样 {result['sampled_frame_count']} 帧，保留文字帧 {result['ocr_frame_count']} 个，"
                f"耗时 {result['processing_seconds']:.1f} 秒",
                flush=True,
            )
        except Exception as exc:
            update_batch_status(
                OCR_DIR,
                key,
                {
                    "status": "error",
                    "error": str(exc),
                    "processing_seconds": round(time.perf_counter() - started, 3),
                    "updated_at": now_text(),
                },
            )
            print(f"  失败：{exc}", flush=True)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def overlapping_text(segments: list[dict[str, Any]], start: float, end: float) -> str:
    parts: list[str] = []
    for segment in segments:
        if float(segment.get("end", 0)) <= start or float(segment.get("start", 0)) >= end:
            continue
        tokens = segment.get("tokens") or []
        timestamps = segment.get("token_timestamps") or []
        if tokens and len(tokens) == len(timestamps):
            selected = [
                str(token)
                for token, timestamp in zip(tokens, timestamps)
                if start - 0.25 <= float(timestamp) <= end + 0.20
            ]
            if selected:
                parts.append("".join(selected))
            # 该时间窗可能正好落在这个 SenseVoice 分段的尾部静音处；
            # 有逐字时间戳却没有命中时，不应退回整段文本。
            continue
        parts.append(segment.get("text", ""))
    return clean_text("".join(parts))


def looks_repetitive(text: str) -> bool:
    key = comparison_text(text)
    if len(key) < 20:
        return False
    for width in range(4, min(20, len(key) // 3) + 1):
        for start in range(0, len(key) - width * 2 + 1):
            token = key[start:start + width]
            if token and key.count(token) >= 4:
                return True
    return False


def looks_like_prompt_hallucination(text: str) -> bool:
    key = comparison_text(text)
    return any(
        marker in key
        for marker in (
            "请准确转写完整语句人名和数字",
            "以下是普通话音视频内容",
            "文件主题",
        )
    )


def select_ocr_subtitle(
    ocr_frames: list[dict[str, Any]],
    start: float,
    end: float,
    whisper_text: str,
    sense_text: str,
    confidence: float,
    agreement: float | None,
) -> dict[str, Any] | None:
    nearby_lines: list[dict[str, Any]] = []
    for frame in ocr_frames:
        timestamp = float(frame.get("timestamp_seconds", -1))
        if timestamp < start - 0.65 or timestamp > end + 0.65:
            continue
        for line in frame.get("lines") or []:
            text = clean_text(line.get("text", ""))
            length = len(comparison_text(text))
            if length < 4 or length > 100:
                continue
            score = max(similarity(text, whisper_text) or 0.0, similarity(text, sense_text) or 0.0)
            nearby_lines.append(
                {
                    "text": text,
                    "confidence": float(line.get("confidence", 0.0)),
                    "similarity": score,
                    "timestamp_seconds": timestamp,
                    "frame": frame.get("frame", ""),
                }
            )
    if not nearby_lines:
        return None
    nearby_lines.sort(key=lambda item: (item["similarity"], item["confidence"]), reverse=True)
    best = nearby_lines[0]
    if best["confidence"] < 0.72:
        return None
    unreliable_audio = (confidence and confidence < 0.65) or (agreement is not None and agreement <= 0.60)
    ocr_length = len(comparison_text(best["text"]))
    whisper_length = max(1, len(comparison_text(whisper_text)))
    length_ratio = ocr_length / whisper_length
    if best["similarity"] >= 0.55 and (unreliable_audio or 0.75 <= length_ratio <= 1.50):
        return best
    # 定向抽帧附近只有一条正文时，它通常就是片中字幕；仅在语音结果本身
    # 已低置信或两模型明显分歧时采用，且限制长度，避免误用片头标题。
    if unreliable_audio and len(nearby_lines) == 1:
        audio_length = max(len(comparison_text(whisper_text)), len(comparison_text(sense_text)), 1)
        if 0.35 * audio_length <= ocr_length <= 2.2 * audio_length:
            return best
    return None


def choose_consensus_segments(
    whisper_segments: list[dict[str, Any]],
    sense_segments: list[dict[str, Any]],
    ocr_frames: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    if not whisper_segments:
        for segment in sense_segments:
            reviewed.append(
                {
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": clean_text(segment["text"], terminal=True),
                    "selected_source": "SenseVoice",
                    "whisper_text": "",
                    "sensevoice_text": segment["text"],
                    "agreement": None,
                    "review_flags": ["缺少 Whisper 对照"],
                }
            )
        return reviewed

    whisper_text_counts: dict[str, int] = {}
    for segment in whisper_segments:
        key = comparison_text(segment.get("text", ""))
        if key:
            whisper_text_counts[key] = whisper_text_counts.get(key, 0) + 1
    used_ocr: set[tuple[str, str]] = set()
    for segment in whisper_segments:
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start))
        whisper_text = clean_text(segment.get("text", ""), terminal=True)
        sense_text = overlapping_text(sense_segments, start, end)
        agreement = similarity(whisper_text, sense_text)
        confidence = float(segment.get("confidence", 0.0) or 0.0)
        flags: list[str] = []
        selected = "Whisper"
        text = whisper_text
        if not sense_text:
            flags.append("缺少 SenseVoice 对照")
        elif agreement is not None and agreement < 0.60:
            flags.append("两模型差异较大")
        elif agreement is not None and agreement < 0.90:
            flags.append("两模型存在差异")
        if confidence and confidence < 0.65:
            flags.append("Whisper 低置信度")
        if looks_repetitive(whisper_text):
            flags.append("Whisper 疑似重复")
        whisper_key = comparison_text(whisper_text)
        cross_segment_repetition = len(whisper_key) >= 8 and whisper_text_counts.get(whisper_key, 0) >= 6
        prompt_hallucination = looks_like_prompt_hallucination(whisper_text)
        if cross_segment_repetition:
            flags.append("Whisper 疑似跨段重复")
        if prompt_hallucination:
            flags.append("Whisper 疑似复述提示词")
        # 只有在 Whisper 明显不可靠且第二模型给出实质文本时才自动替换，避免改写事实。
        if sense_text and (
            (confidence and confidence < 0.45)
            or looks_repetitive(whisper_text)
            or cross_segment_repetition
            or prompt_hallucination
        ):
            text = clean_text(sense_text, terminal=True)
            selected = "SenseVoice"
            flags.append("已采用第二模型候选")
        elif prompt_hallucination:
            text = ""
            selected = "丢弃"
            flags.append("已丢弃无对照的提示词幻觉")
        ocr_candidate = select_ocr_subtitle(
            ocr_frames or [],
            start,
            end,
            whisper_text,
            sense_text,
            confidence,
            agreement,
        )
        if ocr_candidate:
            ocr_key = (ocr_candidate["frame"], comparison_text(ocr_candidate["text"]))
            if ocr_key in used_ocr:
                ocr_candidate = None
        if ocr_candidate:
            used_ocr.add((ocr_candidate["frame"], comparison_text(ocr_candidate["text"])))
            text = clean_text(ocr_candidate["text"], terminal=True)
            selected = "OCR字幕"
            flags.append("已采用同时间点高置信 OCR 字幕")
        text, corrections = proofread_text(text)
        if corrections:
            flags.append("已应用保守文字校正")
        reviewed.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "selected_source": selected,
                "whisper_text": whisper_text,
                "sensevoice_text": sense_text,
                "ocr_text": ocr_candidate["text"] if ocr_candidate else "",
                "ocr_confidence": round(ocr_candidate["confidence"], 4) if ocr_candidate else None,
                "ocr_frame": ocr_candidate["frame"] if ocr_candidate else "",
                "whisper_confidence": round(confidence, 4) if confidence else None,
                "agreement": agreement,
                "corrections": corrections,
                "review_flags": flags,
            }
        )
    return reviewed


def build_normalized_item(source: Path) -> dict[str, Any]:
    properties = media_properties(source)
    whisper_meta = read_json(derived_path(source, WHISPER_METADATA_DIR, ".json"))
    sense_meta = read_json(derived_path(source, SENSEVOICE_DIR, ".json"))
    ocr_meta = read_json(derived_path(source, OCR_DIR, ".json"))
    whisper_segments = (whisper_meta or {}).get("segments") or []
    sense_segments = (sense_meta or {}).get("segments") or []
    visual_records: list[dict[str, Any]] = []
    for frame in (ocr_meta or {}).get("frames") or []:
        normalized_lines = [
            {**line, "text": clean_text(line.get("text", ""))}
            for line in frame.get("lines") or []
            if clean_text(line.get("text", ""))
        ]
        visual_records.append(
            {
                **frame,
                "lines": normalized_lines,
                "text": "\n".join(line["text"] for line in normalized_lines),
            }
        )
    reviewed_segments = choose_consensus_segments(whisper_segments, sense_segments, visual_records)
    if not reviewed_segments and whisper_meta:
        fallback_text_path = derived_path(source, WHISPER_TRANSCRIPT_DIR, ".txt")
        if fallback_text_path.is_file():
            text = clean_text(fallback_text_path.read_text(encoding="utf-8"))
            if text and not text.startswith("["):
                reviewed_segments = [
                    {
                        "start": 0.0,
                        "end": properties["duration_seconds"],
                        "text": text,
                        "selected_source": "Whisper",
                        "whisper_text": text,
                        "sensevoice_text": "",
                        "agreement": None,
                        "review_flags": ["Whisper 元数据无分段"],
                    }
                ]
    transcript = paragraphize(segment["text"] for segment in reviewed_segments)
    agreements = [segment["agreement"] for segment in reviewed_segments if segment.get("agreement") is not None]
    flagged = [segment for segment in reviewed_segments if segment.get("review_flags")]
    coverage = {
        "whisper": bool(whisper_segments),
        "sensevoice": bool(sense_segments or sense_meta),
        "ocr": ocr_meta is not None,
    }
    if not properties["has_audio"]:
        review_status = "图像内容已自动整理，待人工抽查"
    elif not reviewed_segments:
        review_status = "无可用语音转写"
    elif any("两模型差异较大" in segment.get("review_flags", []) for segment in reviewed_segments):
        review_status = "已自动校对，含低一致性片段待人工复核"
    elif coverage["whisper"] and coverage["sensevoice"]:
        review_status = "已完成双模型自动校对，建议人工抽查"
    else:
        review_status = "仅单模型结果，待补充交叉校验"
    return {
        "schema_version": "1.0",
        "source": relative_key(source),
        "title": source.stem,
        **properties,
        "generated_at": now_text(),
        "review_status": review_status,
        "coverage": coverage,
        "quality": {
            "mean_model_agreement": round(sum(agreements) / len(agreements), 4) if agreements else None,
            "compared_segment_count": len(agreements),
            "flagged_segment_count": len(flagged),
            "ocr_keyframe_count": len(visual_records),
            "ocr_assisted_segment_count": sum(segment.get("selected_source") == "OCR字幕" for segment in reviewed_segments),
            "text_correction_count": sum(len(segment.get("corrections") or []) for segment in reviewed_segments),
        },
        "normalized_transcript": transcript,
        "segments": reviewed_segments,
        "visual_content": visual_records,
        "repeated_visual_overlay": (ocr_meta or {}).get("repeated_overlay_text") or [],
    }


def markdown_for_item(item: dict[str, Any]) -> str:
    coverage = item["coverage"]
    lines = [
        f"# {item['title']}",
        "",
        f"- 来源：`{item['source']}`",
        f"- 时长：{format_timestamp(item['duration_seconds'])}",
        f"- 校验状态：{item['review_status']}",
        f"- 数据覆盖：Whisper {'有' if coverage['whisper'] else '无'} / "
        f"SenseVoice {'有' if coverage['sensevoice'] else '无'} / OCR {'有' if coverage['ocr'] else '无'}",
        f"- 双模型平均一致度：{item['quality']['mean_model_agreement'] if item['quality']['mean_model_agreement'] is not None else '—'}",
        "",
        "## 规范化语音文本",
        "",
        item["normalized_transcript"] or "[没有可用的语音文本]",
        "",
        "## 视频画面文字（OCR）",
        "",
    ]
    if item["repeated_visual_overlay"]:
        lines.extend(
            [
                "固定画面文字（从逐帧结果中去重）：" + "；".join(item["repeated_visual_overlay"]),
                "",
            ]
        )
    if item["visual_content"]:
        for frame in item["visual_content"]:
            lines.extend([f"### {frame['timestamp']}", "", frame["text"], ""])
    else:
        lines.extend(["[没有提取到满足置信度条件的画面文字]", ""])
    flagged = [segment for segment in item["segments"] if segment.get("review_flags")]
    lines.extend(["## 校验记录", ""])
    if flagged:
        for segment in flagged:
            flags = "、".join(segment["review_flags"])
            lines.extend(
                [
                    f"- `{format_timestamp(segment['start'])}`—`{format_timestamp(segment['end'])}`：{flags}",
                    f"  - 采用：{segment['text']}",
                    f"  - Whisper：{segment.get('whisper_text') or '—'}",
                    f"  - SenseVoice：{segment.get('sensevoice_text') or '—'}",
                    f"  - OCR 字幕：{segment.get('ocr_text') or '—'}",
                ]
            )
    else:
        lines.append("没有自动标记的差异片段。")
    lines.append("")
    return "\n".join(lines)


def run_normalize(media: list[Path]) -> None:
    items: list[dict[str, Any]] = []
    for sequence, source in enumerate(media, 1):
        if stop_requested:
            break
        print(f"规范化 [{sequence}/{len(media)}] {relative_key(source)}", flush=True)
        item = build_normalized_item(source)
        items.append(item)
        atomic_write_json(derived_path(source, NORMALIZED_DIR / "逐视频", ".json"), item)
        atomic_write_text(derived_path(source, NORMALIZED_DIR / "逐视频", ".md"), markdown_for_item(item))

    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
    atomic_write_text(NORMALIZED_DIR / "全部内容.jsonl", jsonl)

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(
        ["序号", "文件", "标题", "时长秒", "Whisper", "SenseVoice", "OCR", "双模型平均一致度", "待复核片段数", "OCR关键帧数", "校验状态"]
    )
    for index, item in enumerate(items, 1):
        writer.writerow(
            [
                index,
                item["source"],
                item["title"],
                item["duration_seconds"],
                "是" if item["coverage"]["whisper"] else "否",
                "是" if item["coverage"]["sensevoice"] else "否",
                "是" if item["coverage"]["ocr"] else "否",
                item["quality"]["mean_model_agreement"],
                item["quality"]["flagged_segment_count"],
                item["quality"]["ocr_keyframe_count"],
                item["review_status"],
            ]
        )
    atomic_write_text(NORMALIZED_DIR / "内容索引.csv", csv_buffer.getvalue(), encoding="utf-8-sig")

    review_buffer = io.StringIO()
    review_writer = csv.writer(review_buffer)
    review_writer.writerow(["文件", "开始", "结束", "标记", "采用来源", "采用文本", "Whisper", "SenseVoice", "OCR字幕", "一致度"])
    for item in items:
        for segment in item["segments"]:
            if not segment.get("review_flags"):
                continue
            review_writer.writerow(
                [
                    item["source"],
                    format_timestamp(segment["start"]),
                    format_timestamp(segment["end"]),
                    "；".join(segment["review_flags"]),
                    segment["selected_source"],
                    segment["text"],
                    segment.get("whisper_text", ""),
                    segment.get("sensevoice_text", ""),
                    segment.get("ocr_text", ""),
                    segment.get("agreement"),
                ]
            )
    atomic_write_text(NORMALIZED_DIR / "待复核片段.csv", review_buffer.getvalue(), encoding="utf-8-sig")

    with_both = [item for item in items if item["coverage"]["whisper"] and item["coverage"]["sensevoice"]]
    with_ocr = [item for item in items if item["coverage"]["ocr"]]
    all_agreements = [
        segment["agreement"]
        for item in items
        for segment in item["segments"]
        if segment.get("agreement") is not None
    ]
    all_segments = [segment for item in items for segment in item["segments"]]
    large_difference_count = sum("两模型差异较大" in segment.get("review_flags", []) for segment in all_segments)
    single_model_segment_count = sum("缺少 Whisper 对照" in segment.get("review_flags", []) for segment in all_segments)
    dual_model_flagged_count = sum(
        bool(segment.get("review_flags"))
        for item in items
        if item["coverage"]["whisper"] and item["coverage"]["sensevoice"]
        for segment in item["segments"]
    )
    ocr_assisted_count = sum(segment.get("selected_source") == "OCR字幕" for segment in all_segments)
    sensevoice_selected_count = sum(segment.get("selected_source") == "SenseVoice" for segment in all_segments)
    dual_model_sensevoice_selected_count = sum(
        segment.get("selected_source") == "SenseVoice"
        for item in items
        if item["coverage"]["whisper"] and item["coverage"]["sensevoice"]
        for segment in item["segments"]
    )
    correction_count = sum(len(segment.get("corrections") or []) for segment in all_segments)
    prompt_hallucination_count = sum("Whisper 疑似复述提示词" in segment.get("review_flags", []) for segment in all_segments)
    discarded_prompt_count = sum("已丢弃无对照的提示词幻觉" in segment.get("review_flags", []) for segment in all_segments)
    report_lines = [
        "# 多模态转写与校验报告",
        "",
        f"> 生成时间：{now_text()}",
        "",
        "## 覆盖情况",
        "",
        f"- 视频/音频总数：{len(items)}",
        f"- Whisper 有结果：{sum(item['coverage']['whisper'] for item in items)}",
        f"- SenseVoice 有结果：{sum(item['coverage']['sensevoice'] for item in items)}",
        f"- 已完成双模型对照：{len(with_both)}",
        f"- 已完成 OCR：{len(with_ocr)}",
        f"- OCR 文字关键帧：{sum(item['quality']['ocr_keyframe_count'] for item in items)}",
        "",
        "## 自动校验结论",
        "",
        f"- 已比较语音片段：{len(all_agreements)}",
        f"- 平均字符一致度：{sum(all_agreements) / len(all_agreements):.4f}" if all_agreements else "- 平均字符一致度：—",
        f"- 被标记片段：{sum(item['quality']['flagged_segment_count'] for item in items)}",
        f"  - 缺少 Whisper 对照的单模型片段：{single_model_segment_count}",
        f"  - 双模型中含任一复核标记：{dual_model_flagged_count}（其中差异较大 {large_difference_count}）",
        f"- 自动采用 SenseVoice：{sensevoice_selected_count} 个片段",
        f"  - 其中双模型择优替换：{dual_model_sensevoice_selected_count} 个；其余为仅有 SenseVoice 的片段",
        f"- 自动采用同时间点 OCR 字幕：{ocr_assisted_count} 个片段",
        f"- 应用高把握词语校正：{correction_count} 处",
        f"- 识别并替换 Whisper 提示词幻觉：{prompt_hallucination_count} 个片段",
        f"  - 其中因无有效对照而丢弃：{discarded_prompt_count} 个片段",
        "- 规范化文本以带时间戳、带置信度的 Whisper 分段为主；Whisper 低置信度、重复或提示词幻觉时，采用 SenseVoice 候选。",
        "- OCR 画面事实层独立保存；只有同时间点高置信字幕通过相似度、长度和单次使用约束时，才用于语音正文校正。",
        "- 所有低一致性片段均保留 Whisper、SenseVoice、OCR 候选和时间戳，便于回听/看图复核；自动校对不等同于人工事实核验。",
        "",
        "## 输出说明",
        "",
        "- `全部内容.jsonl`：逐视频完整结构化记录，适合程序、检索或后续模型使用。",
        "- `内容索引.csv`：视频级索引与质量概览，可直接用表格软件打开。",
        "- `待复核片段.csv`：模型分歧和低置信度片段的集中清单。",
        "- `逐视频/`：每个视频各有一份 JSON 和一份便于阅读的 Markdown。",
        "",
    ]
    atomic_write_text(NORMALIZED_DIR / "校验报告.md", "\n".join(report_lines))
    print(f"规范化输出：{NORMALIZED_DIR}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="第二模型 ASR、视频 OCR 与跨模型规范化校验")
    parser.add_argument("--mode", choices=("asr", "ocr", "normalize", "all"), default="all")
    parser.add_argument("--match", default="", help="只处理相对路径中包含此文字的媒体")
    parser.add_argument("--limit", type=int, default=0, help="ASR/OCR 本次最多处理多少个待处理文件；0 为全部")
    parser.add_argument("--force", action="store_true", help="覆盖已经完成的第二模型或 OCR 结果")
    parser.add_argument("--asr-threads", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--asr-chunk-seconds", type=float, default=22.0)
    parser.add_argument("--ocr-interval", type=float, default=30.0, help="有音轨视频的抽帧间隔")
    parser.add_argument("--ocr-no-audio-interval", type=float, default=10.0, help="无音轨视频的抽帧间隔")
    parser.add_argument("--ocr-single-asr-interval", type=float, default=15.0, help="缺少 Whisper 对照时的抽帧间隔")
    parser.add_argument("--ocr-min-confidence", type=float, default=0.62)
    parser.add_argument("--ocr-max-dimension", type=int, default=1600)
    parser.add_argument("--ocr-visual-change", type=float, default=0.004)
    parser.add_argument("--ocr-text-similarity", type=float, default=0.94)
    parser.add_argument("--ocr-review-frame-limit", type=int, default=60, help="每个视频为低一致性语音片段补抽的帧数上限")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not DATA_DIR.is_dir():
        raise SystemExit(f"目录不存在：{DATA_DIR}")
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    media = list_media(args.match)
    if not media:
        raise SystemExit("没有找到符合条件的媒体文件")
    if args.mode in {"asr", "all"}:
        run_asr(media, args)
    if not stop_requested and args.mode in {"ocr", "all"}:
        run_ocr(media, args)
    if not stop_requested and args.mode in {"normalize", "all"}:
        run_normalize(media)


if __name__ == "__main__":
    main()
