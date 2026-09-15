#!/usr/bin/env python3
"""Second, local-only review of ambiguous blank MiMo ASR intervals.

This tool never edits or replaces MiMo output.  It independently reviews every
blank MiMo interval recorded by ``audit_blank_asr_segments.py``--including the
SenseVoice ``speech_detected`` and ``no_speech`` cases--so cross-model counts
are based on one complete common set.  The result is audit/triage evidence used
only to decide which intervals might be re-segmented and sent to MiMo again.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_INPUT = ROOT / ".work" / "first-principles-2026" / "blank_asr_review.json"
DEFAULT_OUTPUT = ROOT / ".work" / "first-principles-2026" / "blank_asr_whisper_review.json"
DEFAULT_MODEL = ROOT / "models" / "faster-whisper-small"
SAMPLE_RATE = 16_000
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON顶层必须是对象：{path}")
    return value


def input_asr_corpus_sha256(document: dict[str, Any]) -> str:
    value = document.get("asr_corpus_sha256")
    nested = document.get("inputs", {}).get("asr_corpus_sha256")
    if (not isinstance(value, str) or SHA_RE.fullmatch(value) is None
            or nested != value):
        raise ValueError("SenseVoice 审计缺少一致的当前 ASR 语料绑定")
    return value


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


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def meaningful_characters(value: str) -> int:
    return len(re.findall(r"[0-9A-Za-z\u3400-\u9fff]", value))


def classify(text: str, segments: list[dict[str, Any]]) -> str:
    """Conservative triage; ambiguity remains manual and never changes ASR."""
    if not segments:
        return "no_speech"
    characters = meaningful_characters(text)
    speech_like = [
        item for item in segments
        if float(item["no_speech_probability"]) < 0.65
        and float(item["average_log_probability"]) > -1.35
    ]
    if characters >= 4 and speech_like:
        return "speech_detected"
    if characters == 0 and all(float(item["no_speech_probability"]) >= 0.65 for item in segments):
        return "no_speech"
    return "needs_manual"


def decode_interval(path: Path, start: int, end: int) -> np.ndarray:
    from media_pipeline.asr import _decode_audio

    samples, metadata = _decode_audio(path)
    if metadata.get("sample_rate") != SAMPLE_RATE or metadata.get("channels") != 1:
        raise ValueError(f"不是16kHz单声道解码结果：{path}")
    if start < 0 or end <= start or end > len(samples):
        raise ValueError(f"MiMo样本区间越界：{path}: {start}-{end}/{len(samples)}")
    return samples[start:end].astype(np.float32) / 32768.0


def run(
    input_path: Path,
    output_path: Path,
    model_path: Path,
    *,
    threads: int,
    model: Any | None = None,
    decoder: Callable[[Path], tuple[np.ndarray, dict[str, Any]]] | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    source = read_json(input_path)
    asr_corpus_sha256 = input_asr_corpus_sha256(source)
    candidates = source.get("reviews")
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise ValueError("SenseVoice 审计 reviews 必须是完整数组")
    if not candidates:
        raise ValueError("SenseVoice 审计没有 MiMo 空白段可复核")
    if model is None:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(model_path), device="cpu", compute_type="int8", cpu_threads=max(1, threads),
        )
    if decoder is None:
        from media_pipeline.asr import _decode_audio

        decoder = _decode_audio
    results: list[dict[str, Any]] = []
    decoded_cache: dict[Path, np.ndarray] = {}
    for index, item in enumerate(candidates, 1):
        path = Path(item["source_path"])
        path = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
        if path not in decoded_cache:
            decoded, metadata = decoder(path)
            if metadata.get("sample_rate") != SAMPLE_RATE or metadata.get("channels") != 1:
                raise ValueError(f"不是16kHz单声道解码结果：{path}")
            decoded_cache[path] = decoded
        start, end = int(item["sample_start"]), int(item["sample_end"])
        decoded = decoded_cache[path]
        if start < 0 or end <= start or end > len(decoded):
            raise ValueError(f"MiMo样本区间越界：{path}: {start}-{end}/{len(decoded)}")
        interval = decoded[start:end].astype(np.float32) / 32768.0
        generated, info = model.transcribe(
            interval,
            language="zh",
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            word_timestamps=False,
        )
        segments = []
        text_parts = []
        for segment in generated:
            text = clean_text(segment.text)
            if text:
                text_parts.append(text)
            segments.append({
                "start_seconds_relative": round(float(segment.start), 3),
                "end_seconds_relative": round(float(segment.end), 3),
                "text": text,
                "average_log_probability": round(float(segment.avg_logprob), 6),
                "no_speech_probability": round(float(segment.no_speech_prob), 6),
                "compression_ratio": round(float(segment.compression_ratio), 6),
            })
        text = " ".join(text_parts).strip()
        judgment = classify(text, segments)
        results.append({
            "review_id": item["review_id"],
            "asset_id": item["asset_id"],
            "asset_sha256": item["asset_sha256"],
            "source_sha256": item["source_sha256"],
            "source_path": item["source_path"],
            "sample_start": start,
            "sample_end": end,
            "sensevoice_text": item.get("local_review", {}).get("text", ""),
            "sensevoice_judgment": item.get("judgment"),
            "whisper_review": {
                "engine": "faster-whisper",
                "model": model_path.name,
                "provenance": "local_audit_only_not_mimo",
                "language": getattr(info, "language", "zh"),
                "language_probability": round(float(getattr(info, "language_probability", 0.0)), 6),
                "text": text,
                "segments": segments,
            },
            "whisper_judgment": judgment,
            # Compatibility alias consumed by the generic re-run queue loader;
            # it always denotes Whisper's independent judgment in this file.
            "judgment": judgment,
            "mimo_action": "resegment_and_rerun" if judgment == "speech_detected" else "none",
        })
        progress(f"Whisper复核 [{index:02d}/{len(candidates):02d}] {judgment} {path.name}")

    sensevoice_counts = Counter(item["sensevoice_judgment"] for item in results)
    whisper_counts = Counter(item["whisper_judgment"] for item in results)
    cross_counts = Counter(
        (item["sensevoice_judgment"], item["whisper_judgment"]) for item in results
    )
    queue = [
        {
            "review_id": item["review_id"],
            "asset_id": item["asset_id"],
            "asset_sha256": item["asset_sha256"],
            "source_sha256": item["source_sha256"],
            "source_path": item["source_path"],
            "sample_start": item["sample_start"],
            "sample_end": item["sample_end"],
            "sensevoice_judgment": item["sensevoice_judgment"],
            "whisper_judgment": item["whisper_judgment"],
            "required_action": "MiMo re-segmentation and re-run; do not substitute local text",
        }
        for item in results if item["whisper_judgment"] == "speech_detected"
    ]
    result = {
        "schema_version": "blank-asr-whisper-review/v1",
        "asr_corpus_sha256": asr_corpus_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {
            "local_review_only": True,
            "mimo_asr_immutable": True,
            "local_text_must_not_enter_asr_section": True,
            "input_scope": "all_blank_mimo_segments_without_sensevoice_prefilter",
        },
        "input_review": str(input_path.resolve()),
        "model": str(model_path.resolve()),
        "summary": {
            "input_blank_segment_count": len(candidates),
            "joint_reviewed_blank_segment_count": len(results),
            "sensevoice_judgments": {
                key: sensevoice_counts.get(key, 0)
                for key in ("no_speech", "speech_detected", "needs_manual")
            },
            "whisper_judgments": {
                key: whisper_counts.get(key, 0)
                for key in ("no_speech", "speech_detected", "needs_manual")
            },
            "cross_judgments": {
                f"sensevoice={sensevoice}|whisper={whisper}": count
                for (sensevoice, whisper), count in sorted(cross_counts.items())
            },
            "additional_mimo_rerun_count": len(queue),
        },
        "reviews": results,
        "additional_mimo_resegmentation_rerun_queue": queue,
    }
    atomic_json(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4))
    args = parser.parse_args()
    result = run(
        args.input, args.output, args.model, threads=args.threads,
        progress=lambda message: print(message, flush=True),
    )
    print(json.dumps({"output": str(args.output.resolve()), **result["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
