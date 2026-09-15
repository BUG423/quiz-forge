#!/usr/bin/env python3
"""Safely extract the four audiovisual assets embedded in supplied PPTX files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / ".work" / "embedded-office" / "2025"
MAPPINGS = (
    ("data/2025/电网员工职业发展指引.pptx", "ppt/media/media1.wmv",
     "电网员工职业发展指引_第4页_media1.wmv", 4),
    ("data/2025/电网员工职业发展指引.pptx", "ppt/media/media2.wmv",
     "电网员工职业发展指引_第48页_media2.wmv", 48),
    ("data/2025/电网员工职业发展指引.pptx", "ppt/media/media3.wmv",
     "电网员工职业发展指引_第82页_media3.wmv", 82),
    ("data/2025/网络及社交媒体保密管理-网络.pptx", "ppt/media/media1.mov",
     "网络及社交媒体保密管理_第13页_media1.mov", 13),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for parent_name, member, output_name, slide in MAPPINGS:
        parent = (ROOT / parent_name).resolve(strict=True)
        with ZipFile(parent) as archive:
            data = archive.read(member)
        target = OUTPUT / output_name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        manifest.append({
            "parent_source": str(parent),
            "parent_name": parent.name,
            "slide": slide,
            "member": member,
            "output_path": str(target.resolve()),
            "size_bytes": len(data),
            "sha256": digest(data),
        })
    manifest_path = OUTPUT.parent / "manifest.json"
    manifest_path.write_text(
        json.dumps({"file_count": len(manifest), "files": manifest}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"file_count": len(manifest), "output": str(OUTPUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
