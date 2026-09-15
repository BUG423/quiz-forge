"""Extract the prior reviewed Word into a read-only comparison reference."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document

from .runner import atomic_json


FILE_HEADING = re.compile(r"^\d+\.\s*(.+)$")


def extract(path: Path) -> dict:
    document = Document(path)
    files: list[dict] = []
    current = None
    section = None
    label = None
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        style = paragraph.style.name
        match = FILE_HEADING.match(text) if style == "Heading 1" else None
        if match:
            if current:
                files.append(current)
            current = {"source_name": match.group(1), "ocr_blocks": [], "asr_paragraphs": []}
            section = label = None
            continue
        if current is None:
            continue
        if style == "Heading 2":
            section = "ocr" if text == "OCR 结果" else "asr" if text == "ASR 结果" else None
            label = None
        elif section == "ocr" and style == "Heading 3":
            label = text
        elif section == "ocr" and text:
            current["ocr_blocks"].append({"label": label, "text": text})
            label = None
        elif section == "asr" and text:
            current["asr_paragraphs"].append(text)
    if current:
        files.append(current)
    if len(files) != 58:
        raise ValueError(f"旧报告应有58个文件，实际{len(files)}")
    if len({item["source_name"] for item in files}) != len(files):
        raise ValueError("旧报告存在重复文件标题")
    return {"source_report": str(path.resolve()), "file_count": len(files), "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取旧版Word作为复核参考")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = extract(args.source.resolve(strict=True))
    atomic_json(args.output.resolve(), result)
    print(f"EXTRACTED {len(result['files'])} files -> {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
