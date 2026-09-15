"""Build concise ASR/OCR/fused prose for every 2025 source.

The prior reviewed report is treated as proofreading reference, never as a
replacement for the newly source-bound MiMo ASR and one-second RapidOCR runs.
User-facing text contains no frame indices, timestamps, confidence values or
processing notes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from .runner import atomic_json, read_json

try:
    from scripts.build_reviewed_word import clean_text, natural_key
except ImportError:  # pragma: no cover - direct module execution fallback
    try:
        from build_reviewed_word import clean_text, natural_key
    except ImportError:  # public-safe checkout omits private correction tables
        def clean_text(text: str, title: str = "") -> str:
            del title
            text = str(text or "").replace("\u3000", " ").replace("\ufeff", "")
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r" *\n *", "\n", text).strip()
            text = re.sub(r"。{2,}", "。", text)
            return re.sub(r"，{2,}", "，", text)

        def natural_key(value: str) -> tuple[Any, ...]:
            return tuple(
                int(part) if part.isdigit() else part.casefold()
                for part in re.split(r"(\d+)", value)
            )


SECTION_TITLES = ("ASR 结果", "OCR 结果", "GPT 融合校对结果")
COURSEWARE_ORDER = {".pdf": 0, ".png": 1, ".jpg": 2, ".jpeg": 2,
                    ".pptx": 3, ".docx": 4}
IMPORTANT_VISUAL = re.compile(
    r"姓名|职务|单位|标题|课程|目录|目标|原则|流程|职责|数据|案例|要点|标准|风险|安全|制度|要求|定义|步骤|清单|时间|人物|我是|经理|总工|专责|书记|主任|讲师|供电局|有限公司|项目|工程|习近平"
)
NUMBER_WITH_CONTEXT = re.compile(r"\d+(?:\.\d+)?\s*(?:年|月|日|人|次|%|％|米|公里|千伏|伏|吨|元|户|台|项|条|个)")
INCIDENTAL_PRINT = re.compile(
    r"本社|邮票|征稿|报讯|通讯地址|评审工作|版刊登|初评小组|办公室文件|各州、市|本部各部门|现予以印发|补助资金|报送"
)
BOILERPLATE_VISUAL = {"青马工程", "云南电网公司", "中国南方电网", "视频引自网络", "视频资料出自网络"}
VISUAL_PREFIX = re.compile(r"^\s*\[(?:字幕|课件|其他)(?:/(?:subtitles|slide_content|other))?\]\s*", re.I)


def text_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value).casefold())


def normalize_visual_line(value: str, title: str) -> str:
    line = clean_text(VISUAL_PREFIX.sub("", str(value or "")), title)
    compact = text_key(line)
    if 3 <= len(compact) <= 10 and "南方电" in compact:
        return "中国南方电网"
    if 4 <= len(compact) <= 10 and "云南电" in compact and "公司" in compact:
        return "云南电网公司"
    if compact.startswith("青马工") and len(compact) <= 6:
        return "青马工程"
    if compact.startswith("开讲") and len(compact) <= 4:
        return "开讲啦"
    return line


def usable_visual_line(value: str) -> bool:
    compact = text_key(value)
    if len(compact) < 2:
        return False
    if len(compact) >= 4 and len(set(compact)) == 1:
        return False
    chinese = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if re.fullmatch(r"[\d\s.=|()\[\],+\-—–/\\]+", value):
        return False
    if chinese == 0 and latin and not re.search(r"\s", value) and len(compact) < 8:
        return False
    if value.count("|") >= 3 and chinese < 4:
        return False
    return True


def similar(left: str, right: str, threshold: float = 0.84) -> bool:
    a, b = text_key(left), text_key(right)
    if not a or not b:
        return False
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= threshold


def dedupe_lines(values: Iterable[str], title: str, *, fuzzy: bool = True) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        line = normalize_visual_line(value, title)
        line = re.sub(r"^[·•▪■□◆◇—–-]+\s*", "", line).strip()
        key = text_key(line)
        if not usable_visual_line(line) or key in seen:
            continue
        if fuzzy and any(similar(line, previous) for previous in output[-80:]):
            continue
        seen.add(key)
        output.append(line)
    return output


def paragraphize(text: str, title: str, maximum: int = 650) -> list[str]:
    text = clean_text(text, title)
    if not text:
        return []
    sentences = [part.strip() for part in re.split(r"(?<=[。！？；])\s*|\n+", text) if part.strip()]
    paragraphs: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > maximum:
            paragraphs.append(current)
            current = ""
        if len(sentence) <= maximum:
            current += sentence
            continue
        if current:
            paragraphs.append(current)
            current = ""
        for begin in range(0, len(sentence), maximum):
            paragraphs.append(sentence[begin:begin + maximum])
    if current:
        paragraphs.append(current)
    return paragraphs


def load_manual_overrides(primary: Path) -> dict[str, list[str]]:
    """Load the reviewed corpus plus independently reviewed draft groups."""
    overrides = read_json(primary) if primary.is_file() else {}
    if not isinstance(overrides, dict):
        raise ValueError("人工融合校对文件格式错误")
    for supplemental in sorted(primary.parent.glob("fusion-draft-*.json")):
        additions = read_json(supplemental)
        if not isinstance(additions, dict):
            raise ValueError(f"补充融合校对文件格式错误：{supplemental.name}")
        overlap = set(overrides).intersection(additions)
        if overlap:
            raise ValueError(f"融合校对稿出现重复文件：{sorted(overlap)[0]}")
        overrides.update(additions)
    return overrides


def apply_full_fusion_review(files: list[dict], review: dict) -> None:
    """Replace every provisional fusion with the audited full-text review."""
    reviewed = review.get("files")
    review_model = review.get("model")
    accepted_models = {"mimo-v2.5-pro", "gpt-lossless-full-review"}
    if (review_model not in accepted_models or not isinstance(reviewed, list)
            or len(reviewed) != len(files)):
        raise ValueError("无损全文融合复核文件不完整")
    for source, replacement in zip(files, reviewed):
        if (replacement.get("source_name") != source["source_name"]
                or replacement.get("source_sha256") != source["source_sha256"]):
            raise ValueError(f"无损全文融合复核顺序或源文件指纹不匹配：{source['source_name']}")
        sections = replacement.get("sections")
        paragraphs = sections.get("GPT 融合校对结果") if isinstance(sections, dict) else None
        if (not isinstance(paragraphs, list) or not paragraphs
                or not all(isinstance(value, str) and value.strip() for value in paragraphs)):
            raise ValueError(f"无损全文融合复核正文无效：{source['source_name']}")
        source["sections"]["GPT 融合校对结果"] = paragraphs
        replacement_status = replacement.get("evidence_status") or {}
        if not isinstance(replacement_status, dict):
            raise ValueError(f"无损全文融合复核证据状态无效：{source['source_name']}")
        destination_status = source.setdefault("evidence_status", {})
        destination_status["fusion_text_source"] = replacement_status.get(
            "fusion_text_source",
            "mimo-v2.5-pro-lossless-full-review" if review_model == "mimo-v2.5-pro"
            else "gpt-lossless-full-review",
        )
        if "verified_ocr_noise_anchors" in replacement_status:
            exclusions = replacement_status["verified_ocr_noise_anchors"]
            if not isinstance(exclusions, list):
                raise ValueError(
                    f"无损全文融合复核OCR噪声核验项无效：{source['source_name']}")
            destination_status["verified_ocr_noise_anchors"] = deepcopy(exclusions)


def _legacy_time(label: str) -> float:
    match = re.search(r"(\d+):(\d+):(\d+(?:\.\d+)?)", str(label))
    return (int(match.group(1)) * 3600 + int(match.group(2)) * 60
            + float(match.group(3))) if match else math.inf


def _persistent_rapid_lines(ocr: dict, title: str) -> list[tuple[float, str, str]]:
    occurrences: Counter[str] = Counter()
    best: dict[str, tuple[float, float, str]] = {}
    for frame in ocr.get("frames", []):
        frame_seen: set[str] = set()
        for item in frame.get("lines", []):
            text = normalize_visual_line(item.get("text", ""), title)
            key = text_key(text)
            if len(key) < 2 or key in frame_seen:
                continue
            frame_seen.add(key)
            occurrences[key] += 1
            candidate = (float(item.get("confidence", 0)), float(frame["timestamp_seconds"]), text)
            if key not in best or candidate[0] > best[key][0]:
                best[key] = candidate
    output = []
    for key, count in occurrences.items():
        confidence, timestamp, text = best[key]
        # Two sightings preserve slide headings and static facts while avoiding
        # the one-second fragments produced by rolling subtitles.
        if count >= 2 and confidence >= 0.72:
            output.append((timestamp, text, "rapid"))
    return sorted(output)


def _corroborated_vision(vision: dict, ocr: dict, asr_text: str,
                         legacy_lines: list[str], title: str) -> list[tuple[float, str, str]]:
    rapid_by_index = {frame["index"]: [item.get("text", "") for item in frame.get("lines", [])]
                      for frame in ocr.get("frames", [])}
    output = []
    references = legacy_lines + [asr_text]
    for frame in vision.get("frames", []):
        timestamp = float(frame.get("timestamp_seconds", 0))
        local = rapid_by_index.get(frame.get("frame_index"), [])
        for raw in frame.get("visible_text", []):
            line = normalize_visual_line(raw, title)
            if len(text_key(line)) < 2:
                continue
            if any(similar(line, value, 0.58) for value in local + references):
                output.append((timestamp, line, "vision"))
    return output


def _strong_rapid_lines(ocr: dict, title: str) -> list[str]:
    occurrences: Counter[str] = Counter()
    non_subtitle_occurrences: Counter[str] = Counter()
    best: dict[str, tuple[float, float, str]] = {}
    for frame in ocr.get("frames", []):
        frame_seen: set[str] = set()
        for item in frame.get("lines", []):
            text = normalize_visual_line(item.get("text", ""), title)
            key = text_key(text)
            if len(key) < 2 or key in frame_seen or not usable_visual_line(text):
                continue
            frame_seen.add(key)
            occurrences[key] += 1
            box = item.get("box") or []
            height = float(frame.get("height") or 0)
            top = min((float(point[1]) for point in box
                       if isinstance(point, (list, tuple)) and len(point) >= 2),
                      default=0.0)
            # Spoken subtitles in these videos occupy a narrow band at the
            # very bottom.  They belong in the OCR transcript, but must not be
            # reintroduced as supposedly visual-only facts in the fusion.
            if not height or top < height * 0.82:
                non_subtitle_occurrences[key] += 1
            candidate = (float(item.get("confidence", 0)), float(frame["timestamp_seconds"]), text)
            if key not in best or candidate[0] > best[key][0]:
                best[key] = candidate
    values = [best[key] for key, count in occurrences.items()
              if count >= 3 and non_subtitle_occurrences[key] >= 1
              and best[key][0] >= 0.88]
    return [item[2] for item in sorted(values, key=lambda item: item[1])]


def _bottom_subtitle_transcript(ocr: dict, title: str) -> str:
    """Recover a clean, time-ordered subtitle transcript for ASR proofreading.

    This is used only by the fused section when a fresh MiMo run is missing;
    the separately labelled ASR section still exposes the speech-recognition
    result.  Exact frame/timestamp metadata never enters user-facing text.
    """
    phrases: list[str] = []
    for frame in ocr.get("frames", []):
        height = float(frame.get("height") or 0)
        if not height:
            continue
        frame_lines: list[tuple[float, float, str]] = []
        for item in frame.get("lines", []):
            box = item.get("box") or []
            points = [point for point in box
                      if isinstance(point, (list, tuple)) and len(point) >= 2]
            if not points or min(float(point[1]) for point in points) < height * 0.79:
                continue
            if float(item.get("confidence", 0)) < 0.88:
                continue
            line = normalize_visual_line(item.get("text", ""), title)
            key = text_key(line)
            if len(key) < 3 or line in BOILERPLATE_VISUAL:
                continue
            if len(re.findall(r"[\u3400-\u9fff]", line)) < 2:
                continue
            frame_lines.append((min(float(point[1]) for point in points),
                                min(float(point[0]) for point in points), line))
        phrase = " ".join(item[2] for item in sorted(frame_lines))
        if not phrase:
            continue
        if phrases and similar(phrase, phrases[-1], 0.94):
            # Keep the more complete form of a rolling/two-line subtitle.
            if len(text_key(phrase)) > len(text_key(phrases[-1])):
                phrases[-1] = phrase
            continue
        phrases.append(phrase)
    return clean_text("；".join(phrases), title)


def video_ocr_lines(manifest: dict, ocr: dict, vision: dict,
                    legacy: dict | None, asr_text: str) -> list[str]:
    title = manifest["source_name"]
    timed: list[tuple[float, str, str]] = []
    legacy_lines: list[str] = []
    for block in (legacy or {}).get("ocr_blocks", []):
        timestamp = _legacy_time(block.get("label", ""))
        for raw in re.split(r"[\r\n]+", block.get("text", "")):
            line = normalize_visual_line(raw, title)
            if line:
                legacy_lines.append(line)
                timed.append((timestamp, line, "legacy"))
    timed.extend(_persistent_rapid_lines(ocr, title))
    timed.extend(_corroborated_vision(vision, ocr, asr_text, legacy_lines, title))
    timed.sort(key=lambda item: (item[0], {"vision": 0, "legacy": 1, "rapid": 2}[item[2]]))
    return dedupe_lines((item[1] for item in timed), title)


def _sections_from_sample(content: dict) -> dict[str, list[str]]:
    result = {title: [] for title in SECTION_TITLES}
    current = None
    for block in content.get("blocks", []):
        if block.get("type") == "heading":
            current = block.get("text")
        elif current in result and block.get("type") == "paragraph":
            result[current].append(block.get("text", ""))
        elif current in result and block.get("type") == "table":
            headers = "｜".join(block.get("headers", []))
            rows = ["｜".join(map(str, row)) for row in block.get("rows", [])]
            result[current].append("；".join([headers] + rows))
    return result


def _visual_supplements(ocr_lines: list[str], narrative: str, limit: int = 40) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    reference_sentences = [part for part in re.split(r"[。！？；\n]+", narrative) if text_key(part)]
    for order, line in enumerate(ocr_lines):
        key = text_key(line)
        if len(key) < 4 or key in text_key(narrative):
            continue
        if line in BOILERPLATE_VISUAL:
            continue
        if line.startswith("份有限公司") or re.search(r"相关措施.*特制定", line):
            continue
        if (re.fullmatch(r".*供电局", line)
                or (line.endswith("年") and not re.search(r"\d", line))
                or (len(key) <= 10 and re.fullmatch(
                    r"(?:党支部)?书记|(?:人力资源部)?主任|主任工程师|"
                    r"项目总工|总工程师|安全管理专责|讲师", line))):
            continue
        # A near match is usually a subtitle spelling variant, not a visual-only
        # fact.  Keeping it would reintroduce the very typo that ASR corrected.
        if any(similar(line, reference, 0.64) for reference in reference_sentences):
            continue
        if INCIDENTAL_PRINT.search(line):
            continue
        if IMPORTANT_VISUAL.search(line) or NUMBER_WITH_CONTEXT.search(line):
            score = 0
            score += 6 if re.search(r"我是|经理|项目总工|总工程师|专责|书记|主任|讲师|出品", line) else 0
            score += 5 if re.search(r"供电局|有限公司|所属单位", line) else 0
            score += 4 if re.search(r"标题|课程|目录|目标|原则|流程|职责|案例|要点|标准|风险|安全|制度|要求|定义|步骤|清单", line) else 0
            score += 3 if NUMBER_WITH_CONTEXT.search(line) else 0
            score += 2 if 5 <= len(key) <= 45 else 0
            score -= 3 if len(key) > 80 or line.rstrip().endswith(("：", ":")) else 0
            if score >= 6:
                candidates.append((score, order, line))
    top = sorted(candidates, key=lambda item: (-item[0], item[1]))[:limit]
    top.sort(key=lambda item: item[1])
    return dedupe_lines((item[2] for item in top), "", fuzzy=True)


def build_video(manifest: dict, work: Path, legacy: dict | None,
                override: list[str] | None = None) -> dict:
    title = manifest["source_name"]
    ocr = read_json(work / "ocr.json")
    asr_path, vision_path = work / "asr.json", work / "vision.json"
    asr = read_json(asr_path) if asr_path.is_file() else {}
    vision = read_json(vision_path) if vision_path.is_file() else {"frames": []}
    prior = "\n".join((legacy or {}).get("asr_paragraphs", []))
    if manifest.get("has_audio"):
        mimo_complete = (
            asr.get("model") == "mimo-v2.5-asr"
            and asr.get("source_sha256") == manifest["sha256"]
            and asr.get("coverage", {}).get("complete") is True
            and bool(asr.get("segments"))
        )
        raw_asr = ("\n".join(segment.get("text", "") for segment in asr.get("segments", []))
                   if mimo_complete else prior)
        asr_paragraphs = paragraphize(raw_asr, title)
        if not asr_paragraphs:
            asr_paragraphs = ["未识别到可用语音文字。"]
        asr_source = "mimo-v2.5-asr" if mimo_complete else "prior-reviewed-asr-fallback"
    else:
        raw_asr = ""
        asr_paragraphs = ["不适用：该视频不含音轨。"]
        asr_source = "no-audio-stream"

    ocr_lines = video_ocr_lines(manifest, ocr, vision, legacy, raw_asr)
    ocr_paragraphs = paragraphize("；".join(ocr_lines), title, maximum=480)
    if not ocr_paragraphs:
        ocr_paragraphs = ["未识别到可用画面文字。"]

    sample_path = work / "fusion_content.json"
    fusion_text_source = None
    if override:
        fused = [clean_text(paragraph, title) for paragraph in override if clean_text(paragraph, title)]
        fusion_text_source = "manual-cross-checked"
    elif sample_path.is_file() and "决战二号塔" in title:
        sample = _sections_from_sample(read_json(sample_path))
        # Keep the fresh MiMo ASR section; reuse only the thoroughly adjudicated
        # visual/fused prose from the accepted sample.
        ocr_paragraphs = sample["OCR 结果"] or ocr_paragraphs
        fused = sample["GPT 融合校对结果"]
        fusion_text_source = "accepted-mimo-vision-sample"
    else:
        if manifest.get("has_audio"):
            # Prefer the complete MiMo transcription.  The prior report is a
            # useful proofreading/fallback reference, but some of its older
            # ASR passages still contain phonetic substitutions and must never
            # overwrite a complete, materially cleaner MiMo result.
            if mimo_complete:
                narrative = raw_asr
                fusion_text_source = "mimo-asr-corrected"
            else:
                subtitle_text = _bottom_subtitle_transcript(ocr, title)
                prior_size = len(text_key(prior))
                subtitle_size = len(text_key(subtitle_text))
                if subtitle_size >= 300 and subtitle_size >= prior_size * 0.45:
                    narrative = subtitle_text
                    fusion_text_source = "rapidocr-subtitle-assisted"
                else:
                    narrative = prior
                    fusion_text_source = "prior-reviewed-asr-fallback"
        else:
            narrative = ""
            fusion_text_source = "visual-only"
        narrative_paragraphs = paragraphize(narrative, title)
        supplement_limit = 160 if not manifest.get("has_audio") else (90 if len(narrative) < 500 else 12)
        strong_visual = _strong_rapid_lines(ocr, title)
        if vision.get("frames"):
            legacy_lines = [raw for block in (legacy or {}).get("ocr_blocks", [])
                            for raw in re.split(r"[\r\n]+", block.get("text", ""))]
            strong_visual.extend(item[1] for item in _corroborated_vision(
                vision, ocr, raw_asr, legacy_lines, title))
        supplements = _visual_supplements(
            strong_visual, "\n".join(narrative_paragraphs), limit=supplement_limit)
        fused = narrative_paragraphs
        if supplements:
            label = "画面呈现的核心内容包括：" if len(narrative) < 500 else "画面文字补充："
            fused += paragraphize(label + "；".join(supplements) + "。", title, maximum=600)
        if not fused:
            fused = ["未形成可用的融合校对文字。"]
    return {
        "source_name": title,
        "source_sha256": manifest["sha256"],
        "kind": "video",
        "evidence_status": {"asr_source": asr_source,
                            "vision_model": vision.get("model") if vision.get("frames") else None,
                            "rapidocr_every_second": True,
                            "fusion_text_source": fusion_text_source},
        "sections": {"ASR 结果": asr_paragraphs, "OCR 结果": ocr_paragraphs,
                     "GPT 融合校对结果": fused},
    }


def _courseware_pages(record: dict, title: str) -> list[list[str]]:
    if record.get("reviewed_document_lines"):
        # DOCX native text is a document-wide sequence rather than page-bound
        # OCR.  Small semantic windows prevent the fusion summary from keeping
        # only the first six lines of a long document.
        lines = dedupe_lines(record["reviewed_document_lines"], title)
        return [lines[begin:begin + 24] for begin in range(0, len(lines), 24)]
    pages = []
    for page in record.get("pages", []):
        lines = page.get("reviewed_lines") or [item.get("text", "") for item in page.get("ocr_lines", [])]
        cleaned = dedupe_lines(lines, title)
        if cleaned:
            pages.append(cleaned)
    return pages


def _courseware_fusion_lines(pages: list[list[str]], title: str) -> list[str]:
    """Retain each slide's heading and strongest core statements."""
    selected: list[str] = []
    for page in pages:
        if not page:
            continue
        ranked: list[tuple[int, int, int, str]] = []
        for index, line in enumerate(page):
            key = text_key(line)
            score = (4 if index < 2 else 0) + (3 if IMPORTANT_VISUAL.search(line) else 0)
            score += 2 if any(mark in line for mark in ("：", "。", "？", "！")) else 0
            score += 1 if 10 <= len(key) <= 100 else 0
            ranked.append((score, min(len(key), 120), -index, line))
        choices = sorted(ranked, reverse=True)[:min(6, len(ranked))]
        choices.sort(key=lambda item: -item[2])
        selected.extend(item[3] for item in choices)
    return dedupe_lines(selected, title)


def build_courseware(item: dict, record: dict, legacy: dict | None,
                     override: list[str] | None = None) -> dict:
    title = item["source_name"]
    if record.get("source") != title or record.get("source_sha256") != item["sha256"]:
        raise ValueError(f"课件OCR与源文件指纹不匹配：{title}")
    pages = _courseware_pages(record, title)
    ocr_paragraphs = []
    for lines in pages:
        # Keep the visual reading order visible without exposing page numbers.
        # Newlines are substantially easier to scan than a wall of semicolons
        # for tables, slide bullets and document outlines.
        page_text = "\n".join(lines)
        if len(page_text) <= 1000:
            ocr_paragraphs.append(page_text)
        else:
            ocr_paragraphs.extend(paragraphize("；".join(lines), title, maximum=620))
    if not ocr_paragraphs:
        old = [block.get("text", "") for block in (legacy or {}).get("ocr_blocks", [])]
        ocr_paragraphs = paragraphize("\n".join(old), title, maximum=520)
    if not ocr_paragraphs:
        ocr_paragraphs = ["未识别到可用文字。"]
    # Native OOXML/PDF text has already been used to correct OCR.  The fused
    # section keeps the complete order, with duplicate lines removed.
    if override:
        fused = [clean_text(paragraph, title) for paragraph in override
                 if clean_text(paragraph, title)]
        fusion_text_source = "manual-cross-checked"
    else:
        fused_lines = _courseware_fusion_lines(pages, title)
        fused = paragraphize("；".join(fused_lines), title, maximum=620)
        fusion_text_source = "native-text-and-ocr"
    if not fused:
        fused = list(ocr_paragraphs)
    return {
        "source_name": title,
        "source_sha256": item["sha256"],
        "kind": "courseware",
        "evidence_status": {"fusion_text_source": fusion_text_source},
        "sections": {"ASR 结果": ["不适用：该文件不含音轨。"],
                     "OCR 结果": ocr_paragraphs,
                     "GPT 融合校对结果": fused},
    }


def validate_corpus(result: dict, inventory: dict) -> None:
    expected = len(inventory["videos"]) + len(inventory["courseware"])
    files = result.get("files", [])
    if len(files) != expected:
        raise ValueError(f"融合文件数不正确：{len(files)} != {expected}")
    names = [item.get("source_name") for item in files]
    if len(set(names)) != len(names):
        raise ValueError("融合结果出现重复文件")
    expected_names = [item["source_name"] for item in inventory["videos"]] + [
        item["source_name"] for item in sorted(
            inventory["courseware"],
            key=lambda value: (COURSEWARE_ORDER[value["extension"]], natural_key(value["source_name"])),
        )
    ]
    if names != expected_names:
        raise ValueError("融合结果未按视频、PDF、PNG/JPG、PPTX、DOCX顺序组织")
    for item in files:
        sections = item.get("sections", {})
        if tuple(sections) != SECTION_TITLES:
            raise ValueError(f"三段式标题错误：{item.get('source_name')}")
        if any(not isinstance(values, list) or not any(str(v).strip() for v in values)
               for values in sections.values()):
            raise ValueError(f"存在空结果部分：{item.get('source_name')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成2025全部文件三路融合正文")
    parser.add_argument("--inventory", type=Path, default=Path(".work/batch-2025/inventory.json"))
    parser.add_argument("--legacy", type=Path, default=Path(".work/batch-2025/legacy_reference.json"))
    parser.add_argument("--courseware", type=Path, default=Path(".work/batch-2025/courseware_ocr.json"))
    parser.add_argument("--work-root", type=Path, default=Path(".work/single-video"))
    parser.add_argument("--manual-overrides", type=Path,
                        default=Path(".work/batch-2025/manual_fusion_overrides.json"))
    parser.add_argument("--full-review", type=Path,
                        default=Path(".work/batch-2025/full_fusion_review.json"))
    parser.add_argument("--output", type=Path, default=Path(".work/batch-2025/fused_content.json"))
    args = parser.parse_args()
    try:
        inventory, legacy_data, courseware = map(read_json, (
            args.inventory.resolve(), args.legacy.resolve(), args.courseware.resolve()))
        legacy = {item["source_name"]: item for item in legacy_data.get("files", [])}
        courseware_by_name = {item["source"]: item for item in courseware.get("files", [])}
        overrides = load_manual_overrides(args.manual_overrides.resolve())
        files = []
        for manifest in inventory["videos"]:
            work = args.work_root.resolve() / manifest["sha256"][:16]
            files.append(build_video(
                manifest, work, legacy.get(manifest["source_name"]),
                overrides.get(manifest["source_name"])))
        sorted_courseware = sorted(
            inventory["courseware"],
            key=lambda value: (COURSEWARE_ORDER[value["extension"]], natural_key(value["source_name"])),
        )
        for item in sorted_courseware:
            if item["source_name"] not in courseware_by_name:
                raise ValueError(f"缺少课件OCR结果：{item['source_name']}")
            files.append(build_courseware(
                item, courseware_by_name[item["source_name"]], legacy.get(item["source_name"]),
                overrides.get(item["source_name"])))
        if args.full_review.is_file():
            apply_full_fusion_review(files, read_json(args.full_review.resolve()))
        result = {"schema_version": "2025-fused-1", "file_count": len(files), "files": files}
        validate_corpus(result, inventory)
        atomic_json(args.output.resolve(), result)
        print(json.dumps({"file_count": len(files), "output": str(args.output.resolve())},
                         ensure_ascii=False), flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
