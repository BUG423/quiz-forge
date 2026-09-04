from __future__ import annotations

import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# 强制模型库进入离线模式。启动服务后不会尝试访问模型网站。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from faster_whisper import WhisperModel


BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = Path(os.getenv("ASR_MODEL_PATH", BASE_DIR / "models" / "faster-whisper-small"))
WORK_DIR = BASE_DIR / ".work"
WORK_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = int(os.getenv("ASR_MAX_UPLOAD_BYTES", str(20 * 1024**3)))
ALLOWED_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".webm",
    ".ogg", ".ogv", ".oga", ".aac", ".flac", ".wma", ".avi",
}

app = FastAPI(title="本地音频转文档", docs_url=None, redoc_url=None)
_model: WhisperModel | None = None
_model_guard = threading.Lock()
_inference_guard = threading.Lock()


def get_model() -> WhisperModel:
    """只从本地目录加载模型，禁止隐式下载。"""
    global _model
    if _model is not None:
        return _model
    with _model_guard:
        if _model is None:
            if not (MODEL_DIR / "model.bin").is_file():
                raise RuntimeError(f"未找到本地模型：{MODEL_DIR}")
            _model = WhisperModel(
                str(MODEL_DIR),
                device=os.getenv("ASR_DEVICE", "cpu"),
                compute_type=os.getenv("ASR_COMPUTE_TYPE", "int8"),
                cpu_threads=max(1, int(os.getenv("ASR_CPU_THREADS", str(os.cpu_count() or 4)))),
                local_files_only=True,
            )
    return _model


def confidence_from_segment(segment: Any) -> float:
    word_scores = [
        float(word.probability)
        for word in (segment.words or [])
        if word.probability is not None
    ]
    if word_scores:
        return sum(word_scores) / len(word_scores)
    return max(0.0, min(1.0, math.exp(float(segment.avg_logprob))))


def normalize_punctuation(text: str) -> str:
    """只统一中英文标点，不猜测或改写语义。"""
    if not re.search(r"[\u4e00-\u9fff]", text):
        return text
    return (
        text.replace(",", "，")
        .replace("?", "？")
        .replace("!", "！")
        .replace(";", "；")
    )


def build_document(segments: list[dict[str, Any]]) -> str:
    """把短识别片段合并成适合阅读的自然段，不修改识别文字。"""
    paragraphs: list[str] = []
    current = ""
    previous_end: float | None = None

    for segment in segments:
        text = segment["text"].strip()
        if not text:
            continue
        long_pause = previous_end is not None and segment["start"] - previous_end >= 2.2
        if current and (long_pause or len(current) + len(text) > 180):
            paragraphs.append(current.strip())
            current = ""
        current += text
        previous_end = segment["end"]

    if current.strip():
        paragraphs.append(current.strip())
    return "\n\n".join(paragraphs)


def transcribe_local(media_path: Path, original_name: str) -> dict[str, Any]:
    started = time.perf_counter()
    model = get_model()

    # 当前机器只有 CPU，因此串行推理，避免批量文件同时耗尽内存。
    with _inference_guard:
        filename_context = re.sub(r"[^\w\u4e00-\u9fff]+", " ", Path(original_name).stem)[:160].strip()
        raw_segments, info = model.transcribe(
            str(media_path),
            language="zh",
            beam_size=5,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,
            condition_on_previous_text=True,
            initial_prompt=(
                f"文件主题：{filename_context}。以下是普通话音视频内容，请准确转写完整语句、人名和数字。"
                if filename_context
                else "以下是普通话音视频内容，请准确转写完整语句、人名和数字。"
            ),
            hotwords=filename_context or None,
        )

        segments: list[dict[str, Any]] = []
        for raw in raw_segments:
            text = normalize_punctuation(raw.text.strip())
            if not text:
                continue
            confidence = confidence_from_segment(raw)
            segments.append(
                {
                    "start": round(float(raw.start), 2),
                    "end": round(float(raw.end), 2),
                    "text": text,
                    "confidence": round(confidence, 4),
                    "needs_review": confidence < 0.75,
                }
            )

    document = build_document(segments)
    return {
        "text": document,
        "duration": round(float(info.duration), 2),
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4),
        "segments": segments,
        "low_confidence_count": sum(item["needs_review"] for item in segments),
        "processing_seconds": round(time.perf_counter() - started, 2),
        "model": MODEL_DIR.name,
        "local_only": True,
    }


async def save_upload(upload: UploadFile) -> Path:
    original_name = upload.filename or "media"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式：{suffix or '未知'}")

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=WORK_DIR)
    path = Path(handle.name)
    size = 0
    try:
        with handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件超过本机设置的大小限制")
                handle.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


@app.middleware("http")
async def local_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'"
    )
    return response


@app.get("/api/health")
def health() -> dict[str, Any]:
    model_ready = (MODEL_DIR / "model.bin").is_file()
    return {
        "status": "ready" if model_ready else "model_missing",
        "model": MODEL_DIR.name,
        "model_ready": model_ready,
        "model_loaded": _model is not None,
        "device": os.getenv("ASR_DEVICE", "cpu"),
        "local_only": True,
    }


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)) -> JSONResponse:
    original_name = file.filename or "media"
    path = await save_upload(file)
    try:
        result = await run_in_threadpool(transcribe_local, path, original_name)
        return JSONResponse(result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"本地转写失败：{exc}") from exc
    finally:
        path.unlink(missing_ok=True)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(BASE_DIR / "styles.css", media_type="text/css")


@app.get("/app.js")
def script() -> FileResponse:
    return FileResponse(BASE_DIR / "app.js", media_type="application/javascript")


@app.get("/document-utils.js")
def document_utils() -> FileResponse:
    return FileResponse(BASE_DIR / "document-utils.js", media_type="application/javascript")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
