"""Reject abbreviated fusion copy that drops source content or exam anchors.

The checker is deliberately lexical.  It does not try to decide whether prose
is *good*; it establishes a hard lower bound for completeness before a human or
model performs semantic proofreading.  Punctuation, whitespace, Unicode width
variants, and duplicate/rolling subtitle units do not affect length coverage.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from typing import Iterable


ASR_SECTION = "ASR 结果"
OCR_SECTION = "OCR 结果"
FUSION_SECTION = "GPT 融合校对结果"

_UNIT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*|[\r\n]+")
_LENGTH_CHARS = re.compile(r"[0-9a-z\u3400-\u9fff]", re.I)
_CHINESE_DIGITS = "零〇一二三四五六七八九十百千万亿两点"
_NUMBER_UNITS = (
    "亿(?:公里|千米|米|元|户|人次|人|次|台|套|座|宗|项|条|个|种|类)?|"
    "万(?:公里|千米|米|元|户|人次|人|次|台|套|座|宗|项|条|个|种|类)?|"
    "公里|千米|毫米|厘米|米|平方公里|平方千米|千伏|伏|千瓦|兆瓦|吉瓦|"
    "千瓦时|兆瓦时|毫克|千克|公斤|吨|元|万元|亿元|户|人次|人|次|台|套|"
    "座|宗|项|条|个|种|类|级|阶段|倍|份|组|家|所|件|声|届|帧|号"
)
_OCR_ACRONYM_ALLOWLIST = frozenset({
    "AI", "API", "AOPA", "AR", "BIM", "BPMN", "BSC", "CA", "CCTV",
    "CNAS", "COP", "CSG", "CT", "DHR", "DNS", "EAM", "EAP", "EMS",
    "ERP", "GB", "GDP", "GIS", "GOOSE", "GPU", "HD", "HR", "HRBP",
    "HRS", "IAAS", "IEC", "IO", "IP", "IT", "KPI", "MAC", "MBO",
    "MD", "MECE", "MIMO", "MMS", "MPLS", "OA", "OCR", "OCS", "OMS",
    "OSS", "PAAS", "PB", "PC", "PDCA", "PDF", "PPT", "PUE", "RGB",
    "RPA", "RS", "RTK", "SCADA", "SF", "SIM", "SSC", "SVG", "TB",
    "TP", "UI", "UPS", "USB", "VPN", "WEB",
})

# Patterns are applied in this order and overlapping matches are discarded.
# This keeps ``2024年10月9日`` as one time anchor rather than three numbers.
_ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("percentage", re.compile(
        rf"百分之[{_CHINESE_DIGITS}]+|[+\-±]?\d+(?:[.,]\d+)*\s*[%％]"
    )),
    ("time", re.compile(
        rf"(?:[{_CHINESE_DIGITS}]+|\d{{2,4}})年"
        rf"(?:(?:[{_CHINESE_DIGITS}]+|\d{{1,2}})月)?"
        rf"(?:(?:[{_CHINESE_DIGITS}]+|\d{{1,2}})[日号])?"
        r"|(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"
        r"|(?<!\d)\d{1,2}[:：]\d{2}(?:[:：]\d{2}(?:[.,]\d+)?)?(?!\d)"
        rf"|(?:(?:早上|上午|中午|下午|晚上|夜间)"
        rf"(?:[零〇一二三四五六七八九十两]+|\d{{1,2}})点"
        rf"(?:(?:[零〇一二三四五六七八九十两]+|\d{{1,2}})分)?"
        rf"|(?:[三四五六七八九十]+|\d{{1,2}})点"
        rf"(?:(?:[零〇一二三四五六七八九十两]+|\d{{1,2}})分)?)"
        rf"(?![零〇一二三四五六七八九十百千万亿两])"
        rf"|(?:[{_CHINESE_DIGITS}]+|[+\-±]?\d+(?:[.,]\d+)*)\s*"
        rf"(?:小时|分钟|秒|年|月|日|号(?!杆))"
        r"|[+\-±]?\d+(?:[.,]\d+)*\s*时"
    )),
    ("model", re.compile(
        r"(?<![A-Za-z0-9])(?:"
        r"[#№]\s*\d+[A-Za-z0-9+_.\-/—–]*"
        r"|[A-Za-z]{1,12}[+_.\-/—–]*\d[A-Za-z0-9+_.\-/—–]*"
        r"|[+\-±]?\d+(?:\.\d+)?\s*(?:kV|V|kW|MW|GW|kWh|MWh|GHz|MHz|GB|TB)"
        r")(?![A-Za-z0-9])", re.I
    )),
    ("acronym", re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}(?:\+)?(?![A-Za-z0-9])")),
    ("ordinal", re.compile(
        rf"第(?:[{_CHINESE_DIGITS}]+|\d+)\s*(?:条|章|节|部分|阶段|类|项|步|点)"
    )),
    ("number", re.compile(
        rf"(?:[{_CHINESE_DIGITS}]+|[+\-±]?\d+(?:[.,]\d+)*)\s*(?:{_NUMBER_UNITS})"
        r"|(?<![A-Za-z0-9])(?:[+\-±]?\d+(?:[.,]\d+)*)(?![A-Za-z0-9])"
    )),
)

_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("secret_value", re.compile(
        r"(?i)(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|密钥|令牌|"
        r"authorization|bearer)\s*[:=：]\s*[\"']?[A-Za-z0-9_./+\-=]{6,}"
    )),
    ("secret_token", re.compile(
        r"(?i)(?:(?:sk|tp)-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{12,}|"
        r"Bearer\s+[A-Za-z0-9_.-]{12,})"
    )),
    ("frame_field", re.compile(
        r"(?i)\b(?:frame_index|frame_id|frame_number|ocr_frames|vision_frames|image_path)\b"
        r"|帧(?:号|编号|索引)\s*[:：=]?"
    )),
    ("timestamp_field", re.compile(
        r"(?i)\b(?:timestamp_seconds|timestamp_ms|timecode)\b|时间戳\s*[:：=]?"
    )),
    ("confidence_field", re.compile(r"(?i)\b(?:ocr_)?confidence\b|置信度\s*[:：=]")),
    ("subtitle_timecode", re.compile(
        r"(?m)^\s*(?:\[\s*)?\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"
        r"(?:\s*\]|\s*-->)"
    )),
)

_NO_AUDIO_PLACEHOLDER = re.compile(
    r"^(?:不适用)?(?:该|本)?(?:音视频|文件|视频|媒体|素材)?"
    r"(?:不含|没有|无|未包含|未检测到|未发现)(?:任何)?(?:可用)?的?"
    r"(?:音频|音轨|语音|声音)(?:流|轨道|内容|数据|信息)?"
    r"(?:因此)?(?:跳过|无需)?(?:asr|语音识别)?$",
    re.I,
)
_EMPTY_ASR_PLACEHOLDERS = {
    "不适用", "静音", "静音视频", "noaudio", "noaudiostream", "silence",
    "asr为空", "asr无内容", "asr识别失败", "语音识别为空", "语音识别无内容",
    "语音识别失败", "未识别到语音", "未识别到有效语音", "未识别到有效内容",
}


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _texts(value: object, section: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{section}必须是字符串或字符串数组")
    return value


def has_usable_asr(values: Iterable[str]) -> bool:
    """Distinguish real speech from the corpus' no-audio placeholders.

    Matching is intentionally anchored to the complete value.  A real lesson
    sentence such as ``本文件不含音轨时应使用OCR`` must not be discarded just
    because it contains the words ``不含音轨``.
    """
    compact = _length_key("".join(values))
    if not compact or compact in _EMPTY_ASR_PLACEHOLDERS:
        return False
    return _NO_AUDIO_PLACEHOLDER.fullmatch(compact) is None


def _length_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(_LENGTH_CHARS.findall(normalized))


def _units(values: Iterable[str]) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for value in values:
        for raw in _UNIT_SPLIT.split(unicodedata.normalize("NFKC", value)):
            raw = raw.strip()
            key = _length_key(raw)
            if key:
                output.append((raw, key))
    return output


def _deduplicated_length(values: Iterable[str]) -> int:
    """Measure unique content while collapsing exact/contained subtitle rolls."""
    retained: list[str] = []
    for _, key in _units(values):
        duplicate = False
        replacement: int | None = None
        for index, previous in enumerate(retained):
            if key == previous:
                duplicate = True
                break
            # OCR subtitles often grow a few characters at a time, and OCR may
            # use a semicolon where ASR used a comma.  Four meaningful chars is
            # enough to collapse such fragments while keeping tiny list labels
            # (``1项``, ``A类``) independently measurable.
            if min(len(key), len(previous)) >= 4:
                if key in previous:
                    duplicate = True
                    break
                if previous in key:
                    replacement = index
                    break
        if duplicate:
            continue
        if replacement is not None:
            retained[replacement] = key
        else:
            retained.append(key)
    return sum(map(len, retained))


_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
             "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_SMALL_UNIT = {"十": 10, "百": 100, "千": 1000}


def _chinese_integer(value: str) -> int:
    if not value:
        return 0
    if not any(character in value for character in "十百千万亿"):
        return int("".join(str(_CN_DIGIT[character]) for character in value))
    total = section = number = 0
    for character in value:
        if character in _CN_DIGIT:
            number = _CN_DIGIT[character]
        elif character in _CN_SMALL_UNIT:
            section += (number or 1) * _CN_SMALL_UNIT[character]
            number = 0
        elif character == "万":
            total += (section + number) * 10_000
            section = number = 0
        elif character == "亿":
            total = (total + section + number) * 100_000_000
            section = number = 0
        else:  # pragma: no cover - guarded by the caller's regex
            raise ValueError(f"不支持的中文数字：{value}")
    return total + section + number


def _chinese_number(value: str) -> str:
    value = value.replace("兩", "两")
    if "点" not in value:
        return str(_chinese_integer(value))
    integer, fraction = value.split("点", 1)
    magnitude = Decimal(1)
    while fraction.endswith(("万", "亿")):
        magnitude *= Decimal(10_000 if fraction[-1] == "万" else 100_000_000)
        fraction = fraction[:-1]
    if not fraction or any(character not in _CN_DIGIT for character in fraction):
        return _length_key(value)
    digits = "".join(str(_CN_DIGIT[character]) for character in fraction)
    number = (Decimal(_chinese_integer(integer)) + Decimal(f"0.{digits}")) * magnitude
    return _decimal_number(format(number, "f"))


def _decimal_number(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace(",", "")
    value = value.replace("±", "")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value.casefold()
    if number == number.to_integral():
        return str(int(number))
    return format(number.normalize(), "f")


def _number_value(value: str) -> str:
    value = value.strip()
    if value and all(character in _CHINESE_DIGITS for character in value):
        return _chinese_number(value)
    return _decimal_number(value)


def _quantity_value(number: str, unit: str) -> str:
    normalized_number = _number_value(number)
    try:
        value = Decimal(normalized_number)
    except InvalidOperation:
        return normalized_number + unit.casefold()
    unit = unit.casefold()
    if unit.startswith("万"):
        value *= Decimal(10_000)
        unit = unit[1:]
    elif unit.startswith("亿"):
        value *= Decimal(100_000_000)
        unit = unit[1:]
    return _decimal_number(format(value, "f")) + unit


def _canonical_anchor(kind: str, value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    compact = re.sub(r"\s+", "", value)
    if kind in {"model", "acronym"}:
        return re.sub(r"[^A-Za-z0-9#]+", "", compact).upper()
    if kind == "percentage":
        if compact.startswith("百分之"):
            return _number_value(compact[3:]) + "%"
        return _number_value(compact.rstrip("%")) + "%"
    if kind == "ordinal":
        match = re.fullmatch(rf"第([{_CHINESE_DIGITS}]+|\d+)(.+)", compact)
        return f"第{_number_value(match.group(1))}{match.group(2)}" if match else compact
    if kind == "time":
        iso = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", compact)
        if iso:
            return f"{int(iso.group(1))}年{int(iso.group(2))}月{int(iso.group(3))}日"
        clock = re.fullmatch(r"(\d{1,2})[:：](\d{2})(?:[:：](\d{2})(?:[.,](\d+))?)?", compact)
        if clock:
            result = f"{int(clock.group(1)):02d}:{clock.group(2)}"
            if clock.group(3) is not None:
                result += f":{clock.group(3)}"
            if clock.group(4):
                result += f".{clock.group(4)}"
            return result
        parts = re.findall(
            rf"([{_CHINESE_DIGITS}]+|\d+(?:\.\d+)?)(年|月|日|号|点|小时|时|分钟|分|秒)",
            compact,
        )
        if parts:
            return "".join(_number_value(number) + ("日" if unit == "号" else unit)
                           for number, unit in parts)
        return compact.casefold()
    match = re.fullmatch(rf"([{_CHINESE_DIGITS}]+|[+\-±]?\d+(?:[.,]\d+)*)(.*)", compact)
    if match:
        return _quantity_value(match.group(1), match.group(2))
    return compact.casefold()


def _reliable_ocr_anchor(item: dict[str, str]) -> bool:
    """Keep machine-checkable visual facts without canonising OCR debris."""
    kind, raw, canonical = item["kind"], item["value"], item["canonical"]
    compact = re.sub(r"\s+", "", raw)
    if kind == "acronym":
        base = re.sub(r"\d+$", "", canonical)
        return canonical in _OCR_ACRONYM_ALLOWLIST or base in _OCR_ACRONYM_ALLOWLIST
    if kind == "time":
        # ``十分`` and ``四分`` are commonly ordinary Chinese words or a
        # fraction, not clock facts.  Explicit dates, clocks, 点, 小时, 分钟
        # and 秒 remain mandatory.
        if compact.endswith("分") and "分钟" not in compact and "点" not in compact:
            return ":" in compact or "：" in compact
        return True
    if kind == "number":
        if re.fullmatch(r"[+\-±]?\d+(?:[.,]\d+)*", compact):
            return False
        number = re.match(rf"([{_CHINESE_DIGITS}]+|[+\-±]?\d+(?:[.,]\d+)*)", compact)
        if number and number.group(1) in {"万", "亿", "点"}:
            return False
    return True


def _ocr_dump_like(values: Iterable[str]) -> bool:
    """Detect semicolon-heavy raw OCR pastes masquerading as proofread prose."""
    text = "\n".join(values)
    semicolons = text.count("；") + text.count(";")
    sentence_stops = sum(text.count(mark) for mark in "。！？!?")
    very_long_rolls = sum(
        1 for paragraph in values
        if len(paragraph) >= 500 and paragraph.count("；") + paragraph.count(";") >= 20
    )
    return very_long_rolls >= 2 or (semicolons >= 100 and semicolons > sentence_stops * 4)


def _anchor_covered(item: dict[str, str], fused_keys: set[tuple[str, str]]) -> bool:
    key = item["kind"], item["canonical"]
    if key in fused_keys:
        return True
    # OCR rolling fragments may split ``1987年2月10日`` into independent
    # ``2月`` and ``10日`` anchors.  A complete fused date covers those
    # fragments.  Year fragments are deliberately excluded: ``7年`` must not
    # be treated as covered by ``2017年``.
    if item["kind"] == "time" and re.fullmatch(r"\d{1,2}(?:月|日)", item["canonical"]):
        return any(kind == "time" and item["canonical"] in canonical
                   for kind, canonical in fused_keys)
    if item["kind"] == "time" and re.fullmatch(r"\d{4}年", item["canonical"]):
        return any(kind == "time" and canonical.startswith(item["canonical"])
                   for kind, canonical in fused_keys)
    if item["kind"] == "time" and re.fullmatch(r"\d{2}:\d{2}", item["canonical"]):
        return any(kind == "time" and re.fullmatch(r"\d{2}:\d{2}:\d{2}", canonical)
                   and canonical.endswith(item["canonical"])
                   for kind, canonical in fused_keys)
    if (item["kind"] == "number" and item["value"].strip().endswith(("万", "亿"))
            and re.fullmatch(r"\d+(?:\.\d+)?", item["canonical"])):
        return any(kind == "number" and canonical.startswith(item["canonical"])
                   and len(canonical) > len(item["canonical"])
                   for kind, canonical in fused_keys)
    return False


def extract_anchors(values: Iterable[str]) -> list[dict[str, str]]:
    """Extract unique facts whose exact preservation can be tested cheaply."""
    anchors: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for text in values:
        text = unicodedata.normalize("NFKC", text)
        secret_spans = [match.span() for _, pattern in _FORBIDDEN_PATTERNS[:2]
                        for match in pattern.finditer(text)]
        occupied: list[tuple[int, int]] = []
        for kind, pattern in _ANCHOR_PATTERNS:
            for match in pattern.finditer(text):
                span = match.span()
                if any(span[0] < end and start < span[1]
                       for start, end in secret_spans):
                    continue
                if any(span[0] < end and start < span[1] for start, end in occupied):
                    continue
                raw = match.group(0).strip()
                canonical = _canonical_anchor(kind, raw)
                key = kind, canonical
                occupied.append(span)
                if not canonical or key in seen:
                    continue
                seen.add(key)
                anchors.append({"kind": kind, "value": raw, "canonical": canonical})
    return anchors


def _forbidden_matches(text: str) -> list[dict[str, int | str]]:
    # Never echo a possible credential into either stdout or the JSON report.
    return [{"kind": kind, "count": len(list(pattern.finditer(text)))}
            for kind, pattern in _FORBIDDEN_PATTERNS
            if pattern.search(text)]


def check_file(record: dict, *, min_length_retention: float = 0.90,
               min_fused_chars: int = 200,
               min_visual_retention: float = 0.70,
               require_ocr_anchors: bool = True) -> dict:
    """Return a serializable completeness report for one corpus record."""
    if not 0 < min_length_retention <= 1:
        raise ValueError("长度保留率阈值必须在0到1之间")
    if min_fused_chars < 0:
        raise ValueError("最短融合字数不能小于0")
    if not 0 < min_visual_retention <= 1:
        raise ValueError("视觉主线长度保留率阈值必须在0到1之间")
    if not isinstance(require_ocr_anchors, bool):
        raise ValueError("require_ocr_anchors必须是布尔值")
    if not isinstance(record, dict) or not str(record.get("source_name", "")).strip():
        raise ValueError("融合记录缺少source_name")
    sections = record.get("sections")
    if not isinstance(sections, dict):
        raise ValueError(f"{record['source_name']}缺少sections")
    asr = _texts(sections.get(ASR_SECTION), ASR_SECTION)
    ocr = _texts(sections.get(OCR_SECTION), OCR_SECTION)
    fused = _texts(sections.get(FUSION_SECTION), FUSION_SECTION)
    if not asr and not ocr:
        raise ValueError(f"{record['source_name']}没有ASR/OCR输入")

    raw_asr_length = _deduplicated_length(asr)
    asr_usable = has_usable_asr(asr)
    asr_length = raw_asr_length if asr_usable else 0
    ocr_length = _deduplicated_length(ocr)
    combined_length = _deduplicated_length([*(asr if asr_usable else []), *ocr])
    suffix = Path(str(record["source_name"])).suffix.casefold()
    kind = record.get("kind") or ("video" if suffix in {
        ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v"
    } else "courseware")
    strong_asr = (
        kind == "video" and asr_usable
        and (asr_length >= 300
             or (ocr_length < 300 and asr_length >= ocr_length * 0.25))
    )
    # Spoken video uses complete MiMo ASR as the non-summary backbone.  OCR is
    # a supplement and may contain repeated subtitles, scenery, watermarks or
    # unrelated scanned-page debris, so it must not force a raw OCR dump.  For
    # silent/lyric-heavy video a 70% cleaned-visual floor is paired with hard
    # visual anchors and manual review.  Native/courseware OCR stays at 90%.
    if strong_asr:
        baseline_kind = "asr"
        baseline_length = asr_length
        effective_retention = min_length_retention
    elif kind == "video":
        baseline_kind = "ocr_visual"
        baseline_length = ocr_length
        effective_retention = min_visual_retention
    else:
        baseline_kind = "ocr"
        baseline_length = ocr_length
        effective_retention = min_length_retention
    fused_length = _deduplicated_length(fused)
    denominator = max(1, baseline_length)
    retention = fused_length / denominator
    required_length = min(baseline_length, max(
        min_fused_chars, math.ceil(baseline_length * effective_retention)))
    low_retention = retention + 1e-12 < effective_retention
    too_short = fused_length < min(min_fused_chars, baseline_length)

    # ASR anchors are all mandatory.  OCR contributes only anchors not already
    # represented by ASR, so duplicated subtitles do not inflate requirements;
    # the typed anchor grammar is the reliability gate for OCR-only evidence.
    asr_anchors = extract_anchors(asr) if asr_usable else []
    asr_anchor_keys = {(item["kind"], item["canonical"])
                       for item in asr_anchors}
    ocr_unique_anchors = ([
        item for item in extract_anchors(ocr)
        if _reliable_ocr_anchor(item)
        and (item["kind"], item["canonical"]) not in asr_anchor_keys
    ] if require_ocr_anchors else [])
    source_anchors = [dict(item, source="asr") for item in asr_anchors]
    source_anchors.extend(dict(item, source="ocr") for item in ocr_unique_anchors)
    raw_exclusions = record.get("evidence_status", {}).get(
        "verified_ocr_noise_anchors", [])
    if not isinstance(raw_exclusions, list):
        raise ValueError("verified_ocr_noise_anchors必须是数组")
    allowed_reasons = {"ocr_noise", "irrelevant_background", "malformed_duplicate"}
    exclusions: list[dict[str, str]] = []
    exclusion_keys: set[tuple[str, str]] = set()
    for value in raw_exclusions:
        if (not isinstance(value, dict)
                or value.get("reason") not in allowed_reasons
                or not isinstance(value.get("kind"), str)
                or not isinstance(value.get("canonical"), str)):
            raise ValueError("verified_ocr_noise_anchors含无效人工核验项")
        key = value["kind"], value["canonical"]
        exclusion_keys.add(key)
        exclusions.append({"kind": key[0], "canonical": key[1],
                           "reason": value["reason"]})
    available_ocr_keys = {(item["kind"], item["canonical"])
                          for item in source_anchors if item["source"] == "ocr"}
    unknown_exclusions = exclusion_keys - available_ocr_keys
    source_anchors = [
        item for item in source_anchors
        if item["source"] != "ocr"
        or (item["kind"], item["canonical"]) not in exclusion_keys
    ]
    fused_anchor_keys = {(item["kind"], item["canonical"])
                         for item in extract_anchors(fused)}
    missing = [item for item in source_anchors
               if not _anchor_covered(item, fused_anchor_keys)]
    forbidden = _forbidden_matches("\n".join(fused))
    ocr_dump = _ocr_dump_like(fused)
    failures = []
    if low_retention:
        failures.append("length_retention_below_threshold")
    if too_short:
        failures.append("summary_like_short_output")
    if missing:
        failures.append("missing_exam_anchors")
    if forbidden:
        failures.append("forbidden_technical_or_secret_fields")
    if ocr_dump:
        failures.append("raw_ocr_dump_without_proofreading")
    if unknown_exclusions:
        failures.append("invalid_or_stale_ocr_noise_exclusion")

    return {
        "source_name": record["source_name"],
        "asr_usable": asr_usable,
        "strong_asr_backbone": strong_asr,
        "raw_asr_source_length": raw_asr_length,
        "asr_source_length": asr_length,
        "ocr_source_length": ocr_length,
        "combined_source_length": combined_length,
        "length_baseline_kind": baseline_kind,
        "length_baseline": baseline_length,
        "fused_length": fused_length,
        "asr_length_retention_ratio": (
            round(fused_length / asr_length, 6) if asr_usable and asr_length else None),
        "ocr_length_retention_ratio": round(fused_length / max(1, ocr_length), 6),
        "length_retention_ratio": round(retention, 6),
        "minimum_length_retention_ratio": effective_retention,
        "minimum_required_fused_length": required_length,
        "length_retention_passed": not low_retention,
        "summary_like_short_output": too_short or low_retention,
        "anchors": {
            "total": len(source_anchors),
            "covered": len(source_anchors) - len(missing),
            "all_covered": not missing,
            "missing": missing,
        },
        "verified_ocr_noise_anchors": exclusions,
        "verified_ocr_noise_anchor_count": len(exclusions),
        "unknown_ocr_noise_exclusion_count": len(unknown_exclusions),
        "forbidden_matches": forbidden,
        "raw_ocr_dump_detected": ocr_dump,
        "failures": failures,
        "passed": not failures,
    }


def build_full_fusion_qa(corpus: dict, *, min_length_retention: float = 0.90,
                         min_fused_chars: int = 200) -> dict:
    """Check every file and expose one unambiguous corpus-level boolean."""
    if not isinstance(corpus, dict) or not isinstance(corpus.get("files"), list):
        raise ValueError("融合内容必须包含files数组")
    reports = [check_file(item, min_length_retention=min_length_retention,
                          min_fused_chars=min_fused_chars)
               for item in corpus["files"]]
    return {
        "schema_version": "full-fusion-qa-1",
        "file_count": len(reports),
        "files": reports,
        "passed": all(item["passed"] for item in reports),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="逐文件核验融合稿完整性和考试锚点覆盖")
    parser.add_argument("--content", type=Path,
                        default=Path(".work/batch-2025/fused_content.json"))
    parser.add_argument("--output", type=Path, help="可选的JSON报告输出路径")
    parser.add_argument("--min-length-retention", type=float, default=0.90)
    parser.add_argument("--min-fused-chars", type=int, default=200)
    args = parser.parse_args()
    try:
        result = build_full_fusion_qa(
            _read_json(args.content.resolve(strict=True)),
            min_length_retention=args.min_length_retention,
            min_fused_chars=args.min_fused_chars,
        )
        if args.output:
            _atomic_json(args.output.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["passed"] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
