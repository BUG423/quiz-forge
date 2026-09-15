#!/usr/bin/env python3
"""Audit blank MiMo ASR segments with a local-only SenseVoice pass.

This program never edits MiMo ``asr.json`` files.  SenseVoice output is audit
evidence only: it is kept in a separate review document and is never labelled
as MiMo text.  Only locally detected speech is placed in the MiMo
re-segmentation/re-run queue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ASR_ROOT = ROOT / ".work" / "single-video"
DEFAULT_ASSET_MANIFEST = ROOT / ".work" / "first-principles-2026" / "asset_manifest.json"
DEFAULT_EMBEDDED_MANIFEST = ROOT / ".work" / "embedded-office" / "manifest.json"
DEFAULT_OUTPUT = ROOT / ".work" / "first-principles-2026" / "blank_asr_review.json"
MODEL_DIR = ROOT / "models" / "sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17"
SAMPLE_RATE = 16_000
MIMO_MODEL = "mimo-v2.5-asr"
MIMO_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


class AuditError(RuntimeError):
    """An input is incomplete or fails a provenance check."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def asr_corpus_binding(
    records: Iterable[tuple[Path, dict[str, Any]]],
) -> tuple[str, list[dict[str, str]]]:
    """Bind the audit to the canonical content of every current ASR document."""
    entries = sorted(({
        "source_sha256": str(value.get("source_sha256", "")),
        "asr_sha256": canonical_sha256(value),
    } for _, value in records), key=lambda item: item["source_sha256"])
    if (not entries or any(SHA_RE.fullmatch(item["source_sha256"]) is None for item in entries)
            or len({item["source_sha256"] for item in entries}) != len(entries)):
        raise AuditError("无法建立唯一的 MiMo ASR 语料绑定。")
    return canonical_sha256(entries), entries


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clean_sensevoice_text(text: Any) -> str:
    """Remove SenseVoice control tags without otherwise rewriting its words."""
    value = re.sub(r"<\|[^|]*\|>", "", str(text or ""))
    value = value.replace("▁", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def meaningful_character_count(text: str) -> int:
    return len(re.findall(r"[0-9A-Za-z\u3400-\u9fff]", text))


def classify_review(local_text: str, rms: float, *, silence_rms: float) -> str:
    """Conservatively classify local evidence; ambiguity stays manual."""
    characters = meaningful_character_count(local_text)
    # A one- or two-character decode from a long music/noise interval is weak
    # evidence and can be a local-model hallucination.  Keep it for manual
    # review; require at least four meaningful characters before queuing MiMo.
    if characters >= 4 and rms > silence_rms:
        return "speech_detected"
    if characters == 0 and rms <= silence_rms:
        return "no_speech"
    return "needs_manual"


def split_exact(sample_count: int, maximum_samples: int) -> list[tuple[int, int]]:
    """Partition an interval exactly once, with neither gaps nor overlap."""
    if sample_count < 0 or maximum_samples <= 0:
        raise ValueError("sample counts must be non-negative and the maximum positive")
    return [
        (start, min(sample_count, start + maximum_samples))
        for start in range(0, sample_count, maximum_samples)
    ]


class SenseVoiceReviewer:
    engine = "sherpa-onnx"

    def __init__(self, model_dir: Path = MODEL_DIR, *, num_threads: int = 4) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise AuditError("缺少 sherpa-onnx，无法运行本地 SenseVoice 复核。") from exc
        model = model_dir / "model.int8.onnx"
        tokens = model_dir / "tokens.txt"
        if not model.is_file() or not tokens.is_file():
            raise AuditError(f"SenseVoice 模型不完整：{model_dir}")
        self.model_id = model_dir.name
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            use_itn=True,
            num_threads=max(1, num_threads),
            debug=False,
        )

    def recognize(self, samples: np.ndarray, sample_rate: int) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples.astype(np.float32, copy=False))
        self._recognizer.decode_stream(stream)
        try:
            result = json.loads(str(stream.result))
        except json.JSONDecodeError as exc:
            raise AuditError("SenseVoice 返回了无法解析的本地结果。") from exc
        return clean_sensevoice_text(result.get("text", ""))


def default_decoder(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    # Reuse the exact timestamp-aware 16 kHz mono decoder that generated the
    # sample_start/sample_end coordinates in the verified MiMo ASR files.
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from media_pipeline.asr import _decode_audio

    return _decode_audio(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"无法读取 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"JSON 顶层必须是对象：{path}")
    return value


def _resolved_input_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def load_sources(
    root: Path,
    asset_manifest_path: Path,
    embedded_manifest_path: Path,
) -> dict[str, dict[str, Any]]:
    """Resolve the 63 course videos and four embedded-media sources by SHA."""
    asset_manifest = _read_json(asset_manifest_path)
    sources: dict[str, dict[str, Any]] = {}
    assets_by_path: dict[Path, dict[str, Any]] = {}

    for asset in asset_manifest.get("assets", []):
        source_path = _resolved_input_path(asset["source_path"], root)
        assets_by_path[source_path] = asset
        if asset.get("kind") != "video":
            continue
        source_sha = str(asset["source_sha256"])
        if source_sha in sources:
            raise AuditError(f"重复视频 SHA：{source_sha}")
        sources[source_sha] = {
            "asset_id": asset["asset_id"],
            "asset_sha256": source_sha,
            "source_sha256": source_sha,
            "source_path": source_path,
            "source_role": "course_video",
            "parent_source_path": None,
        }

    embedded_manifest = _read_json(embedded_manifest_path)
    for item in embedded_manifest.get("files", []):
        media_path = _resolved_input_path(item["output_path"], root)
        parent_path = _resolved_input_path(item["parent_source"], root)
        parent_asset = assets_by_path.get(parent_path)
        if parent_asset is None:
            raise AuditError(f"内嵌媒体的父课程资产不在权威清单中：{parent_path}")
        source_sha = str(item["sha256"])
        if source_sha in sources:
            raise AuditError(f"重复媒体 SHA：{source_sha}")
        sources[source_sha] = {
            "asset_id": parent_asset["asset_id"],
            "asset_sha256": parent_asset["source_sha256"],
            "source_sha256": source_sha,
            "source_path": media_path,
            "source_role": "embedded_media",
            "parent_source_path": parent_path,
            "embedded_slide": item.get("slide"),
            "embedded_member": item.get("member"),
        }
    return sources


def load_verified_asr(asr_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    seen_sha: set[str] = set()
    for path in sorted(asr_root.glob("*/asr.json")):
        value = _read_json(path)
        source_sha = str(value.get("source_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            raise AuditError(f"MiMo ASR 缺少有效 source_sha256：{path}")
        if source_sha in seen_sha:
            raise AuditError(f"同一媒体存在多份顶层 MiMo ASR：{source_sha}")
        if path.parent.name != source_sha[:16]:
            raise AuditError(f"MiMo ASR 路径与 source_sha256 不一致：{path}")
        if value.get("model") != MIMO_MODEL:
            raise AuditError(f"不是已核验的 MiMo 模型结果：{path}")
        no_audio = value.get("has_audio") is False and value.get("skipped_reason") == "no_audio_stream"
        if not no_audio and value.get("configuration", {}).get("endpoint") != MIMO_ENDPOINT:
            raise AuditError(f"MiMo ASR 不是 token-plan-cn 官方 endpoint 结果：{path}")
        if value.get("coverage", {}).get("complete") is not True:
            raise AuditError(f"MiMo ASR 覆盖未完成：{path}")
        if not isinstance(value.get("segments"), list):
            raise AuditError(f"MiMo ASR segments 结构无效：{path}")
        seen_sha.add(source_sha)
        records.append((path, value))
    return records


def blank_segments(asr_records: Iterable[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    blanks: list[dict[str, Any]] = []
    for path, value in asr_records:
        previous_end = 0
        for ordinal, segment in enumerate(value["segments"]):
            start = segment.get("sample_start")
            end = segment.get("sample_end")
            if not isinstance(start, int) or not isinstance(end, int) or start < previous_end or end <= start:
                raise AuditError(f"MiMo 分段样本区间无效：{path} segment {ordinal}")
            previous_end = end
            if not str(segment.get("text", "")).strip():
                blanks.append({
                    "asr_path": path,
                    "asr": value,
                    "segment": segment,
                    "segment_ordinal": ordinal,
                })
    return blanks


def transcribe_exact_interval(
    samples: np.ndarray,
    source_start: int,
    reviewer: Any,
    *,
    local_chunk_seconds: float,
) -> tuple[str, list[dict[str, Any]]]:
    if not math.isfinite(local_chunk_seconds) or local_chunk_seconds <= 0:
        raise ValueError("local_chunk_seconds must be a finite positive value")
    maximum = max(1, round(local_chunk_seconds * SAMPLE_RATE))
    parts: list[str] = []
    chunks: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(split_exact(len(samples), maximum)):
        text = clean_sensevoice_text(reviewer.recognize(samples[start:end], SAMPLE_RATE))
        if text:
            parts.append(text)
        chunks.append({
            "index": index,
            "source_sample_start": source_start + start,
            "source_sample_end": source_start + end,
            "text": text,
        })
    return " ".join(parts).strip(), chunks


def build_review(
    *,
    root: Path = ROOT,
    asr_root: Path = DEFAULT_ASR_ROOT,
    asset_manifest_path: Path = DEFAULT_ASSET_MANIFEST,
    embedded_manifest_path: Path = DEFAULT_EMBEDDED_MANIFEST,
    output_path: Path | None = DEFAULT_OUTPUT,
    reviewer: Any | None = None,
    decoder: Callable[[Path], tuple[np.ndarray, dict[str, Any]]] = default_decoder,
    num_threads: int = 4,
    local_chunk_seconds: float = 20.0,
    silence_rms: float = 0.001,
    expected_asr_files: int | None = None,
    expected_blank_segments: int | None = None,
    expected_course_videos: int | None = None,
    expected_embedded_media: int | None = None,
    show_progress: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    sources = load_sources(root, asset_manifest_path, embedded_manifest_path)
    asr_records = load_verified_asr(asr_root)
    asr_corpus_sha256, asr_documents = asr_corpus_binding(asr_records)
    blanks = blank_segments(asr_records)

    source_counts = Counter(item["source_role"] for item in sources.values())
    assertions = (
        ("MiMo ASR 文件", len(asr_records), expected_asr_files),
        ("MiMo 空文本段", len(blanks), expected_blank_segments),
        ("顶层课程视频", source_counts["course_video"], expected_course_videos),
        ("内嵌媒体", source_counts["embedded_media"], expected_embedded_media),
    )
    for label, actual, expected in assertions:
        if expected is not None and actual != expected:
            raise AuditError(f"{label}计数应为 {expected}，实际为 {actual}")

    asr_by_sha = {value["source_sha256"]: (path, value) for path, value in asr_records}
    if set(asr_by_sha) != set(sources):
        missing_source = sorted(set(asr_by_sha) - set(sources))
        missing_asr = sorted(set(sources) - set(asr_by_sha))
        raise AuditError(f"MiMo/媒体映射不完整：无媒体={missing_source}；无ASR={missing_asr}")

    # Check the actual media bytes before trusting any persisted source SHA.
    for index, (source_sha, source) in enumerate(sorted(sources.items()), 1):
        media_path = source["source_path"]
        if not media_path.is_file():
            raise AuditError(f"媒体不存在：{media_path}")
        if sha256_file(media_path) != source_sha:
            raise AuditError(f"媒体 SHA 与 MiMo ASR 不一致：{media_path}")
        if show_progress:
            print(f"SHA [{index:02d}/{len(sources):02d}] {relative_path(media_path, root)}", flush=True)

    if reviewer is None:
        reviewer = SenseVoiceReviewer(num_threads=num_threads)
    reviewer_model = getattr(reviewer, "model_id", "test-or-injected-reviewer")
    reviewer_engine = getattr(reviewer, "engine", "injected-local-reviewer")

    blanks_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for blank in blanks:
        blanks_by_sha[blank["asr"]["source_sha256"]].append(blank)

    reviews: list[dict[str, Any]] = []
    affected = sorted(blanks_by_sha)
    for source_number, source_sha in enumerate(affected, 1):
        source = sources[source_sha]
        media_path = source["source_path"]
        decoded, metadata = decoder(media_path)
        if metadata.get("sample_rate") != SAMPLE_RATE or metadata.get("channels") != 1:
            raise AuditError(f"解码结果不是 16 kHz 单声道：{media_path}")
        expected_samples = asr_by_sha[source_sha][1].get("audio", {}).get("sample_count")
        if expected_samples != len(decoded):
            raise AuditError(
                f"重新解码样本数与 MiMo 坐标系不一致：{media_path} "
                f"expected={expected_samples} actual={len(decoded)}"
            )
        if show_progress:
            print(
                f"SenseVoice [{source_number:02d}/{len(affected):02d}] "
                f"{relative_path(media_path, root)}",
                flush=True,
            )
        for blank in blanks_by_sha[source_sha]:
            segment = blank["segment"]
            start, end = segment["sample_start"], segment["sample_end"]
            if end > len(decoded):
                raise AuditError(f"空段越过已解码音频末尾：{media_path} segment {segment.get('index')}")
            exact_int16 = decoded[start:end].astype(np.int16, copy=False)
            exact = exact_int16.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(np.square(exact), dtype=np.float64))) if exact.size else 0.0
            peak = float(np.max(np.abs(exact))) if exact.size else 0.0
            local_text, local_chunks = transcribe_exact_interval(
                exact,
                start,
                reviewer,
                local_chunk_seconds=local_chunk_seconds,
            )
            judgment = classify_review(local_text, rms, silence_rms=silence_rms)
            asr_path = blank["asr_path"]
            review_id = f"{source_sha[:16]}:{segment.get('index', blank['segment_ordinal'])}:{start}-{end}"
            reviews.append({
                "review_id": review_id,
                "asset_id": source["asset_id"],
                "asset_sha256": source["asset_sha256"],
                "source_sha256": source_sha,
                "source_path": relative_path(media_path, root),
                "source_role": source["source_role"],
                "parent_source_path": (
                    relative_path(source["parent_source_path"], root)
                    if source.get("parent_source_path") else None
                ),
                "mimo_asr_path": relative_path(asr_path, root),
                "mimo_model": blank["asr"]["model"],
                "mimo_segment_index": segment.get("index", blank["segment_ordinal"]),
                "mimo_text": "",
                "sample_rate": SAMPLE_RATE,
                "sample_start": start,
                "sample_end": end,
                "sample_count": end - start,
                "start_seconds": round(start / SAMPLE_RATE, 6),
                "end_seconds": round(end / SAMPLE_RATE, 6),
                "mimo_start_seconds": segment.get("start_seconds"),
                "mimo_end_seconds": segment.get("end_seconds"),
                "rms": round(rms, 8),
                "peak": round(peak, 8),
                "local_review": {
                    "engine": reviewer_engine,
                    "model": reviewer_model,
                    "provenance": "local_sensevoice_audit_only_not_mimo",
                    "text": local_text,
                    "chunks": local_chunks,
                },
                "judgment": judgment,
                "mimo_action": "resegment_and_rerun" if judgment == "speech_detected" else "none",
            })

    reviews.sort(key=lambda item: (item["source_sha256"], item["sample_start"], item["sample_end"]))
    queue = [
        {
            "review_id": item["review_id"],
            "asset_id": item["asset_id"],
            "asset_sha256": item["asset_sha256"],
            "source_sha256": item["source_sha256"],
            "source_path": item["source_path"],
            "sample_start": item["sample_start"],
            "sample_end": item["sample_end"],
            "judgment": "speech_detected",
            "reason": "local_sensevoice_speech_detected_in_blank_mimo_segment",
            "required_action": "MiMo re-segmentation and re-run; do not substitute local text",
        }
        for item in reviews
        if item["judgment"] == "speech_detected"
    ]
    decisions = Counter(item["judgment"] for item in reviews)
    result = {
        "schema_version": "blank-asr-review/v1",
        "asr_corpus_sha256": asr_corpus_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {
            "mimo_asr_immutable": True,
            "local_result_role": "audit_only",
            "local_result_is_mimo": False,
            "rerun_queue_rule": "contains speech_detected reviews only",
        },
        "inputs": {
            "asr_root": relative_path(asr_root, root),
            "asset_manifest": relative_path(asset_manifest_path, root),
            "embedded_manifest": relative_path(embedded_manifest_path, root),
            "asr_file_count": len(asr_records),
            "asr_corpus_sha256": asr_corpus_sha256,
            "asr_documents": asr_documents,
            "course_video_count": source_counts["course_video"],
            "embedded_media_count": source_counts["embedded_media"],
            "local_engine": reviewer_engine,
            "local_model": reviewer_model,
            "local_chunk_seconds": local_chunk_seconds,
            "silence_rms_threshold": silence_rms,
        },
        "summary": {
            "blank_segment_count": len(reviews),
            "sources_with_blank_segments": len(affected),
            "judgments": {key: decisions.get(key, 0) for key in (
                "no_speech", "speech_detected", "needs_manual"
            )},
            "mimo_resegmentation_rerun_count": len(queue),
        },
        "reviews": reviews,
        "mimo_resegmentation_rerun_queue": queue,
    }
    if len(reviews) != len(blanks):
        raise AssertionError("Every blank MiMo segment must produce exactly one review")
    if any(item["judgment"] != "speech_detected" for item in queue):
        raise AssertionError("MiMo re-run queue may contain speech_detected items only")
    if output_path is not None:
        atomic_json(output_path, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--asr-root", type=Path, default=DEFAULT_ASR_ROOT)
    parser.add_argument("--asset-manifest", type=Path, default=DEFAULT_ASSET_MANIFEST)
    parser.add_argument("--embedded-manifest", type=Path, default=DEFAULT_EMBEDDED_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--local-chunk-seconds", type=float, default=20.0)
    parser.add_argument("--silence-rms", type=float, default=0.001)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_review(
        root=args.root,
        asr_root=args.asr_root,
        asset_manifest_path=args.asset_manifest,
        embedded_manifest_path=args.embedded_manifest,
        output_path=args.output,
        num_threads=args.threads,
        local_chunk_seconds=args.local_chunk_seconds,
        silence_rms=args.silence_rms,
        expected_asr_files=67,
        # A legitimate MiMo re-run may change the number of empty responses.
        # Audit every blank interval in the current 67-file corpus instead of
        # binding admission to the legacy count of 40.
        expected_blank_segments=None,
        expected_course_videos=63,
        expected_embedded_media=4,
        show_progress=True,
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        **result["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
