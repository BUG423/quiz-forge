"""Assemble independently proofread lossless fusion drafts into one artifact.

The assembler is intentionally strict: every source must have exactly one
manual/GPT-reviewed replacement, every OCR-noise exclusion must be explicit,
and the complete ASR/OCR evidence is re-audited before anything is written.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from .full_fusion_qa import build_full_fusion_qa
from .runner import atomic_json, read_json


REVIEW_MODEL = "gpt-lossless-full-review"
REVIEW_SCHEMA = "full-fusion-review-v2"
DEFAULT_INPUT = Path(".work/batch-2025/fused_content.json")
DEFAULT_OUTPUT = Path(".work/batch-2025/full_fusion_review.json")
DEFAULT_QA = Path(".work/batch-2025/full_fusion_review_qa.json")


def _load_mapping(paths: Iterable[Path], *, label: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in paths:
        value = read_json(Path(path))
        if not isinstance(value, dict):
            raise ValueError(f"{label}文件顶层必须是对象：{path}")
        overlap = set(merged).intersection(value)
        if overlap:
            raise ValueError(f"{label}出现重复文件：{sorted(overlap)[0]}")
        merged.update(value)
    return merged


def assemble(base: dict, overrides: dict[str, Any],
             noise: dict[str, Any] | None = None) -> tuple[dict, dict]:
    records = base.get("files") if isinstance(base, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError("融合证据必须包含非空files数组")
    names = [record.get("source_name") for record in records]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("融合证据含无效source_name")
    if len(set(names)) != len(names):
        raise ValueError("融合证据含重复source_name")
    expected, supplied = set(names), set(overrides)
    if supplied != expected:
        missing, extra = sorted(expected - supplied), sorted(supplied - expected)
        raise ValueError(
            f"人工全文复核覆盖不完整：缺少{len(missing)}个，多出{len(extra)}个"
            + (f"；首个缺少：{missing[0]}" if missing else "")
            + (f"；首个多出：{extra[0]}" if extra else "")
        )
    noise = noise or {}
    if not isinstance(noise, dict):
        raise ValueError("OCR噪声核验清单必须是对象")
    unexpected_noise = set(noise) - expected
    if unexpected_noise:
        raise ValueError(f"OCR噪声清单含未知文件：{sorted(unexpected_noise)[0]}")

    reviewed_records = []
    qa_records = []
    for source in records:
        name = source["source_name"]
        paragraphs = overrides[name]
        if (not isinstance(paragraphs, list) or not paragraphs
                or not all(isinstance(value, str) and value.strip()
                           for value in paragraphs)):
            raise ValueError(f"人工全文复核正文无效：{name}")
        exclusions = noise.get(name, [])
        if not isinstance(exclusions, list):
            raise ValueError(f"OCR噪声核验项必须是数组：{name}")
        evidence_status = {
            "fusion_text_source": REVIEW_MODEL,
            "verified_ocr_noise_anchors": deepcopy(exclusions),
        }
        audit_record = deepcopy(source)
        sections = audit_record.get("sections")
        if not isinstance(sections, dict):
            raise ValueError(f"融合证据缺少sections：{name}")
        sections["GPT 融合校对结果"] = list(paragraphs)
        audit_record["evidence_status"] = {
            **(audit_record.get("evidence_status") or {}), **evidence_status,
        }
        qa_records.append(audit_record)
        reviewed_records.append({
            "source_name": name,
            "source_sha256": source.get("source_sha256"),
            "kind": source.get("kind"),
            "evidence_status": evidence_status,
            "sections": {"GPT 融合校对结果": list(paragraphs)},
        })

    qa = build_full_fusion_qa({"files": qa_records})
    failed = [item for item in qa["files"] if not item["passed"]]
    if failed:
        first = failed[0]
        raise ValueError(
            f"仍有{len(failed)}个文件未通过无损完整性复核；"
            f"首个：{first['source_name']}（{','.join(first['failures'])}）"
        )
    review = {
        "schema_version": REVIEW_SCHEMA,
        "model": REVIEW_MODEL,
        "review_method": "逐文件GPT无损融合、人工式证据取舍与确定性QA复核",
        "file_count": len(reviewed_records),
        "files": reviewed_records,
    }
    return review, qa


def _default_paths(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总并强制核验全部GPT无损融合校对稿")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--overrides", type=Path, nargs="*")
    parser.add_argument("--noise", type=Path, nargs="*")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--qa", type=Path, default=DEFAULT_QA)
    args = parser.parse_args(argv)
    try:
        root = args.input.resolve().parent
        override_paths = (args.overrides if args.overrides is not None
                          else _default_paths(root, "manual-filter-[a-f].json"))
        noise_paths = (args.noise if args.noise is not None else [
            path for path in [root / "verified-ocr-noise-anchors.json",
                              *_default_paths(root, "manual-filter-[d-f]-noise.json")]
            if path.is_file()
        ])
        if not override_paths:
            raise ValueError("没有找到人工全文复核稿")
        review, qa = assemble(
            read_json(args.input.resolve()),
            _load_mapping(override_paths, label="人工全文复核稿"),
            _load_mapping(noise_paths, label="OCR噪声核验清单") if noise_paths else {},
        )
        atomic_json(args.output.resolve(), review)
        atomic_json(args.qa.resolve(), qa)
        print(json.dumps({
            "file_count": review["file_count"], "passed": qa["passed"],
            "output": str(args.output.resolve()), "qa": str(args.qa.resolve()),
        }, ensure_ascii=False), flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
