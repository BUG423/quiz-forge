#!/usr/bin/env python3
"""Export the final three-section Word without synthesising course content.

The exporter has deliberately narrow responsibilities: validate three independent
inputs, bind them by ``asset_id`` and source SHA-256, mechanically lay out ASR/OCR
evidence, copy GPT paragraphs verbatim, and verify the resulting Word file.

Input contracts
---------------

``asset_manifest.json``::

    {
      "catalog": {
        "entry_count": 81,
        "entries": ["... exactly 81 strings ..."]
      },
      "asset_count": 91,
      "assets": [{
        "asset_id": "2026/video/<sha256>",
        "source_sha256": "<64 hex characters>",
        "source_name": "lesson.mp4",
        "year": 2026,
        "kind": "video",
        "catalog_position": 1,
        "catalog_suborder": 1,
        "coverage_positions": [1]
      }]
    }

The final 1--91 asset order is the order of the manifest's ``assets`` array.
There is intentionally no parallel ``order`` field to reconcile.

``evidence_corpus.json``::

    {
      "file_count": 91,
      "files": [{
        "asset_id": "...",
        "source_sha256": "...",
        "sections": {"ASR 结果": ["..."], "OCR 结果": ["..."]}
      }]
    }

``gpt_final.json``::

    {
      "model": "<truthful model identifier>",
      "file_count": 91,
      "files": [{
        "asset_id": "...",
        "source_sha256": "...",
        "evidence_sha256": "...",
        "task_sha256": "...",
        "paragraphs": ["final GPT prose copied verbatim"]
      }]
    }

No legacy fusion field is read, and no ASR/OCR fallback can enter the red section.
The QA function is read-only: it reports or raises but never edits input JSON or the
Word document.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import re
from pathlib import Path
import sys
from typing import Any, Iterable
from zipfile import ZipFile

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


ASSET_COUNT = 91
CATALOG_ENTRY_COUNT = 81
SECTION_TITLES = ("ASR 结果", "OCR 结果", "GPT 融合结果")
ASR_SECTION, OCR_SECTION, GPT_SECTION = SECTION_TITLES

RED = "C00000"
BLACK = "000000"
BLUE = "175A8A"
DARK = "203746"
GRAY = "667085"
LIGHT_GRAY = "F3F4F6"
FONT_CN = "Microsoft YaHei"
# Word-facing packing only.  Evidence keeps the original frame/page/object and
# line structure in raw_evidence; the black OCR presentation stream is packed
# mechanically so thousands of OCR boxes do not become thousands of paragraphs.
OCR_PARAGRAPH_CHARACTERS = 10_000
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DISPLAY_NAMES = ROOT / "config" / "word_display_names.json"
DISPLAY_NAMES_SCHEMA_VERSION = "word-display-names-v1"

SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
SECRET_PATTERNS = (
    re.compile(rb"(?:sk|tp)-[A-Za-z0-9_-]{12,}"),
    re.compile(rb"api[_ -]?key\s*[:=]\s*[A-Za-z0-9._-]{12,}", re.I),
    re.compile(rb"bearer\s+[A-Za-z0-9._-]{16,}", re.I),
)
FORBIDDEN_FUSION_LITERALS = (
    "完整语音识别底稿",
    "完整画面文字",
    "ASR底稿",
    "ASR 底稿",
    "OCR底稿",
    "OCR 底稿",
    "OCR原始文字",
    "整页OCR原始文字",
    "OCR补充",
    "OCR 补充",
    "文档原生文字、脚注及图片交叉校正",
    "原生文字、备注、图表及替代文本",
    "嵌入工作簿完整单元格",
    "完整内嵌媒体语音",
    "完整内嵌媒体画面文字",
    "既有GPT逐文件校对正文",
)
FORBIDDEN_FUSION_REGEXES = (
    re.compile(
        r"(?i)(?:完整(?:的)?\s*)?"
        r"(?:(?:ASR|OCR|语音识别|文字识别)"
        r"(?:\s*(?:和|与|/|\+|、)\s*(?:ASR|OCR|语音识别|文字识别))*)"
        r"\s*(?:的)?\s*(?:识别)?\s*(?:底稿|原稿|原文|结果|补充)"
    ),
    re.compile(r"(?i)根据\s*(?:上述|前述|所给|提供的)?\s*(?:ASR|OCR|语音识别|文字识别|识别结果)"),
    re.compile(r"(?:从画面可见|识别结果显示|语音识别显示|文字识别显示|OCR显示|ASR显示)"),
    re.compile(r"(?i)(?:frame_index|timestamp_seconds|request_id|cache_path)\s*[:=]"),
)


class ExportValidationError(ValueError):
    """A safe, deterministic input or delivery validation failure."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ExportValidationError(f"无法读取有效JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ExportValidationError(f"JSON顶层必须是对象：{path}")
    return value


def _sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ExportValidationError(f"{field}必须是64位SHA-256")
    return value.lower()


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExportValidationError(f"{field}必须是非空字符串")
    return value


def _normalized_space(value: str) -> str:
    """Perform presentation-only whitespace joining; never correct wording."""
    value = value.replace("\u3000", " ").replace("\ufeff", "")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _mechanical_values(values: Any, *, field: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ExportValidationError(f"{field}必须是字符串数组")
    result = [_normalized_space(item) for item in values]
    result = [item for item in result if item]
    if not result:
        raise ExportValidationError(f"{field}不得为空")
    return result


def mechanical_ocr_paragraphs(values: Any, *, maximum: int = OCR_PARAGRAPH_CHARACTERS) -> list[str]:
    """Space-join OCR fragments and split only by character capacity.

    Joining the returned paragraphs with one space reconstructs the normalized
    OCR stream exactly. There is no de-duplication, correction, ranking, or
    semantic selection.
    """
    if not isinstance(maximum, int) or maximum < 100:
        raise ValueError("maximum must be an integer of at least 100")
    pieces = _mechanical_values(values, field=OCR_SECTION)
    paragraphs: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        if current and len(candidate) > maximum:
            paragraphs.append(current)
            current = piece
        else:
            current = candidate
    if current:
        paragraphs.append(current)
    if " ".join(paragraphs) != " ".join(pieces):
        raise AssertionError("mechanical OCR partition changed text")
    return paragraphs


def _text_stream_sha256(values: Iterable[str]) -> str:
    """Bind an already normalized paragraph stream, including its separators."""
    return hashlib.sha256(" ".join(values).encode("utf-8")).hexdigest()


def _gpt_paragraphs(values: Any, *, asset_id: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ExportValidationError(f"GPT最终正文缺失：{asset_id}")
    paragraphs: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ExportValidationError(f"GPT段落必须是已整理的非空字符串：{asset_id}")
        if re.search(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ExportValidationError(f"GPT段落不得包含换行或控制字符：{asset_id}")
        paragraphs.append(value)
    _assert_no_forbidden_fusion_labels(paragraphs, asset_id=asset_id)
    return paragraphs


def _assert_no_forbidden_fusion_labels(values: Iterable[str], *, asset_id: str) -> None:
    text = "\n".join(values)
    if any(label in text for label in FORBIDDEN_FUSION_LITERALS):
        raise ExportValidationError(f"GPT最终正文含底稿或处理标签：{asset_id}")
    if any(pattern.search(text) for pattern in FORBIDDEN_FUSION_REGEXES):
        raise ExportValidationError(f"GPT最终正文含证据或技术标签：{asset_id}")


def _declared_count(document: dict[str, Any], actual: int, *, field: str) -> None:
    declared = document.get(field)
    if declared is not None and declared != actual:
        raise ExportValidationError(f"{field}与实际数量不一致")


def _indexed_files(document: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    files = document.get("files")
    if not isinstance(files, list) or len(files) != ASSET_COUNT:
        raise ExportValidationError(f"{label}必须恰好包含{ASSET_COUNT}个资产")
    _declared_count(document, len(files), field="file_count")
    output: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(files, 1):
        if not isinstance(item, dict):
            raise ExportValidationError(f"{label}第{index}项必须是对象")
        asset_id = _nonempty_string(item.get("asset_id"), field=f"{label}.asset_id")
        if asset_id in output:
            raise ExportValidationError(f"{label}含重复asset_id")
        output[asset_id] = item
    return output


def validate_asset_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate the phase-A schema and derive presentation order from the array."""
    catalog_object = manifest.get("catalog")
    if not isinstance(catalog_object, dict):
        raise ExportValidationError("asset_manifest.catalog必须是对象")
    catalog = catalog_object.get("entries")
    if (not isinstance(catalog, list) or len(catalog) != CATALOG_ENTRY_COUNT
            or not all(isinstance(item, str) and item.strip() for item in catalog)):
        raise ExportValidationError(
            f"catalog.entries必须恰好包含{CATALOG_ENTRY_COUNT}条非空原文")
    if catalog_object.get("entry_count") != len(catalog):
        raise ExportValidationError("catalog.entry_count与catalog.entries数量不一致")

    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) != ASSET_COUNT:
        raise ExportValidationError(f"assets必须恰好包含{ASSET_COUNT}个资产")
    if manifest.get("asset_count") != len(assets):
        raise ExportValidationError("asset_count与assets数量不一致")

    seen_ids: set[str] = set()
    normalized_assets: list[dict[str, Any]] = []
    previous_sort_key: tuple[float, int] | None = None
    for order, item in enumerate(assets, 1):
        if not isinstance(item, dict):
            raise ExportValidationError(f"assets第{order}项必须是对象")
        asset_id = _nonempty_string(item.get("asset_id"), field="assets.asset_id")
        if asset_id in seen_ids:
            raise ExportValidationError("asset_manifest含重复asset_id")
        seen_ids.add(asset_id)
        source_sha256 = _sha(item.get("source_sha256"), field=f"{asset_id}.source_sha256")
        source_name = _nonempty_string(item.get("source_name"), field=f"{asset_id}.source_name")
        year = item.get("year")
        if not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2100:
            raise ExportValidationError(f"{asset_id}.year必须是有效四位年份")
        kind = item.get("kind")
        if kind not in {"video", "courseware"}:
            raise ExportValidationError(f"{asset_id}.kind必须是video或courseware")

        catalog_position = item.get("catalog_position")
        if (not isinstance(catalog_position, (int, float))
                or isinstance(catalog_position, bool)
                or not 1 <= float(catalog_position) <= CATALOG_ENTRY_COUNT
                or not float(catalog_position * 2).is_integer()):
            raise ExportValidationError(
                f"{asset_id}.catalog_position必须是1至81之间的整数或半整数")
        catalog_suborder = item.get("catalog_suborder")
        if (not isinstance(catalog_suborder, int) or isinstance(catalog_suborder, bool)
                or catalog_suborder < 1):
            raise ExportValidationError(f"{asset_id}.catalog_suborder必须是正整数")
        coverage_positions = item.get("coverage_positions")
        if (not isinstance(coverage_positions, list) or not coverage_positions
                or any(not isinstance(value, int) or isinstance(value, bool)
                       or not 1 <= value <= CATALOG_ENTRY_COUNT
                       for value in coverage_positions)
                or coverage_positions != sorted(set(coverage_positions))):
            raise ExportValidationError(
                f"{asset_id}.coverage_positions必须是1至81的有序唯一整数数组")

        sort_key = (float(catalog_position), catalog_suborder)
        if previous_sort_key is not None and sort_key < previous_sort_key:
            raise ExportValidationError(
                "assets数组未按catalog_position、catalog_suborder顺序排列")
        previous_sort_key = sort_key
        normalized_assets.append({
            "order": order,
            "asset_id": asset_id,
            "source_sha256": source_sha256,
            "source_name": source_name,
            "year": year,
            "kind": kind,
            "catalog_position": catalog_position,
            "catalog_suborder": catalog_suborder,
            "coverage_positions": list(coverage_positions),
        })

    return {
        "catalog_entries": list(catalog),
        "asset_count": ASSET_COUNT,
        "assets": normalized_assets,
    }


def validate_display_names(
    document: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """Bind optional Word-only titles to immutable manifest identity.

    A filename is not an identity: entries use ``asset_id`` and SHA-256, while
    retaining the source name and catalog coordinates as additional audit
    guards.  This mapping never flows back into evidence or GPT inputs.
    """
    if document.get("schema_version") != DISPLAY_NAMES_SCHEMA_VERSION:
        raise ExportValidationError("Word显示名配置schema_version不正确")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ExportValidationError("Word显示名配置entries必须是数组")
    manifest_by_id = {item["asset_id"]: item for item in manifest["assets"]}
    result: dict[str, dict[str, str]] = {}
    titles: set[str] = set()
    required = {
        "asset_id", "source_sha256", "source_name", "catalog_position",
        "catalog_suborder", "display_title", "basis",
    }
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or set(entry) != required:
            raise ExportValidationError(f"Word显示名配置第{index}项字段不正确")
        asset_id = _nonempty_string(entry.get("asset_id"), field="display.asset_id")
        if asset_id in result:
            raise ExportValidationError("Word显示名配置含重复asset_id")
        asset = manifest_by_id.get(asset_id)
        if asset is None:
            raise ExportValidationError(f"Word显示名配置引用未知资产：{asset_id}")
        if _sha(entry.get("source_sha256"), field=f"{asset_id}.display.sha256") != asset["source_sha256"]:
            raise ExportValidationError(f"Word显示名SHA绑定失败：{asset_id}")
        source_name = _nonempty_string(
            entry.get("source_name"), field=f"{asset_id}.display.source_name")
        if source_name != asset["source_name"]:
            raise ExportValidationError(f"Word显示名源文件名绑定失败：{asset_id}")
        if (entry.get("catalog_position") != asset["catalog_position"]
                or entry.get("catalog_suborder") != asset["catalog_suborder"]):
            raise ExportValidationError(f"Word显示名目录坐标绑定失败：{asset_id}")
        display_title = _nonempty_string(
            entry.get("display_title"), field=f"{asset_id}.display_title").strip()
        basis = _nonempty_string(entry.get("basis"), field=f"{asset_id}.basis").strip()
        if re.search(r"[\r\n\t\x00-\x1f\x7f]", display_title + basis):
            raise ExportValidationError(f"Word显示名或依据含控制字符：{asset_id}")
        if display_title == source_name:
            raise ExportValidationError(f"Word显示名不得与原始文件名相同：{asset_id}")
        if display_title in titles:
            raise ExportValidationError(f"Word显示名配置含重复正式标题：{display_title}")
        titles.add(display_title)
        result[asset_id] = {
            "display_title": display_title,
            "basis": basis,
        }
    return result


def load_bundle(
    manifest_path: Path,
    evidence_path: Path,
    gpt_path: Path,
    display_names_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and bind all inputs without mutating any source document."""
    manifest = validate_asset_manifest(read_json(manifest_path))
    evidence = read_json(evidence_path)
    gpt = read_json(gpt_path)
    display_names = (
        validate_display_names(read_json(display_names_path), manifest)
        if display_names_path is not None else {}
    )

    evidence_by_id = _indexed_files(evidence, label="evidence_corpus")
    gpt_by_id = _indexed_files(gpt, label="gpt_final")
    model = _nonempty_string(gpt.get("model"), field="gpt_final.model")

    manifest_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for item in manifest["assets"]:
        asset_id = item["asset_id"]
        manifest_ids.add(asset_id)
        source_sha256 = item["source_sha256"]
        evidence_item = evidence_by_id.get(asset_id)
        gpt_item = gpt_by_id.get(asset_id)
        if evidence_item is None or gpt_item is None:
            raise ExportValidationError(f"三路输入资产集不一致：{asset_id}")
        if _sha(evidence_item.get("source_sha256"), field=f"{asset_id}.evidence.sha256") != source_sha256:
            raise ExportValidationError(f"ASR/OCR证据SHA绑定失败：{asset_id}")
        if _sha(gpt_item.get("source_sha256"), field=f"{asset_id}.gpt.sha256") != source_sha256:
            raise ExportValidationError(f"GPT正文SHA绑定失败：{asset_id}")
        evidence_sha256 = _sha(
            evidence_item.get("evidence_sha256"), field=f"{asset_id}.evidence.evidence_sha256")
        if (_sha(gpt_item.get("evidence_sha256"), field=f"{asset_id}.gpt.evidence_sha256")
                != evidence_sha256):
            raise ExportValidationError(f"GPT正文与本次ASR/OCR证据SHA绑定失败：{asset_id}")
        _sha(gpt_item.get("task_sha256"), field=f"{asset_id}.gpt.task_sha256")
        sections = evidence_item.get("sections")
        if not isinstance(sections, dict) or set(sections) != {ASR_SECTION, OCR_SECTION}:
            raise ExportValidationError(
                f"ASR/OCR证据只能包含两个底稿分区，不得携带旧融合稿：{asset_id}"
            )
        if set(gpt_item) != {
            "asset_id", "source_sha256", "evidence_sha256", "task_sha256", "paragraphs",
        }:
            raise ExportValidationError(
                "GPT条目只能包含身份/证据/任务绑定字段和paragraphs："
                f"{asset_id}"
            )
        asr_values = _mechanical_values(
            sections[ASR_SECTION], field=f"{asset_id}.{ASR_SECTION}")
        ocr_fragments = _mechanical_values(
            sections[OCR_SECTION], field=f"{asset_id}.{OCR_SECTION}")
        ocr_paragraphs = mechanical_ocr_paragraphs(ocr_fragments)
        # This binding is derived read-only from evidence_corpus.  It allows
        # qa_word() to prove that Word's packed OCR paragraphs expand to the
        # exact normalized source-fragment stream, with one space between every
        # adjacent fragment and with repetitions retained.
        evidence_qa = {
            "ocr_source_fragment_count": len(ocr_fragments),
            "ocr_word_paragraph_count": len(ocr_paragraphs),
            "ocr_normalized_stream_sha256": _text_stream_sha256(ocr_fragments),
        }
        if _text_stream_sha256(ocr_paragraphs) != evidence_qa["ocr_normalized_stream_sha256"]:
            raise AssertionError("OCR机械装箱未能逐字重建原始碎片流")
        record = dict(item)
        display = display_names.get(asset_id)
        record["display_title"] = (
            display["display_title"] if display is not None else item["source_name"])
        record["display_name_applied"] = display is not None
        record["display_name_basis"] = display["basis"] if display is not None else None
        record["sections"] = {
            ASR_SECTION: asr_values,
            OCR_SECTION: ocr_paragraphs,
            GPT_SECTION: _gpt_paragraphs(gpt_item["paragraphs"], asset_id=asset_id),
        }
        record["evidence_qa"] = evidence_qa
        records.append(record)

    if manifest_ids != set(evidence_by_id) or manifest_ids != set(gpt_by_id):
        raise ExportValidationError("三路输入的asset_id集合不一致")
    return {
        "catalog_entries": manifest["catalog_entries"],
        "asset_count": ASSET_COUNT,
        "model": model,
        "assets": records,
    }


def _asset_label(asset: dict[str, Any]) -> str:
    return f"[{asset['year']}] {asset['display_title']}"


def _asset_metadata(asset: dict[str, Any]) -> str:
    mapping = "、".join(str(value) for value in asset["coverage_positions"])
    text = f"对应课程目录第 {mapping} 条"
    if asset["display_name_applied"]:
        text += f"；源文件名：{asset['source_name']}"
    return text


def _font(style: Any, size: float, *, bold: bool = False, color: str | None = None) -> None:
    style.font.name = FONT_CN
    style.font.size = Pt(size)
    style.font.bold = bold
    if color:
        style.font.color.rgb = RGBColor.from_string(color)
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:eastAsia"), FONT_CN)
    fonts.set(qn("w:ascii"), "Aptos")
    fonts.set(qn("w:hAnsi"), "Aptos")


def _set_run_color(paragraph: Any, color: str) -> None:
    for run in paragraph.runs:
        if run.text:
            run.font.color.rgb = RGBColor.from_string(color)
            fonts = run._element.get_or_add_rPr().get_or_add_rFonts()
            fonts.set(qn("w:eastAsia"), FONT_CN)


def _setup(document: Document) -> None:
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(1.7), Cm(1.6)
    section.left_margin, section.right_margin = Cm(1.9), Cm(1.8)
    normal = document.styles["Normal"]
    _font(normal, 9.5)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    normal.paragraph_format.space_after = Pt(4)
    for name, size, color in (
        ("Title", 25, BLUE), ("Heading 1", 15, DARK),
        ("Heading 2", 12, BLUE), ("Subtitle", 10.5, GRAY),
    ):
        _font(document.styles[name], size, bold=name != "Subtitle", color=color)
        document.styles[name].paragraph_format.keep_with_next = True
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("第 ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    footer.add_run(" 页")


def _shade(cell: Any, color: str) -> None:
    node = OxmlElement("w:shd")
    node.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(node)


def _directory(document: Document, bundle: dict[str, Any]) -> None:
    heading = document.add_heading("简要目录（按《YW第一课线上课程目录》原顺序）", 1)
    heading.paragraph_format.page_break_before = True
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for cell, label in zip(table.rows[0].cells, ("序号", "课程目录原文", "对应资料")):
        cell.text = label
        _shade(cell, BLUE)
        for run in cell.paragraphs[0].runs:
            run.bold = True
            run.font.color.rgb = RGBColor(255, 255, 255)
    by_position: dict[int, list[str]] = defaultdict(list)
    for asset in bundle["assets"]:
        label = _asset_label(asset)
        for position in asset["coverage_positions"]:
            by_position[position].append(label)
    for index, title in enumerate(bundle["catalog_entries"], 1):
        cells = table.add_row().cells
        values = (str(index), title, "；".join(by_position[index]) or "未提供独立资料")
        for cell, value in zip(cells, values):
            cell.text = value
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(1)
                for run in paragraph.runs:
                    run.font.size = Pt(8)
        if index % 2 == 0:
            for cell in cells:
                _shade(cell, LIGHT_GRAY)


def export_word(bundle: dict[str, Any], output: Path) -> Path:
    """Write one Word file. GPT prose is the only red content."""
    output = Path(output)
    if output.suffix.lower() != ".docx":
        raise ExportValidationError("输出必须是.docx Word文件")
    document = Document()
    _setup(document)
    document.core_properties.title = "2026年YW第一课全量ASR、OCR与GPT融合"
    for _ in range(3):
        document.add_paragraph()
    title = document.add_heading("2026年YW第一课全量课程资料", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = document.add_paragraph(
        f"共 {ASSET_COUNT} 个资产 · ASR + OCR + GPT融合", style="Subtitle")
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    notice = document.add_paragraph(
        "红色仅表示GPT最终融合正文；ASR、OCR、分区标题和目录均为非红色。")
    notice.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    _directory(document, bundle)
    for asset in bundle["assets"]:
        heading = document.add_heading(
            f"{asset['order']}. {_asset_label(asset)}", 1)
        heading.paragraph_format.page_break_before = True
        metadata = document.add_paragraph(_asset_metadata(asset), style="Subtitle")
        metadata.paragraph_format.keep_with_next = True
        for section_title in SECTION_TITLES:
            section_heading = document.add_heading(section_title, 2)
            _set_run_color(section_heading, BLUE)
            for text in asset["sections"][section_title]:
                paragraph = document.add_paragraph(text)
                paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                paragraph.paragraph_format.first_line_indent = Cm(0.7)
                _set_run_color(paragraph, RED if section_title == GPT_SECTION else BLACK)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.docx")
    document.save(temporary)
    temporary.replace(output)
    return output


def _run_color(run: Any) -> str | None:
    value = run.font.color.rgb
    return str(value) if value is not None else None


def _paragraph_has_red(paragraph: Any) -> bool:
    return any(run.text and _run_color(run) == RED for run in paragraph.runs)


def _all_text_runs_are(paragraph: Any, color: str) -> bool:
    runs = [run for run in paragraph.runs if run.text]
    return bool(runs) and all(_run_color(run) == color for run in runs)


def qa_word(bundle: dict[str, Any], output: Path) -> dict[str, Any]:
    """Read-only delivery QA. This function never saves the Word document."""
    output = Path(output).resolve(strict=True)
    document = Document(output)
    if len(document.tables) != 1:
        raise ExportValidationError("Word必须且只能包含一张简要目录表")
    table = document.tables[0]
    if len(table.rows) != CATALOG_ENTRY_COUNT + 1:
        raise ExportValidationError(f"Word简要目录必须恰好包含{CATALOG_ENTRY_COUNT}行")

    by_position: dict[int, list[str]] = defaultdict(list)
    for asset in bundle["assets"]:
        label = _asset_label(asset)
        for position in asset["coverage_positions"]:
            by_position[position].append(label)
    for index, row in enumerate(table.rows[1:], 1):
        actual = tuple(cell.text for cell in row.cells)
        expected = (
            str(index), bundle["catalog_entries"][index - 1],
            "；".join(by_position[index]) or "未提供独立资料",
        )
        if actual != expected:
            raise ExportValidationError(f"Word目录第{index}行与asset_manifest不一致")
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                if _paragraph_has_red(paragraph):
                    raise ExportValidationError("GPT红色泄漏到目录")

    expected_headings = [
        f"{asset['order']}. {_asset_label(asset)}"
        for asset in bundle["assets"]
    ]
    actual_headings: list[str] = []
    observed = [{title: [] for title in SECTION_TITLES} for _ in bundle["assets"]]
    observed_section_headings: list[list[str]] = [[] for _ in bundle["assets"]]
    active_asset = -1
    active_section: str | None = None
    fusion_paragraphs = 0
    ocr_paragraphs = 0
    for paragraph in document.paragraphs:
        if paragraph.style.name == "Heading 1":
            match = re.match(r"(\d+)\. \[(\d{4})\] ", paragraph.text)
            if match:
                active_asset = int(match.group(1)) - 1
                actual_headings.append(paragraph.text)
            else:
                active_asset = -1
            active_section = None
            if _paragraph_has_red(paragraph):
                raise ExportValidationError("GPT红色泄漏到文件或目录标题")
            continue
        if paragraph.style.name == "Heading 2":
            active_section = paragraph.text if paragraph.text in SECTION_TITLES else None
            if (active_section is None or not 0 <= active_asset < ASSET_COUNT
                    or not _all_text_runs_are(paragraph, BLUE)):
                raise ExportValidationError("三段式二级标题不一致或颜色错误")
            observed_section_headings[active_asset].append(active_section)
            continue
        if active_asset >= 0 and active_section and paragraph.text:
            if not 0 <= active_asset < ASSET_COUNT:
                raise ExportValidationError("Word资产标题序号越界")
            observed[active_asset][active_section].append(paragraph.text)
            expected_color = RED if active_section == GPT_SECTION else BLACK
            if not _all_text_runs_are(paragraph, expected_color):
                if active_section == GPT_SECTION:
                    raise ExportValidationError("GPT最终正文未全部标红")
                raise ExportValidationError("GPT红色泄漏到ASR/OCR正文或底稿未标黑")
            if active_section == GPT_SECTION:
                fusion_paragraphs += 1
            elif active_section == OCR_SECTION:
                ocr_paragraphs += 1
        elif _paragraph_has_red(paragraph):
            raise ExportValidationError("GPT红色泄漏到GPT正文以外的内容")

    if actual_headings != expected_headings:
        raise ExportValidationError("Word资产标题数量或顺序不正确")
    actual_metadata = [
        paragraph.text for paragraph in document.paragraphs
        if paragraph.style.name == "Subtitle"
        and paragraph.text.startswith("对应课程目录第 ")
    ]
    expected_metadata = [_asset_metadata(asset) for asset in bundle["assets"]]
    if actual_metadata != expected_metadata:
        raise ExportValidationError("Word资产源文件追溯信息数量或顺序不正确")
    for index, asset in enumerate(bundle["assets"]):
        if observed_section_headings[index] != list(SECTION_TITLES):
            raise ExportValidationError(
                f"Word第{index + 1}个资产未严格按ASR/OCR/GPT三段式排列"
            )
        for title in SECTION_TITLES:
            expected = asset["sections"][title]
            actual = observed[index][title]
            if actual != expected:
                raise ExportValidationError(
                    f"Word第{index + 1}个资产的{title}与绑定JSON不逐字一致"
                )
        if (_text_stream_sha256(observed[index][OCR_SECTION])
                != asset["evidence_qa"]["ocr_normalized_stream_sha256"]):
            raise ExportValidationError(
                f"Word第{index + 1}个资产的OCR无法逐字重建原始碎片流")
        _assert_no_forbidden_fusion_labels(
            observed[index][GPT_SECTION], asset_id=asset["asset_id"])

    # Headers, footers and table cells are outside GPT body and therefore must
    # never contain the explicit fusion red.
    for section in document.sections:
        for container in (section.header, section.footer):
            for paragraph in container.paragraphs:
                if _paragraph_has_red(paragraph):
                    raise ExportValidationError("GPT红色泄漏到页眉或页脚")

    with ZipFile(output) as archive:
        xml = b"\n".join(
            archive.read(name) for name in archive.namelist()
            if name.lower().endswith(".xml")
        )
    if any(pattern.search(xml) for pattern in SECRET_PATTERNS):
        raise ExportValidationError("Word中存在疑似API密钥或授权信息")

    return {
        "passed": True,
        "output": str(output),
        "asset_count": len(actual_headings),
        "catalog_row_count": len(table.rows) - 1,
        "section_heading_count": sum(map(len, observed_section_headings)),
        "gpt_red_paragraph_count": fusion_paragraphs,
        "ocr_source_fragment_count": sum(
            asset["evidence_qa"]["ocr_source_fragment_count"] for asset in bundle["assets"]),
        "ocr_word_paragraph_count": ocr_paragraphs,
        "asr_verbatim": True,
        "ocr_verbatim": True,
        "ocr_fragment_separator": "single_space",
        "gpt_verbatim": True,
        "red_not_leaked": True,
        "forbidden_fusion_labels_absent": True,
        "credential_patterns_absent": True,
        "model": bundle["model"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按第一性原理导出91资产三段式Word；不生成或修改融合内容")
    parser.add_argument("--asset-manifest", type=Path, required=True)
    parser.add_argument("--evidence-corpus", type=Path, required=True)
    parser.add_argument("--gpt-final", type=Path, required=True)
    parser.add_argument(
        "--display-names", type=Path, default=DEFAULT_DISPLAY_NAMES,
        help="仅用于Word展示标题的审计配置；不改变源文件、证据或课程顺序")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qa-only", action="store_true",
                        help="只读核验现有Word，不保存任何文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bundle = load_bundle(
            args.asset_manifest.resolve(),
            args.evidence_corpus.resolve(),
            args.gpt_final.resolve(),
            args.display_names.resolve(),
        )
        output = args.output.resolve()
        if not args.qa_only:
            export_word(bundle, output)
        result = qa_word(bundle, output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ExportValidationError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
