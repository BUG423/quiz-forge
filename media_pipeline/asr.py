"""MiMo ASR adapter: timestamped decoding, continuous chunks and resumable calls.

Only normalized audio is sent to the fixed official endpoint. API credentials,
request bodies and server error bodies are never logged or written to disk.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import wave
from pathlib import Path
from typing import Any, Callable

import av
import numpy as np

MODEL = "mimo-v2.5-asr"
ENDPOINT = "https://api.xiaomimimo.com/v1/chat/completions"
ALLOWED_ENDPOINTS = frozenset({
    ENDPOINT,
    "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
    "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions",
    "https://token-plan-ams.xiaomimimo.com/v1/chat/completions",
})
SAMPLE_RATE = 16_000
CACHE_VERSION = 1


class ASRError(RuntimeError):
    """Safe-to-display ASR failure without request or credential contents."""


class ContentFilterError(ASRError):
    """A segment must be retried in shorter, independently validated pieces."""


class NoAudioError(ASRError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials/audio to a redirected address.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _configured_endpoint(value: str | None = None) -> str:
    """Resolve only an official MiMo OpenAI-compatible ASR endpoint."""
    candidate = (value or os.environ.get("MIMO_API_BASE_URL") or ENDPOINT).strip().rstrip("/")
    if candidate.endswith("/v1"):
        candidate += "/chat/completions"
    parsed = urllib.parse.urlsplit(candidate)
    if (candidate not in ALLOWED_ENDPOINTS or parsed.scheme != "https"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ASRError("MiMo ASR 接口地址不在允许的小米官方地址清单中。")
    return candidate


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wav_bytes(samples: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples.astype("<i2", copy=False).tobytes())
    return buffer.getvalue()


def _trim_timestamp_overlap(values: np.ndarray, delta: int) -> tuple[np.ndarray, int]:
    """Remove only samples whose timestamps are already covered."""
    if delta >= -2:
        return values, 0
    overlap = -delta
    if overlap > len(values) or overlap > SAMPLE_RATE:
        raise ASRError("音频时间戳发生无法安全裁切的重叠或倒退。")
    return values[overlap:], overlap


def _decode_audio(source: Path) -> tuple[np.ndarray, dict]:
    """Decode all audio samples, retaining the source's zero-based media timeline."""
    arrays: list[np.ndarray] = []
    total = decoded_samples = inserted_samples = 0
    start: float | None = None
    gaps: list[dict] = []
    overlaps: list[dict] = []
    trimmed_overlap_samples = 0
    with av.open(str(source)) as container:
        streams = list(container.streams.audio)
        if not streams:
            raise NoAudioError("源文件没有音轨，不能运行 ASR。")
        stream = streams[0]
        origin = float(container.start_time / av.time_base) if container.start_time is not None else 0.0
        media_duration = float(container.duration / av.time_base) if container.duration else None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        fallback = float(stream.start_time * stream.time_base) - origin if stream.start_time is not None else 0.0

        def append(frame: av.AudioFrame) -> None:
            nonlocal start, total, decoded_samples, inserted_samples, trimmed_overlap_samples
            values = frame.to_ndarray().reshape(-1).astype(np.int16, copy=False)
            if not values.size:
                return
            timestamp = float(frame.pts * frame.time_base) - origin if frame.pts is not None else None
            if start is None:
                start = timestamp if timestamp is not None else fallback
            if timestamp is not None:
                delta = round((timestamp - start) * SAMPLE_RATE) - total
                if delta < -2:
                    # Some MP4 muxers repeat the final decoded audio frame.  Drop
                    # only the portion whose timestamp is already covered; a jump
                    # beyond this frame still signals an invalid timeline.
                    values, overlap = _trim_timestamp_overlap(values, delta)
                    overlaps.append({"start_seconds": timestamp,
                                     "trimmed_sample_count": overlap})
                    trimmed_overlap_samples += overlap
                    if not values.size:
                        return
                if delta > 2:
                    if delta > SAMPLE_RATE * 60:
                        raise ASRError("音频存在超过 60 秒的时间戳缺口，需检查源文件。")
                    gaps.append({"start_seconds": start + total / SAMPLE_RATE, "sample_count": delta})
                    arrays.append(np.zeros(delta, dtype=np.int16))
                    total += delta
                    inserted_samples += delta
            arrays.append(values)
            total += int(values.size)
            decoded_samples += int(values.size)

        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                append(converted)
        for converted in resampler.resample(None):
            append(converted)
        stream_index = stream.index
        stream_count = len(streams)
    if not arrays or start is None:
        raise NoAudioError("源文件有音轨，但未解码到有效音频样本。")
    samples = np.concatenate(arrays)
    return samples, {
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sample_format": "s16le",
        "sample_count": int(samples.size),
        "decoded_sample_count": decoded_samples,
        "inserted_silence_samples": inserted_samples,
        "timestamp_gaps": gaps,
        "timestamp_overlaps": overlaps,
        "trimmed_overlap_samples": trimmed_overlap_samples,
        "audio_start_seconds": start,
        "audio_end_seconds": start + len(samples) / SAMPLE_RATE,
        "media_duration_seconds": media_duration,
        "timeline_origin_seconds": origin,
        "audio_stream_index": stream_index,
        "audio_stream_count": stream_count,
    }


def _split_audio(samples: np.ndarray, chunk_seconds: float) -> list[tuple[int, int]]:
    """Choose quiet 200 ms windows near each limit; cover each sample exactly once."""
    if not math.isfinite(chunk_seconds) or chunk_seconds < 1:
        raise ValueError("chunk_seconds 必须是大于或等于 1 的有限数值。")
    maximum = int(chunk_seconds * SAMPLE_RATE)
    window = min(int(0.2 * SAMPLE_RATE), maximum // 4)
    step = max(1, int(0.02 * SAMPLE_RATE))
    intervals: list[tuple[int, int]] = []
    begin = 0
    while begin < len(samples):
        end = min(len(samples), begin + maximum)
        if end < len(samples):
            search_begin = max(begin + maximum * 3 // 4, end - 8 * SAMPLE_RATE)
            candidates = np.arange(search_begin, end - window + 1, step)
            if candidates.size:
                region = samples[search_begin:end].astype(np.float64)
                energy = np.concatenate(([0.0], np.cumsum(region * region)))
                offsets = candidates - search_begin
                powers = (energy[offsets + window] - energy[offsets]) / window
                # Prefer the latest equally quiet candidate, avoiding short chunks.
                position = len(powers) - 1 - int(np.argmin(powers[::-1]))
                end = int(candidates[position]) + window // 2
        intervals.append((begin, end))
        begin = end
    return intervals


def _request_json(wav_audio: bytes, api_key: str, *, max_attempts: int = 4,
                  timeout: float = 180, endpoint: str | None = None) -> dict:
    endpoint = _configured_endpoint(endpoint)
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": [{
            "type": "input_audio",
            "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(wav_audio).decode("ascii")},
        }]}],
        "asr_options": {"language": "zh"},
        "stream": False,
    }).encode("utf-8")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for attempt in range(max_attempts):
        request = urllib.request.Request(endpoint, data=body, headers={
            "api-key": api_key, "Content-Type": "application/json",
        }, method="POST")
        delay = min(2 ** attempt, 15)
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                raw = response.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise ASRError("MiMo ASR 响应超过预期大小。")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise ASRError("MiMo ASR 返回的内容不是有效 JSON。") from None
            if not isinstance(result, dict):
                raise ASRError("MiMo ASR 响应结构不正确。")
            return result
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After") if error.headers else None
            error.close()
            if status != 429 and not 500 <= status <= 599:
                hint = "鉴权失败，请检查 MiMo 密钥及权限。" if status in (401, 403) else "请求被拒绝，不自动重试。"
                raise ASRError(f"MiMo ASR HTTP {status}：{hint}") from None
            if attempt + 1 >= max_attempts:
                raise ASRError(f"MiMo ASR HTTP {status}：已达到 {max_attempts} 次尝试上限。") from None
            if retry_after:
                try:
                    delay = min(30.0, max(delay, float(retry_after)))
                except ValueError:
                    pass
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError):
            if attempt + 1 >= max_attempts:
                raise ASRError(f"MiMo ASR 网络请求失败，已达到 {max_attempts} 次尝试上限。") from None
        time.sleep(delay)
    raise AssertionError("unreachable")


def _validate_response(response: dict) -> dict:
    if not isinstance(response, dict):
        raise ASRError("MiMo ASR 响应结构不正确。")
    choices = response.get("choices")
    if response.get("error") or not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ASRError("MiMo ASR 响应缺少唯一有效识别结果。")
    choice = choices[0]
    reason = choice.get("finish_reason")
    if reason != "stop":
        label = reason if reason is None or isinstance(reason, str) and reason in {"length", "content_filter"} else "unknown"
        error = ContentFilterError if label == "content_filter" else ASRError
        raise error(f"MiMo ASR 未完整完成（finish_reason={label}），此段不得计为成功。")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ASRError("MiMo ASR 响应缺少文本内容。")
    if response.get("model") != MODEL:
        raise ASRError("MiMo ASR 响应模型与指定模型不一致。")
    if not isinstance(response.get("id"), str) or not response["id"]:
        raise ASRError("MiMo ASR 响应缺少请求 ID。")
    usage = response.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ASRError("MiMo ASR 用量信息结构不正确。")
    return {"text": message["content"], "finish_reason": reason, "id": response["id"], "usage": usage, "model": MODEL}


def _add_usage(total: dict, addition: dict | None) -> None:
    for key, value in (addition or {}).items():
        if isinstance(value, dict):
            if not isinstance(total.get(key), dict):
                total[key] = {}
            _add_usage(total[key], value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            if not isinstance(total.get(key, 0), (int, float)):
                continue
            total[key] = total.get(key, 0) + value


def _recognize_complete(samples: np.ndarray, api_key: str, endpoint: str, *, depth: int = 0) -> dict:
    """Require complete MiMo output, splitting only content-filtered audio.

    Each child response is independently validated.  This keeps exact audio
    coverage while avoiding treating a filtered 55-second response as text.
    """
    try:
        return _validate_response(_request_json(_wav_bytes(samples), api_key, endpoint=endpoint))
    except ContentFilterError:
        # Political/legal training material can remain filtered even around
        # seven seconds.  Continue to sub-second pieces before declaring the
        # source untranscribable; only the affected branch is split.
        if depth >= 7 or len(samples) < SAMPLE_RATE // 2:
            raise
        split = len(samples) // 2
        left = _recognize_complete(samples[:split], api_key, endpoint, depth=depth + 1)
        right = _recognize_complete(samples[split:], api_key, endpoint, depth=depth + 1)
        usage: dict = {}
        _add_usage(usage, left.get("usage"))
        _add_usage(usage, right.get("usage"))
        request_ids = left.get("request_ids", [left["id"]]) + right.get("request_ids", [right["id"]])
        composite = hashlib.sha256("\n".join(request_ids).encode()).hexdigest()[:24]
        return {
            "text": "\n".join(part for part in (left["text"].strip(), right["text"].strip()) if part),
            "finish_reason": "stop",
            "id": f"split-{composite}",
            "usage": usage,
            "model": MODEL,
            "request_ids": request_ids,
            "content_filter_split": True,
        }


def transcribe(source: Path, work_dir: Path, api_key: str, *, chunk_seconds: float = 55.0,
               progress: Callable[[dict], Any] | None = None,
               endpoint: str | None = None) -> dict:
    """Transcribe one complete file. Resume only independently verified chunks."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ASRError("未提供 MiMo API 密钥。")
    api_key = api_key.strip()
    endpoint = _configured_endpoint(endpoint)
    if not math.isfinite(chunk_seconds) or chunk_seconds < 1:
        raise ValueError("chunk_seconds 必须是大于或等于 1 的有限数值。")
    source, work_dir = Path(source), Path(work_dir)
    fingerprint = _source_hash(source)
    samples, metadata = _decode_audio(source)
    intervals = _split_audio(samples, chunk_seconds)
    configuration = {"cache_version": CACHE_VERSION, "endpoint": endpoint, "model": MODEL,
                     "sample_rate": SAMPLE_RATE, "language": "zh", "chunk_seconds": chunk_seconds,
                     "segmentation": "quiet-200ms-last8s-v1"}
    identity = {"source_sha256": fingerprint, "configuration": configuration,
                "pcm_sha256": hashlib.sha256(samples.astype("<i2", copy=False).tobytes()).hexdigest(),
                "audio_start_seconds": metadata["audio_start_seconds"]}
    cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_dir = work_dir / "asr" / cache_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    audio_path = cache_dir / "audio.wav"
    _atomic_bytes(audio_path, _wav_bytes(samples))
    segments: list[dict] = []
    usage: dict = {}
    hits = 0
    for index, (begin, end) in enumerate(intervals):
        cache_path = cache_dir / f"segment-{index:04d}.json"
        segment_identity = {**identity, "index": index, "sample_start": begin, "sample_end": end}
        recognized = None
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("identity") == segment_identity:
                recognized = _validate_response(cached["response"])
                hits += 1
        except (OSError, ValueError, KeyError, TypeError, ASRError):
            pass
        from_cache = recognized is not None
        if recognized is None:
            recognized = _recognize_complete(samples[begin:end], api_key, endpoint)
            if api_key in json.dumps(recognized, ensure_ascii=False):
                raise ASRError("MiMo ASR 响应异常地包含认证信息，结果已拒绝保存。")
            # Store only schema-selected response fields, never arbitrary echoed data.
            safe_response = {"model": MODEL, "id": recognized["id"], "usage": recognized["usage"],
                             "choices": [{"finish_reason": "stop", "message": {"content": recognized["text"]}}]}
            cache_record = {"identity": segment_identity, "response": safe_response}
            if recognized.get("request_ids"):
                cache_record["request_ids"] = recognized["request_ids"]
                cache_record["content_filter_split"] = True
            _atomic_json(cache_path, cache_record)
        segment = {"index": index, "start_seconds": metadata["audio_start_seconds"] + begin / SAMPLE_RATE,
                   "end_seconds": metadata["audio_start_seconds"] + end / SAMPLE_RATE,
                   "sample_start": begin, "sample_end": end, **recognized}
        segments.append(segment)
        _add_usage(usage, recognized["usage"])
        if progress:
            progress({"stage": "asr", "event": "segment_complete", "completed": index + 1,
                      "total": len(intervals), "cached": from_cache})
    covered = sum(end - begin for begin, end in intervals)
    if (not intervals or intervals[0][0] != 0 or intervals[-1][1] != len(samples)
            or covered != len(samples) or any(left[1] != right[0] for left, right in zip(intervals, intervals[1:]))):
        raise ASRError("ASR 切段覆盖校验失败。")
    result = {"model": MODEL, "source_sha256": fingerprint, "duration_seconds": len(samples) / SAMPLE_RATE,
              "segments": segments, "usage": usage, "audio": metadata,
              "coverage": {"complete": True, "sample_count": len(samples), "covered_sample_count": covered,
                           "segment_count": len(intervals), "cached_segment_count": hits},
              "normalized_audio_path": str(audio_path.resolve()), "configuration": configuration}
    _atomic_json(cache_dir / "result.json", result)
    return result
