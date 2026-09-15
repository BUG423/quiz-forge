#!/usr/bin/env python3
"""Read-only hard acceptance for the first-principles courseware OCR corpus.

The QA recomputes source, render, page, embedded-member and evidence facts.  It
never edits OCR JSON, page images or source documents.  A machine-readable
report is written separately and the command exits 2 when any hard contract
fails.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import posixpath
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from zipfile import ZipFile

import cv2
import numpy as np
import pymupdf
from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / ".work/first-principles-2026/courseware_ocr_lossless.json"
DEFAULT_MANIFEST = ROOT / ".work/first-principles-2026/asset_manifest.json"
DEFAULT_REPORT = ROOT / ".work/first-principles-2026/courseware_ocr_lossless_qa.json"
SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".pptx", ".docx"}
XLSX_RENDER_DEGRADED_STATUS = "native_complete_render_failed_full_page_ocr_covered"
XLSX_RENDER_DEGRADED_RISK = "embedded_xlsx_render_failed_full_page_ocr_covered"


@dataclass(frozen=True)
class Expectations:
    source_files: int = 29
    pptx_files: int = 19
    pdf_files: int = 4
    docx_files: int = 4
    image_files: int = 2
    ppt_source_pages: int = 938
    ppt_page_records: int = 938
    pdf_physical_pages: int = 75
    pdf_page_records: int = 85
    docx_physical_pages: int = 32
    docx_page_records: int = 32
    image_page_records: int = 12
    visual_records: int = 1067
    ppt_embedded_objects: int = 891
    docx_embedded_objects: int = 20
    embedded_objects: int = 911
    missing_external_unique_targets: int = 8
    null_external_relationships: int = 5
    missing_external_occurrences: int = 24


class QAError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_path(value: str | Path, root: Path = ROOT) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def record_check(
    checks: list[dict[str, Any]], name: str, passed: bool, *,
    expected: Any = None, actual: Any = None, details: Any = None,
) -> None:
    checks.append({
        "name": name, "passed": bool(passed), "expected": expected,
        "actual": actual, "details": details,
    })


def expected_manifest_sources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The catalog is the 29th OCR input but not a course-body asset."""
    try:
        catalog = manifest["catalog"]
        assets = [item for item in manifest["assets"] if item.get("kind") == "courseware"]
    except (KeyError, TypeError) as exc:
        raise QAError("asset_manifest 缺少 catalog/assets。") from exc
    output = [{
        "source_path": catalog["source_path"],
        "source_sha256": catalog["source_sha256"],
        "role": "catalog_control",
    }]
    output.extend({
        "source_path": item["source_path"],
        "source_sha256": item["source_sha256"],
        "role": "courseware",
    } for item in assets)
    return output


def directory_control_sources(root: Path = ROOT) -> list[Path]:
    roots = [
        root / "data/2025", root / "data/2026",
        root / ".work/extracted-2026/项目管理1-9",
    ]
    found = set()
    for directory in roots:
        if not directory.is_dir():
            continue
        found.update(
            path.resolve() for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED
        )
    return sorted(found)


def ppt_slide_count(path: Path) -> int:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("ppt/presentation.xml"))
    return sum(local_name(node.tag) == "sldId" for node in root.iter())


def image_slice_count(path: Path) -> int:
    image = cv2.imdecode(
        np.frombuffer(path.read_bytes(), dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        raise QAError(f"无法解码控制图片：{path}")
    height, width = image.shape[:2]
    slice_height = max(1600, round(width * 1.45))
    if height <= slice_height:
        return 1
    overlap = min(160, slice_height // 12)
    count, start = 0, 0
    while start < height:
        end = min(height, start + slice_height)
        count += 1
        if end == height:
            return count
        start = end - overlap
    raise AssertionError("unreachable")


def validate_partition(
    pages: Any, physical_pages: int, *, allow_parts: bool,
) -> list[str]:
    errors = []
    if not isinstance(pages, list):
        return ["pages 不是数组"]
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in pages:
        if not isinstance(item, dict) or not isinstance(item.get("page"), int):
            errors.append("页面记录缺少整数 page")
            continue
        grouped[item["page"]].append(item)
    expected_pages = set(range(1, physical_pages + 1))
    if set(grouped) != expected_pages:
        errors.append(f"物理页覆盖不完整：{sorted(grouped)} != {sorted(expected_pages)}")
    for page, records in sorted(grouped.items()):
        counts = {item.get("part_count") for item in records}
        if len(counts) != 1 or not all(isinstance(value, int) and value >= 1 for value in counts):
            errors.append(f"第{page}页 part_count 不一致或非法")
            continue
        count = next(iter(counts))
        parts = [item.get("part") for item in records]
        if sorted(parts) != list(range(1, count + 1)):
            errors.append(f"第{page}页分片不连续：{parts}/{count}")
        if not allow_parts and (count != 1 or len(records) != 1):
            errors.append(f"第{page}页不应产生多个分片")
    return errors


def validate_raw_lines(lines: Any) -> list[str]:
    """Validate shape only; single-character, duplicate and low score are valid."""
    if not isinstance(lines, list):
        return ["raw_ocr_lines 不是数组"]
    errors = []
    for expected_index, line in enumerate(lines):
        prefix = f"OCR行{expected_index}"
        if not isinstance(line, dict):
            errors.append(f"{prefix}不是对象")
            continue
        if line.get("index") != expected_index:
            errors.append(f"{prefix} index 不连续")
        if not isinstance(line.get("text"), str):
            errors.append(f"{prefix} text 不是字符串")
        confidence = line.get("confidence")
        if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            errors.append(f"{prefix} confidence 非法")
        box = line.get("box")
        if (not isinstance(box, list) or len(box) != 4
                or any(not isinstance(point, list) or len(point) != 2 for point in box)):
            errors.append(f"{prefix} box 不是四点框")
            continue
        for point in box:
            if any(not isinstance(value, (int, float)) or isinstance(value, bool)
                   or not math.isfinite(value) for value in point):
                errors.append(f"{prefix} box 含非法坐标")
                break
    return errors


def validate_visual_record(record: Any, label: str, root: Path = ROOT) -> list[str]:
    errors = []
    if not isinstance(record, dict):
        return [f"{label}不是对象"]
    evidence_value = record.get("evidence_path")
    if not isinstance(evidence_value, str) or not evidence_value:
        errors.append(f"{label}缺少 evidence_path")
    else:
        evidence = resolve_path(evidence_value, root)
        if not evidence.is_file():
            errors.append(f"{label}证据图不存在：{evidence}")
        else:
            digest = record.get("image_sha256")
            if not isinstance(digest, str) or sha256_file(evidence) != digest:
                errors.append(f"{label}证据图 SHA 不匹配")
    errors.extend(f"{label}{message}" for message in validate_raw_lines(record.get("raw_ocr_lines")))
    if not isinstance(record.get("native_lines"), list):
        errors.append(f"{label}缺少独立 native_lines 数组")
    for forbidden in ("reviewed_lines", "merged_lines", "fusion_lines"):
        if forbidden in record:
            errors.append(f"{label}包含禁止的融合字段 {forbidden}")
    return errors


def _workbook_cell_count(native_content: Any) -> int:
    if not isinstance(native_content, list):
        return 0
    return sum(
        len(sheet.get("cells", []))
        for sheet in native_content
        if isinstance(sheet, dict) and isinstance(sheet.get("cells"), list)
    )


def validate_xlsx_render_degradation(
    obj: dict[str, Any], file_risks: list[dict[str, Any]], *,
    source_extension: str, physical_pages: int, label: str,
) -> list[str]:
    """Accept the narrow native-complete/full-page-covered XLSX fallback."""
    if obj.get("status") != XLSX_RENDER_DEGRADED_STATUS:
        return []
    errors = []
    if obj.get("extension") != ".xlsx":
        errors.append(f"{label} 降级状态只能用于 XLSX")
    if _workbook_cell_count(obj.get("native_content")) < 1:
        errors.append(f"{label} 降级状态缺少非空原生单元格")
    if obj.get("raw_ocr_frames") != []:
        errors.append(f"{label} 独立渲染失败却含 raw_ocr_frames")
    pages = obj.get("source_pages")
    if (not isinstance(pages, list) or not pages
            or any(not isinstance(page, int) or page < 1 for page in pages)):
        errors.append(f"{label} 降级状态 source_pages 无效")
    elif source_extension == ".pptx" and any(page > physical_pages for page in pages):
        errors.append(f"{label} 降级状态 source_pages 越界")
    render_error = obj.get("render_error")
    if (not isinstance(render_error, dict)
            or not isinstance(render_error.get("exception_type"), str)
            or not render_error.get("exception_type")
            or not isinstance(render_error.get("message"), str)
            or not render_error.get("message")):
        errors.append(f"{label} 降级状态缺少结构化 render_error")
    matching = [
        risk for risk in file_risks
        if isinstance(risk, dict)
        and risk.get("code") == XLSX_RENDER_DEGRADED_RISK
        and risk.get("member") == obj.get("member")
        and risk.get("source_pages") == pages
    ]
    if len(matching) != 1:
        errors.append(f"{label} 降级状态必须且只能对应一个渲染失败风险")
    else:
        risk = matching[0]
        if (risk.get("coverage") != "source_full_page_ocr"
                or not isinstance(risk.get("exception_type"), str)
                or not risk.get("exception_type")
                or not isinstance(risk.get("exception_message"), str)
                or not risk.get("exception_message")):
            errors.append(f"{label} 渲染失败风险缺少异常或整页 OCR 覆盖声明")
    return errors


def _file_like_external(target: str) -> bool:
    if target == "NULL":
        return True
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme.lower() in {"http", "https", "mailto", "ftp"}:
        return False
    return bool(PurePosixPath(urllib.parse.unquote(target).replace("\\", "/")).suffix)


def source_external_relationships(paths: Iterable[Path]) -> list[dict[str, str]]:
    values = []
    for path in paths:
        if path.suffix.lower() not in {".pptx", ".docx"}:
            continue
        with ZipFile(path) as archive:
            for member in sorted(name for name in archive.namelist() if name.endswith(".rels")):
                root = ET.fromstring(archive.read(member))
                for node in root:
                    if node.attrib.get("TargetMode") != "External":
                        continue
                    target = node.attrib.get("Target", "")
                    if _file_like_external(target):
                        values.append({
                            "source_sha256": sha256_file(path), "relationship_part": member,
                            "relationship_id": node.attrib.get("Id", ""), "target": target,
                        })
    return values


def source_embedded_member_facts(path: Path) -> dict[str, dict[str, Any]]:
    """Independently enumerate every package member requiring custody."""
    extension = path.suffix.lower()
    prefixes = ("ppt/media/", "ppt/embeddings/") if extension == ".pptx" else (
        ("word/media/",) if extension == ".docx" else ()
    )
    if not prefixes:
        return {}
    with ZipFile(path) as archive:
        output = {}
        for member in sorted(
            name for name in archive.namelist()
            if not name.endswith("/") and any(name.startswith(prefix) for prefix in prefixes)
        ):
            payload = archive.read(member)
            frames = None
            if Path(member).suffix.lower() == ".gif":
                with Image.open(io.BytesIO(payload)) as image:
                    frames = image.n_frames
            output[member] = {
                "member_sha256": sha256_bytes(payload),
                "size_bytes": len(payload),
                "extension": Path(member).suffix.lower(),
                "gif_frames": frames,
            }
        return output


def _risk_keys(document: dict[str, Any]) -> list[dict[str, Any]]:
    risks = document.get("risks", [])
    return risks if isinstance(risks, list) else []


def run_qa(
    courseware: dict[str, Any], manifest: dict[str, Any], *,
    root: Path = ROOT, expectations: Expectations = Expectations(),
    progress: Any = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    try:
        manifest_sources = expected_manifest_sources(manifest)
    except Exception as exc:
        manifest_sources = []
        failures.append(str(exc))
    directory_sources = directory_control_sources(root)
    record_check(checks, "manifest_input_count", len(manifest_sources) == expectations.source_files,
                 expected=expectations.source_files, actual=len(manifest_sources))
    record_check(checks, "directory_input_count", len(directory_sources) == expectations.source_files,
                 expected=expectations.source_files, actual=len(directory_sources))

    manifest_by_path = {resolve_path(item["source_path"], root): item for item in manifest_sources}
    directory_set = set(directory_sources)
    record_check(checks, "manifest_directory_control_match", set(manifest_by_path) == directory_set,
                 expected=sorted(map(str, manifest_by_path)), actual=sorted(map(str, directory_set)))
    source_hash_errors = []
    for path, item in manifest_by_path.items():
        if not path.is_file():
            source_hash_errors.append(f"源文件不存在：{path}")
        elif sha256_file(path) != item.get("source_sha256"):
            source_hash_errors.append(f"manifest SHA 不匹配：{path}")
    record_check(checks, "manifest_source_sha", not source_hash_errors,
                 expected="all match", actual=len(source_hash_errors), details=source_hash_errors)

    files = courseware.get("files")
    if not isinstance(files, list):
        files = []
        failures.append("courseware 根层 files 不是数组")
    output_by_sha = {item.get("source_sha256"): item for item in files if isinstance(item, dict)}
    expected_shas = {item["source_sha256"] for item in manifest_sources}
    record_check(checks, "output_file_count", len(files) == expectations.source_files,
                 expected=expectations.source_files, actual=len(files))
    record_check(checks, "output_source_sha_set", set(output_by_sha) == expected_shas,
                 expected=sorted(expected_shas), actual=sorted(str(value) for value in output_by_sha))

    extensions = Counter(path.suffix.lower() for path in directory_sources)
    type_expected = {
        ".pptx": expectations.pptx_files, ".pdf": expectations.pdf_files,
        ".docx": expectations.docx_files,
        "images": expectations.image_files,
    }
    type_actual = {
        ".pptx": extensions[".pptx"], ".pdf": extensions[".pdf"],
        ".docx": extensions[".docx"],
        "images": extensions[".png"] + extensions[".jpg"] + extensions[".jpeg"],
    }
    record_check(checks, "source_type_counts", type_actual == type_expected,
                 expected=type_expected, actual=type_actual)

    ppt_source_pages = ppt_records = pdf_physical = pdf_records = 0
    doc_physical = doc_records = image_records = 0
    all_visual_errors = []
    object_errors = []
    object_count = 0
    ppt_object_count = 0
    docx_object_count = 0
    actual_source_paths = []
    speed_record = None

    for source_index, source in enumerate(directory_sources, 1):
        if progress is not None:
            progress(f"[{source_index}/{len(directory_sources)}] {source.name}")
        source_sha = sha256_file(source)
        item = output_by_sha.get(source_sha)
        if not isinstance(item, dict):
            continue
        actual_source_paths.append(source)
        if resolve_path(item.get("source_path", ""), root) != source:
            failures.append(f"输出 source_path 不对应 manifest：{source}")
        if item.get("source") != source.name:
            failures.append(f"输出 source 名称不匹配：{source}")
        pages = item.get("pages", [])
        extension = source.suffix.lower()
        physical = 0
        allow_parts = False
        if extension == ".pptx":
            physical = ppt_slide_count(source)
            ppt_source_pages += physical
            ppt_records += len(pages) if isinstance(pages, list) else 0
            render = item.get("render", {})
            render_path = resolve_path(render.get("path", ""), root)
            if not render_path.is_file():
                failures.append(f"PPT 渲染 PDF 不存在：{source}")
            else:
                if sha256_file(render_path) != render.get("sha256"):
                    failures.append(f"PPT 渲染 SHA 不匹配：{source}")
                with pymupdf.open(render_path) as document:
                    rendered_pages = len(document)
                if render.get("page_count") != rendered_pages:
                    failures.append(f"PPT render.page_count 不匹配：{source}")
                expected_rendered = 47 if source.name == "南网在线“速捷电”品牌系列功能建设介绍.pptx" else physical
                if rendered_pages != expected_rendered:
                    failures.append(
                        f"PPT 渲染物理页异常：{source}: {rendered_pages}/{expected_rendered}"
                    )
        elif extension == ".pdf":
            with pymupdf.open(source) as document:
                physical = len(document)
            pdf_physical += physical
            pdf_records += len(pages) if isinstance(pages, list) else 0
            allow_parts = True
        elif extension == ".docx":
            render = item.get("render", {})
            render_path = resolve_path(render.get("path", ""), root)
            if not render_path.is_file():
                failures.append(f"DOCX 渲染 PDF 不存在：{source}")
                physical = int(render.get("page_count", 0) or 0)
            else:
                if sha256_file(render_path) != render.get("sha256"):
                    failures.append(f"DOCX 渲染 SHA 不匹配：{source}")
                with pymupdf.open(render_path) as document:
                    physical = len(document)
                if render.get("page_count") != physical:
                    failures.append(f"DOCX render.page_count 不匹配：{source}")
            doc_physical += physical
            doc_records += len(pages) if isinstance(pages, list) else 0
            if not isinstance(item.get("native_document_text"), list):
                failures.append(f"DOCX 缺少独立 native_document_text：{source}")
        else:
            physical = 1
            image_records += len(pages) if isinstance(pages, list) else 0
            expected_slices = image_slice_count(source)
            if len(pages) != expected_slices:
                failures.append(f"图片分片数错误：{source}: {len(pages)}/{expected_slices}")
            allow_parts = True
        partition_errors = validate_partition(pages, physical, allow_parts=allow_parts)
        all_visual_errors.extend(f"{source.name}: {error}" for error in partition_errors)
        for index, page in enumerate(pages if isinstance(pages, list) else []):
            all_visual_errors.extend(validate_visual_record(
                page, f"{source.name}/record-{index}: ", root,
            ))
        if source.name == "南网在线“速捷电”品牌系列功能建设介绍.pptx":
            matches = [page for page in pages if page.get("page") == 48]
            speed_record = matches[0] if len(matches) == 1 else None

        file_risks = item.get("risks", []) if isinstance(item.get("risks"), list) else []
        if any(risk.get("code") == "embedded_object_page_unmapped"
               for risk in file_risks if isinstance(risk, dict)):
            object_errors.append(f"{source.name} 含已废弃且含义不明的 embedded_object_page_unmapped 风险")
        objects = item.get("embedded_objects", []) if isinstance(item.get("embedded_objects"), list) else []
        member_facts: dict[str, dict[str, Any]] = {}
        if extension in {".pptx", ".docx"}:
            try:
                member_facts = source_embedded_member_facts(source)
            except (KeyError, OSError, ValueError) as exc:
                object_errors.append(f"{source.name} 无法建立内嵌成员控制表：{exc}")
            actual_members = [obj.get("member") for obj in objects if isinstance(obj, dict)]
            duplicates = sorted(
                member for member, count in Counter(actual_members).items()
                if isinstance(member, str) and count > 1
            )
            missing = sorted(set(member_facts) - set(actual_members))
            extras = sorted(set(actual_members) - set(member_facts))
            if duplicates:
                object_errors.append(f"{source.name} 内嵌成员重复：{duplicates}")
            if missing:
                object_errors.append(f"{source.name} 内嵌成员缺失：{missing}")
            if extras:
                object_errors.append(f"{source.name} 输出了源包不存在的内嵌成员：{extras}")
        for obj in objects:
            object_count += 1
            if extension == ".pptx":
                ppt_object_count += 1
            elif extension == ".docx":
                docx_object_count += 1
            label = f"{source.name}/{obj.get('member')}"
            if not isinstance(obj.get("status"), str) or not obj["status"]:
                object_errors.append(f"{label} 缺少状态")
            if not isinstance(obj.get("source_pages"), list):
                object_errors.append(f"{label} 缺少 source_pages")
            elif not obj["source_pages"]:
                permitted = {"orphan_embedded_member", "docx_object_page_unmapped"}
                if not any(risk.get("code") in permitted and risk.get("member") == obj.get("member")
                           for risk in file_risks):
                    object_errors.append(f"{label} 无来源页且没有显式风险")
            elif extension == ".pptx" and any(
                    not isinstance(page, int) or not 1 <= page <= physical
                    for page in obj["source_pages"]):
                object_errors.append(f"{label} source_pages 越界")
            member = obj.get("member")
            if extension in {".pptx", ".docx"} and isinstance(member, str):
                facts = member_facts.get(member)
                if facts is None:
                    object_errors.append(f"{label} 无法从源包读取")
                else:
                    if facts["member_sha256"] != obj.get("member_sha256"):
                        object_errors.append(f"{label} 成员 SHA 不匹配")
                    if facts["size_bytes"] != obj.get("size_bytes"):
                        object_errors.append(f"{label} 成员大小不匹配")
                    if facts["extension"] != obj.get("extension"):
                        object_errors.append(f"{label} 成员扩展名不匹配")
                    if (facts["gif_frames"] is not None
                            and len(obj.get("raw_ocr_frames", [])) != facts["gif_frames"]):
                        object_errors.append(f"{label} GIF 帧不完整")
            for frame_index, frame in enumerate(obj.get("raw_ocr_frames", [])):
                object_errors.extend(validate_visual_record(
                    frame, f"{label}/frame-{frame_index}: ", root,
                ))
            if obj.get("extension") in {".xlsx", ".bin"} and not isinstance(
                    obj.get("native_content"), list):
                object_errors.append(f"{label} 缺少工作簿/OLE native_content")
            if obj.get("status") == "source_member_empty_unreadable":
                matching_empty = [
                    risk for risk in file_risks
                    if isinstance(risk, dict)
                    and risk.get("code") == "empty_embedded_member"
                    and risk.get("member") == member
                    and risk.get("member_sha256") == obj.get("member_sha256")
                ]
                if (obj.get("size_bytes") != 0 or obj.get("raw_ocr_frames") != []
                        or len(matching_empty) != 1):
                    object_errors.append(f"{label} 空成员状态缺少零字节事实或唯一显式风险")
            object_errors.extend(validate_xlsx_render_degradation(
                obj, file_risks, source_extension=extension,
                physical_pages=physical, label=label,
            ))
        file_errors = item.get("errors")
        if not isinstance(file_errors, list) or file_errors:
            failures.append(f"文件 errors 非空或非法：{source}: {file_errors}")
        if item.get("processing_complete") is not True:
            failures.append(f"文件未完成：{source}")

    visual_total = ppt_records + pdf_records + doc_records + image_records
    record_check(
        checks, "declared_page_record_count",
        courseware.get("page_record_count") == visual_total,
        expected=visual_total, actual=courseware.get("page_record_count"),
    )
    totals = [
        ("ppt_source_pages", ppt_source_pages, expectations.ppt_source_pages),
        ("ppt_page_records", ppt_records, expectations.ppt_page_records),
        ("pdf_physical_pages", pdf_physical, expectations.pdf_physical_pages),
        ("pdf_page_records", pdf_records, expectations.pdf_page_records),
        ("docx_physical_pages", doc_physical, expectations.docx_physical_pages),
        ("docx_page_records", doc_records, expectations.docx_page_records),
        ("image_page_records", image_records, expectations.image_page_records),
        ("visual_record_count", visual_total, expectations.visual_records),
    ]
    for name, actual, expected in totals:
        record_check(checks, name, actual == expected, expected=expected, actual=actual)
    record_check(checks, "visual_evidence_integrity", not all_visual_errors,
                 expected="all valid", actual=len(all_visual_errors), details=all_visual_errors[:200])
    object_count_actual = {
        "pptx": ppt_object_count, "docx": docx_object_count, "total": object_count,
    }
    object_count_expected = {
        "pptx": expectations.ppt_embedded_objects,
        "docx": expectations.docx_embedded_objects,
        "total": expectations.embedded_objects,
    }
    record_check(checks, "embedded_object_count", object_count_actual == object_count_expected,
                 expected=object_count_expected, actual=object_count_actual)
    record_check(checks, "embedded_object_integrity", not object_errors,
                 expected="all valid", actual=len(object_errors), details=object_errors[:200])

    speed_errors = []
    if speed_record is None:
        speed_errors.append("速捷电第48页记录缺失或重复")
    else:
        native = "".join(line.get("text", "") for line in speed_record.get("native_lines", []))
        ocr = "".join(line.get("text", "") for line in speed_record.get("raw_ocr_lines", []))
        for needle in ("介绍完毕", "谢谢"):
            if needle not in native:
                speed_errors.append(f"第48页 native 未命中 {needle}")
            if needle not in ocr:
                speed_errors.append(f"第48页 OCR 未命中 {needle}")
    record_check(checks, "sujiedian_slide_48", not speed_errors,
                 expected="native+OCR contain 介绍完毕/谢谢", actual=speed_errors)

    external = source_external_relationships(directory_sources)
    non_null_targets = {item["target"] for item in external if item["target"] != "NULL"}
    null_count = sum(item["target"] == "NULL" for item in external)
    external_expected_ok = (
        len(non_null_targets) == expectations.missing_external_unique_targets
        and null_count == expectations.null_external_relationships
        and len(external) == expectations.missing_external_occurrences
    )
    record_check(checks, "source_missing_external_control", external_expected_ok,
                 expected={"unique_missing": expectations.missing_external_unique_targets,
                           "null": expectations.null_external_relationships,
                           "occurrences": expectations.missing_external_occurrences},
                 actual={"unique_missing": len(non_null_targets), "null": null_count,
                         "occurrences": len(external)})
    output_missing = [risk for risk in _risk_keys(courseware)
                      if risk.get("code") == "missing_external_asset"]
    source_sha_by_path = {str(path): sha256_file(path) for path in directory_sources}
    expected_relations = {
        (item["source_sha256"], item["relationship_part"], item["relationship_id"], item["target"])
        for item in external
    }
    output_relations = set()
    for risk in output_missing:
        source_value = risk.get("source")
        try:
            source_path = str(resolve_path(source_value, root))
        except (TypeError, ValueError):
            source_path = ""
        relation_part = risk.get("relationship_part")
        if isinstance(relation_part, str) and not relation_part.endswith(".rels"):
            parent, name = posixpath.split(relation_part)
            relation_part = posixpath.join(parent, "_rels", name + ".rels")
        output_relations.add((
            source_sha_by_path.get(source_path), relation_part,
            risk.get("relationship_id"), risk.get("target"),
        ))
    output_non_null = {item[3] for item in output_relations if item[3] != "NULL"}
    output_null = sum(item[3] == "NULL" for item in output_relations)
    risk_ok = output_relations == expected_relations
    record_check(checks, "missing_external_risks_recorded", risk_ok,
                 expected={"targets": sorted(non_null_targets), "null": null_count,
                           "occurrences": len(external)},
                 actual={"targets": sorted(str(value) for value in output_non_null),
                         "null": output_null, "unique_relations": len(output_relations),
                         "raw_risk_records": len(output_missing)})

    root_errors = courseware.get("errors")
    no_errors = isinstance(root_errors, list) and not root_errors
    record_check(checks, "generator_errors_empty", no_errors,
                 expected=[], actual=root_errors)
    record_check(checks, "generator_declares_complete",
                 courseware.get("processing_complete") is True,
                 expected=True, actual=courseware.get("processing_complete"))

    failures.extend(
        f"{check['name']}: expected={check['expected']!r}, actual={check['actual']!r}"
        for check in checks if not check["passed"]
    )
    return {
        "schema_version": "courseware-lossless-qa/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "read_only": True,
        "expectations": asdict(expectations),
        "passed": not failures,
        "check_count": len(checks),
        "passed_check_count": sum(check["passed"] for check in checks),
        "failed_check_count": sum(not check["passed"] for check in checks),
        "checks": checks,
        "failures": failures[:500],
        "observed": {
            "manifest_sources": len(manifest_sources),
            "directory_sources": len(directory_sources),
            "output_files": len(files),
            "ppt_source_pages": ppt_source_pages,
            "visual_records": visual_total,
            "embedded_objects": object_count,
            "missing_external_occurrences": len(external),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读硬验收全量课件无损 OCR")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        courseware = json.loads(args.input.read_text(encoding="utf-8"))
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        report = run_qa(courseware, manifest, progress=lambda message: print(message, flush=True))
    except Exception as exc:
        report = {
            "schema_version": "courseware-lossless-qa/v1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "read_only": True, "passed": False, "checks": [],
            "failures": [f"QA 执行失败：{exc}"],
        }
    atomic_json(args.report, report)
    print(
        f"课件无损 OCR QA：{'通过' if report['passed'] else '拒绝'}；"
        f"检查 {len(report.get('checks', []))}；失败 {len(report.get('failures', []))}；"
        f"报告 {args.report}",
        flush=True,
    )
    if not report["passed"]:
        for failure in report.get("failures", [])[:20]:
            print(f"- {failure}", flush=True)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
