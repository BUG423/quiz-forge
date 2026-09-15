"""Visual corroboration input and a clean, evidence-backed document export."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .report import _apply_review, _extract_persistent_texts, _ocr_groups
from .runner import atomic_json, read_json, validate_results

SECTION_TITLES = ("ASR 结果", "OCR 结果", "GPT 融合校对结果")


def representative_frames(asr: dict, ocr: dict, review: dict) -> tuple[list[dict], list[dict]]:
    """Keep each text transition; merge only adjacent identical reviewed text."""
    _, corrected, _ = _apply_review(asr, ocr, review)
    display, _ = _extract_persistent_texts(corrected, review)
    groups = _ocr_groups(display)
    originals = {frame["index"]: frame for frame in ocr["frames"]}
    selected = {}
    mapping = []
    for group in groups:
        candidates = [frame for frame in corrected["frames"]
                      if group["start"] <= frame["timestamp_seconds"] <= group["end"]]
        # A clear representative is useful for text, but no source frames are removed.
        best = max(candidates, key=lambda frame: (
            sum(line.get("confidence", 0) for line in frame["lines"]) / max(1, len(frame["lines"])),
            -frame["index"],
        ))
        index = best["index"]
        selected[index] = originals[index]
        mapping.append({"representative_frame": index, "covered_frames": [f["index"] for f in candidates],
                        "reviewed_text": group["text"]})
    # Direct-vision supplements may point to scenes where OCR detected nothing.
    for supplement in review.get("ocr_supplements", []):
        for frame in ocr["frames"]:
            if supplement["start_seconds"] <= frame["timestamp_seconds"] <= supplement["end_seconds"]:
                selected.setdefault(frame["index"], frame)
    return [selected[index] for index in sorted(selected)], mapping


def verify_evidence(content: dict, manifest: dict, asr: dict, ocr: dict, vision: dict) -> None:
    """Final copy is concise; its separate content artifact still needs valid sources."""
    if content.get("source_sha256") != manifest["sha256"]:
        raise ValueError("融合稿与视频源指纹不匹配")
    if vision.get("model") != "mimo-v2.5" or vision.get("source_sha256") != manifest["sha256"]:
        raise ValueError("缺少与本视频对应的小米视觉复核结果")
    if content.get("title") != Path(manifest["source_name"]).stem:
        raise ValueError("融合稿标题必须与源文件名一致")
    asr_ids = {segment["index"] for segment in asr["segments"]}
    ocr_ids = {frame["index"] for frame in ocr["frames"]}
    vision_ids = {frame["frame_index"] for frame in vision["frames"]}
    blocks = content.get("blocks", [])
    if not blocks:
        raise ValueError("融合正文不能为空")
    if tuple(b.get("text") for b in blocks if b.get("type") == "heading") != SECTION_TITLES:
        raise ValueError("必须依次包含 ASR、OCR、GPT 融合校对三个结果部分")
    section = None
    section_counts = dict.fromkeys(SECTION_TITLES, 0)
    visual_supplement = False
    for block in blocks:
        if block.get("type") not in {"heading", "paragraph", "table"}:
            raise ValueError("融合稿块类型不支持")
        if block["type"] == "heading":
            section = block["text"]
            continue
        if section is None:
            raise ValueError("正文必须位于结果分区内")
        section_counts[section] += 1
        evidence = block.get("evidence", {})
        speech = evidence.get("asr_segments", [])
        picture = evidence.get("ocr_frames", [])
        corroboration = evidence.get("vision_frames", [])
        if not speech and not picture:
            raise ValueError("正文事实缺少源证据")
        if section == SECTION_TITLES[0] and not speech:
            raise ValueError("ASR部分必须有语音来源，不可加入纯视觉信息")
        if section == SECTION_TITLES[1] and not picture:
            raise ValueError("OCR部分必须有画面来源，不可加入纯语音信息")
        if any(i not in asr_ids for i in speech) or any(i not in ocr_ids for i in picture) or any(i not in vision_ids for i in corroboration):
            raise ValueError("正文引用了不存在的源段/画面")
        if evidence.get("visual_only_information"):
            if not set(picture).intersection(corroboration):
                raise ValueError("画面独有补充必须有原图及小米视觉复核记录")
            if section == SECTION_TITLES[2]:
                visual_supplement = True
        if block["type"] == "paragraph":
            if not block.get("text", "").strip():
                raise ValueError("段落文本为空")
        else:
            headers, rows = block.get("headers", []), block.get("rows", [])
            if not headers or not rows or any(len(row) != len(headers) for row in rows):
                raise ValueError("人物/数据表结构不完整")
    if not all(section_counts.values()):
        raise ValueError("三个结果部分均不能为空")
    if not visual_supplement:
        raise ValueError("缺少实际画面独有信息，不能把单独ASR冒充融合稿")


def _set_font(style: Any, size: float, *, heading: bool = False) -> None:
    name = "微软雅黑" if heading else "宋体"
    style.font.name = name
    style.font.size = Pt(size)
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for script in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{script}"), name)


def export_clean_word(content: dict, output: Path) -> Path:
    """Write only user-facing prose, headings and content tables, never process data."""
    if output.suffix.lower() != ".docx":
        raise ValueError("输出必须是Word文件")
    document = Document()
    page = document.sections[0]
    page.page_width, page.page_height = Cm(21), Cm(29.7)
    page.top_margin, page.bottom_margin = Cm(1.8), Cm(1.8)
    page.left_margin, page.right_margin = Cm(2.1), Cm(2.1)
    normal = document.styles["Normal"]
    _set_font(normal, 11)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.widow_control = True
    for name, size in [("Title", 18), ("Heading 1", 12.5)]:
        style = document.styles[name]
        _set_font(style, size, heading=True)
        style.font.color.rgb = RGBColor.from_string("203746")
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(9)
        style.paragraph_format.space_after = Pt(7)
    document.core_properties.title = content["title"]
    document.add_heading(content["title"], 0)
    for index, block in enumerate(content["blocks"]):
        kind = block["type"]
        if kind == "heading":
            document.add_heading(block["text"], 1)
        elif kind == "paragraph":
            paragraph = document.add_paragraph(block["text"])
            if index and content["blocks"][index - 1]["type"] == "table":
                paragraph.paragraph_format.space_before = Pt(6)
            if index + 1 < len(content["blocks"]) and content["blocks"][index + 1]["type"] == "table":
                paragraph.paragraph_format.keep_with_next = True
        elif kind == "table":
            table = document.add_table(rows=1, cols=len(block["headers"]))
            table.style = "Table Grid"
            for cell, value in zip(table.rows[0].cells, block["headers"]):
                cell.text = value
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            repeat = OxmlElement("w:tblHeader")
            table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for row in block["rows"]:
                cells = table.add_row().cells
                for cell, value in zip(cells, row):
                    cell.text = str(value)
            for row in table.rows:
                row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.space_after = Pt(3)
                        paragraph.paragraph_format.space_before = Pt(3)
        else:
            raise ValueError("不支持的正文类型")
    footer = page.footer.paragraphs[0]
    footer.alignment = 1
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.docx")
    document.save(temporary)
    temporary.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="单视频视觉复核及ASR/OCR/GPT融合三部分Word")
    parser.add_argument("work", type=Path, help="已经完成单视频ASR/OCR的任务目录")
    parser.add_argument("--vision", action="store_true", help="调用MiMo视觉复核代表画面")
    parser.add_argument("--content", type=Path, help="已审定的融合正文JSON；证据仅存内部")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    work = args.work.resolve()
    manifest, asr, ocr, review = [read_json(work / f"{name}.json") for name in ("manifest", "asr", "ocr", "review")]
    validate_results(manifest, asr, ocr)
    source = Path(manifest["source_path"])
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError("实际源视频已发生变化")
    if args.vision:
        from .vision import review_images

        frames, mapping = representative_frames(asr, ocr, review)
        atomic_json(work / "vision_selection.json", {"source_sha256": digest, "selected_frames": [f["index"] for f in frames], "groups": mapping})
        key = os.environ.get("MIMO_API_KEY")
        if not key:
            if not sys.stdin.isatty():
                raise ValueError("需要MIMO_API_KEY环境变量或终端隐藏输入")
            key = getpass.getpass("MiMo API key (hidden): ").strip()
        result = review_images(frames, work, key, source_sha256=digest,
                               progress=lambda message: print(f"[VISION] {message}", flush=True))
        atomic_json(work / "vision.json", result)
        del key
        print(f"VISION_COMPLETE {len(frames)} representative frames", flush=True)
    if args.content:
        if not args.output:
            raise ValueError("导出需要 --output")
        content, vision = read_json(args.content), read_json(work / "vision.json")
        verify_evidence(content, manifest, asr, ocr, vision)
        export_clean_word(content, args.output)
        print(f"REPORT {args.output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
