#!/usr/bin/env python3
"""Build a provenance-bound ASR/OCR evidence corpus without generated prose.

The strict artifact is emitted only when courseware OCR, supplemental scene
OCR, and blank-MiMo-segment review are all complete.  ``--allow-incomplete``
is intentionally routed to a differently named debug artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".work" / "first-principles-2026"
DEFAULT_MANIFEST = WORK / "asset_manifest.json"
DEFAULT_ASR_OCR_ROOT = ROOT / ".work" / "single-video"
DEFAULT_EMBEDDED_MANIFEST = ROOT / ".work" / "embedded-office" / "manifest.json"
DEFAULT_SCENE_OCR = WORK / "video_scene_ocr.json"
DEFAULT_COURSEWARE_OCR = WORK / "courseware_ocr_lossless.json"
DEFAULT_BLANK_REVIEW = WORK / "blank_asr_review.json"
DEFAULT_BLANK_ADJUDICATION = WORK / "mimo_blank_segment_patch.json"
DEFAULT_OUTPUT = WORK / "evidence_corpus.json"
DEFAULT_DEBUG_OUTPUT = WORK / "evidence_corpus.incomplete.json"
MIMO_MODEL = "mimo-v2.5-asr"
MIMO_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
FORBIDDEN_GENERATED_KEYS = frozenset({
    "paragraphs", "gpt", "gpt_result", "gpt_final", "gpt_fusion",
    "gpt_fused", "fusion_text", "fused_content", "generated_prose",
    "gpt 融合校对结果",
})


class EvidenceError(RuntimeError):
    pass


class IncompleteEvidenceError(EvidenceError):
    def __init__(self, blockers: list[dict[str, Any]]) -> None:
        self.blockers = blockers
        super().__init__("证据依赖未完成：" + "；".join(item["message"] for item in blockers))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"无法读取有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"JSON 顶层必须是对象：{path}")
    return value


def optional_json(path: Path) -> dict[str, Any] | None:
    return read_json(path) if path.is_file() else None


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def asr_corpus_sha256(documents: dict[str, dict[str, Any]]) -> str:
    entries = [
        {"source_sha256": source_sha, "asr_sha256": canonical_sha256(documents[source_sha])}
        for source_sha in sorted(documents)
    ]
    if not entries:
        raise EvidenceError("MiMo ASR 语料为空，无法建立输入绑定")
    return canonical_sha256(entries)


def validate_completed_mimo_patches(
    document: dict[str, Any] | None,
    asr_documents: dict[str, dict[str, Any]],
    expected_asr_corpus_sha256: str,
) -> tuple[dict[str, dict[int, dict[str, Any]]], set[str]]:
    """Validate and sanitize a complete MiMo patch without local-ASR material."""
    if document is None or document.get("complete") is not True:
        return {}, set()
    # A previously complete patch becomes an ordinary stale dependency when
    # any current ASR document changes; readiness will refuse it and request a
    # fresh review/patch rather than interpreting old coordinates.
    if document.get("asr_corpus_sha256") != expected_asr_corpus_sha256:
        return {}, set()
    if (document.get("endpoint") != MIMO_ENDPOINT
            or document.get("model") != MIMO_MODEL
            or document.get("original_asr_files_modified") is not False
            or document.get("errors") != []):
        raise EvidenceError("完成态 MiMo 空段补丁的 endpoint/model/ASR绑定或错误状态无效")
    approved = document.get("approved_review_ids")
    patches = document.get("patches")
    if (not isinstance(approved, list) or not all(isinstance(item, str) and item for item in approved)
            or len(approved) != len(set(approved)) or not isinstance(patches, list)):
        raise EvidenceError("完成态 MiMo 空段补丁的批准列表或 patches 结构无效")
    if (document.get("selected_original_segment_count") != len(approved)
            or document.get("completed_original_segment_count") != len(approved)
            or document.get("execution_requested") is not bool(approved)
            or len(patches) != len(approved)):
        raise EvidenceError("完成态 MiMo 空段补丁的批准/选择/完成计数不一致")

    by_source: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    completed_ids: set[str] = set()
    for patch_order, patch in enumerate(patches):
        if not isinstance(patch, dict):
            raise EvidenceError(f"MiMo patch[{patch_order}] 必须是对象")
        review_id = patch.get("review_id")
        if review_id not in approved or review_id in completed_ids:
            raise EvidenceError(f"MiMo patch review_id 未批准或重复：{review_id}")
        if patch.get("complete") is not True or patch.get("errors") != []:
            raise EvidenceError(f"MiMo patch 未完整成功：{review_id}")
        source_sha = valid_sha(patch.get("source_sha256"), f"patch[{patch_order}].source_sha256")
        original = asr_documents.get(source_sha)
        if original is None:
            raise EvidenceError(f"MiMo patch 无法绑定当前 ASR 源：{source_sha}")
        original_index = patch.get("original_segment_index")
        if not isinstance(original_index, int) or original_index in by_source[source_sha]:
            raise EvidenceError(f"MiMo patch 原 segment index 无效或重复：{review_id}")
        matches = [segment for segment in original.get("segments", [])
                   if isinstance(segment, dict) and segment.get("index") == original_index]
        if len(matches) != 1:
            raise EvidenceError(f"MiMo patch 原 segment index 无法唯一匹配：{review_id}")
        original_segment = matches[0]
        start, end = patch.get("original_sample_start"), patch.get("original_sample_end")
        if (not isinstance(start, int) or not isinstance(end, int) or end <= start
                or original_segment.get("sample_start") != start
                or original_segment.get("sample_end") != end
                or patch.get("original_text") != original_segment.get("text")
                or str(original_segment.get("text", "")).strip()):
            raise EvidenceError(f"MiMo patch 与原空段 index/PCM/text 不精确匹配：{review_id}")

        children = patch.get("children")
        coverage = patch.get("coverage")
        if not isinstance(children, list) or not children or not isinstance(coverage, dict):
            raise EvidenceError(f"MiMo patch 子段或 coverage 缺失：{review_id}")
        sanitized_children = []
        cursor = start
        for child_index, child in enumerate(children):
            if not isinstance(child, dict) or child.get("index") != child_index:
                raise EvidenceError(f"MiMo patch 子段 index 无效：{review_id}/{child_index}")
            child_start, child_end = child.get("sample_start"), child.get("sample_end")
            request_sha = child.get("request_id_sha256")
            request_digest = child.get("request_id_digest")
            if (not isinstance(child_start, int) or not isinstance(child_end, int)
                    or child_start != cursor or child_end <= child_start or child_end > end
                    or child.get("sample_count") != child_end - child_start
                    or child.get("model") != MIMO_MODEL
                    or child.get("finish_reason") != "stop"
                    or not isinstance(child.get("text"), str)
                    or not isinstance(child.get("request_count"), int) or child["request_count"] < 1
                    or not isinstance(request_sha, str) or SHA_RE.fullmatch(request_sha) is None
                    or not isinstance(request_digest, str) or request_digest != request_sha[:24]
                    or not isinstance(child.get("content_filter_split"), bool)):
                raise EvidenceError(f"MiMo patch 子段身份/响应/覆盖无效：{review_id}/{child_index}")
            sanitized_children.append({
                "index": child_index,
                "sample_start": child_start,
                "sample_end": child_end,
                "sample_count": child_end - child_start,
                "start_seconds": child.get("start_seconds"),
                "end_seconds": child.get("end_seconds"),
                "duration_seconds": child.get("duration_seconds"),
                "text": child["text"],
                "finish_reason": "stop",
                "model": MIMO_MODEL,
                "request_count": child["request_count"],
                "request_id_sha256": request_sha,
                "request_id_digest": request_digest,
                "content_filter_split": child["content_filter_split"],
            })
            cursor = child_end
        expected_text = "\n".join(
            child["text"].strip() for child in sanitized_children if child["text"].strip()
        )
        covered = sum(child["sample_count"] for child in sanitized_children)
        if (cursor != end or patch.get("new_mimo_text") != expected_text
                or coverage.get("complete") is not True
                or coverage.get("expected_sample_count") != end - start
                or coverage.get("covered_sample_count") != covered
                or coverage.get("child_count") != len(children)
                or coverage.get("completed_child_count") != len(children)
                or covered != end - start):
            raise EvidenceError(f"MiMo patch 未连续完整覆盖原空段：{review_id}")
        by_source[source_sha][original_index] = {
            "review_id": review_id,
            "source_sha256": source_sha,
            "original_segment_index": original_index,
            "original_sample_start": start,
            "original_sample_end": end,
            "original_text": patch["original_text"],
            "new_mimo_text": expected_text,
            "children": sanitized_children,
            "coverage": {
                "complete": True,
                "expected_sample_count": end - start,
                "covered_sample_count": covered,
                "child_count": len(children),
                "completed_child_count": len(children),
            },
        }
        completed_ids.add(review_id)
    if completed_ids != set(approved):
        raise EvidenceError("完成态 MiMo patches 未精确覆盖全部 approved_review_ids")
    return dict(by_source), completed_ids


def valid_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
        raise EvidenceError(f"{field} 不是 64 位 SHA-256")
    return value.lower()


def resolve_path(value: str, root: Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def assert_no_generated_fields(value: Any, location: str = "root") -> None:
    """The evidence layer may not acquire any generated-prose field."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().casefold()
            if normalized in FORBIDDEN_GENERATED_KEYS or "gpt" in normalized:
                raise EvidenceError(f"证据层禁止生成式字段：{location}.{key}")
            assert_no_generated_fields(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_generated_fields(child, f"{location}[{index}]")


def load_assets(path: Path, *, expected_assets: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_json(path)
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) != expected_assets:
        actual = len(assets) if isinstance(assets, list) else "invalid"
        raise EvidenceError(f"权威资产必须恰好 {expected_assets} 条，实际 {actual}")
    ids, shas = [], []
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise EvidenceError(f"assets[{index}] 必须是对象")
        ids.append(str(asset.get("asset_id", "")))
        shas.append(valid_sha(asset.get("source_sha256"), f"assets[{index}].source_sha256"))
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise EvidenceError("asset_id 缺失或重复")
    if len(shas) != len(set(shas)):
        raise EvidenceError("课程资产 source_sha256 重复")
    return manifest, assets


def source_bindings(
    assets: list[dict[str, Any]], embedded_manifest: dict[str, Any], root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Bind 63 videos directly and four embedded media to their parent assets."""
    video_sources: dict[str, dict[str, Any]] = {}
    assets_by_path: dict[Path, dict[str, Any]] = {}
    for asset in assets:
        source_path = resolve_path(asset["source_path"], root)
        assets_by_path[source_path] = asset
        if asset.get("kind") == "video":
            source_sha = asset["source_sha256"]
            video_sources[source_sha] = {
                "source_sha256": source_sha,
                "source_path": source_path,
                "asset_id": asset["asset_id"],
                "source_role": "course_video",
                "parent_asset_id": None,
            }

    embedded_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    files = embedded_manifest.get("files")
    if not isinstance(files, list):
        raise EvidenceError("embedded manifest.files 必须是数组")
    for order, item in enumerate(files):
        if not isinstance(item, dict):
            raise EvidenceError(f"embedded files[{order}] 必须是对象")
        parent_path = resolve_path(item["parent_source"], root)
        parent = assets_by_path.get(parent_path)
        if parent is None or parent.get("kind") != "courseware":
            raise EvidenceError(f"内嵌媒体父课件无法绑定：{parent_path}")
        source_sha = valid_sha(item.get("sha256"), f"embedded[{order}].sha256")
        binding = {
            "source_sha256": source_sha,
            "source_path": resolve_path(item["output_path"], root),
            "asset_id": parent["asset_id"],
            "source_role": "embedded_media",
            "parent_asset_id": parent["asset_id"],
            "parent_source_path": parent_path,
            "slide": item.get("slide"),
            "member": item.get("member"),
            "manifest_order": order,
        }
        if source_sha in video_sources:
            raise EvidenceError(f"视频源 SHA 重复：{source_sha}")
        video_sources[source_sha] = binding
        embedded_by_parent[parent["asset_id"]].append(binding)
    return video_sources, embedded_by_parent


def assess_readiness(
    courseware: dict[str, Any] | None,
    scene: dict[str, Any] | None,
    blank_review: dict[str, Any] | None,
    blank_adjudication: dict[str, Any] | None,
    *,
    expected_courseware_inputs: int,
    expected_video_sources: int,
    expected_asr_corpus_sha256: str,
    validated_patch_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    if courseware is None:
        course_count = 0
        course_complete = False
    else:
        files = courseware.get("files")
        risks = courseware.get("risks")
        course_count = len(files) if isinstance(files, list) else 0
        missing_external_recorded = bool(
            isinstance(risks, list)
            and any(isinstance(item, dict) and item.get("code") == "missing_external_asset"
                    for item in risks)
        )
        source_complete = courseware.get("source_complete")
        # Known broken external OOXML relationships are source limitations, not
        # OCR failures.  They are accepted only when the generator explicitly
        # records them as risks; a false source_complete may never be silent.
        source_state_valid = (
            source_complete is True
            or (source_complete is False and missing_external_recorded)
        )
        course_complete = bool(
            courseware.get("expected_file_count") == expected_courseware_inputs
            and courseware.get("selected_file_count") == expected_courseware_inputs
            and courseware.get("processed_file_count") == expected_courseware_inputs
            and course_count == expected_courseware_inputs
            and courseware.get("processing_complete") is True
            and source_state_valid
            and not courseware.get("errors")
            and all(isinstance(item, dict) and item.get("processing_complete") is True for item in files)
        )
    details["courseware"] = {
        "required": expected_courseware_inputs,
        "available": course_count,
        "complete": course_complete,
        "source_complete": courseware.get("source_complete") if courseware else None,
        "missing_external_asset_risk_recorded": (
            missing_external_recorded if courseware is not None else False
        ),
    }
    if not course_complete:
        blockers.append({"code": "courseware_ocr_incomplete",
                         "message": f"课件无损 OCR 未满 {expected_courseware_inputs} 份（当前 {course_count}）"})

    if scene is None:
        scene_completed = 0
        scene_complete = False
    else:
        sources = scene.get("sources")
        scene_completed = len(sources) if isinstance(sources, list) else 0
        scene_complete = bool(
            scene.get("status") == "complete"
            and scene.get("source_count") == expected_video_sources
            and scene.get("completed_source_count") == expected_video_sources
            and scene_completed == expected_video_sources
            and scene.get("failed_source_count") == 0
            and scene.get("coverage_verified") is True
            and not scene.get("failures")
        )
    details["scene_ocr"] = {"required": expected_video_sources,
                            "available": scene_completed, "complete": scene_complete}
    if not scene_complete:
        blockers.append({"code": "video_scene_ocr_incomplete",
                         "message": f"视频场景补帧 OCR 未完成（当前 {scene_completed}/{expected_video_sources}）"})

    if blank_review is None:
        needs_manual = candidate_rerun_count = None
        review_binding = None
    else:
        summary = blank_review.get("summary", {})
        judgments = summary.get("judgments", {})
        needs_manual = judgments.get("needs_manual")
        queue = blank_review.get("mimo_resegmentation_rerun_queue")
        if not isinstance(queue, list):
            raise EvidenceError("blank review 重跑清单必须是数组")
        candidate_rerun_count = len(queue)
        review_binding = blank_review.get("asr_corpus_sha256")
        reviews = blank_review.get("reviews")
        if not isinstance(reviews, list):
            raise EvidenceError("blank review.reviews 必须是数组")
        queued = {item.get("review_id") for item in queue}
        detected = {item.get("review_id") for item in reviews
                    if item.get("judgment") == "speech_detected"}
        if queued != detected or any(item.get("judgment") != "speech_detected" for item in queue):
            raise EvidenceError("blank review 的 MiMo 重跑清单与 speech_detected 不一致")
    details["blank_asr_review"] = {
        "needs_manual_candidates": needs_manual,
        "local_candidate_rerun_queue": candidate_rerun_count,
        "role": "local_audit_candidates_only_not_asr_and_not_an_approval",
        "asr_corpus_sha256": review_binding,
        "current_asr_corpus_sha256": expected_asr_corpus_sha256,
        "binding_matches": review_binding == expected_asr_corpus_sha256,
    }
    if review_binding != expected_asr_corpus_sha256:
        blockers.append({
            "code": "blank_asr_review_stale",
            "message": "空段本地审计未绑定当前 MiMo ASR 全量内容，必须重跑审计",
        })

    # Local SenseVoice/Whisper candidates do not alter ASR and do not approve a
    # network re-run.  Only the independent adjudication can do that.
    if blank_adjudication is None:
        adjudication_complete = False
        approved_queue_count = None
    else:
        approved_ids = blank_adjudication.get("approved_review_ids")
        patches = blank_adjudication.get("patches")
        errors = blank_adjudication.get("errors")
        if not isinstance(approved_ids, list) or not all(isinstance(item, str) for item in approved_ids):
            raise EvidenceError("MiMo 空段裁决 approved_review_ids 必须是字符串数组")
        if not isinstance(patches, list) or not isinstance(errors, list):
            raise EvidenceError("MiMo 空段裁决 patches/errors 必须是数组")
        approved_queue_count = len(approved_ids)
        pending_approved_ids = sorted(set(approved_ids) - validated_patch_ids)
        adjudication_binding = blank_adjudication.get("asr_corpus_sha256")
        adjudication_complete = bool(
            blank_adjudication.get("complete") is True
            and not errors
            and blank_adjudication.get("original_asr_files_modified") is False
            and blank_adjudication.get("endpoint") == MIMO_ENDPOINT
            and adjudication_binding == expected_asr_corpus_sha256
            and not pending_approved_ids
        )
    details["blank_asr_adjudication"] = {
        "complete": adjudication_complete,
        "approved_mimo_rerun_count": approved_queue_count,
        "approved_mimo_rerun_pending": (
            len(pending_approved_ids) if blank_adjudication is not None else None
        ),
        "completed_patch_count": len(validated_patch_ids),
        "asr_corpus_sha256": (
            blank_adjudication.get("asr_corpus_sha256") if blank_adjudication else None
        ),
        "current_asr_corpus_sha256": expected_asr_corpus_sha256,
    }
    if not adjudication_complete:
        blockers.append({"code": "blank_asr_adjudication_incomplete",
                         "message": "MiMo 空段裁决未完成、含错误或改写了原 ASR"})
    if blank_adjudication is not None and pending_approved_ids:
        blockers.append({"code": "blank_asr_approved_mimo_rerun_pending",
                         "message": f"经独立裁决批准的 MiMo 重切分重跑尚有 {len(pending_approved_ids)} 条"})
    return blockers, details


def load_asr_documents(
    bindings: dict[str, dict[str, Any]], work_root: Path,
) -> dict[str, dict[str, Any]]:
    asr_by_sha: dict[str, dict[str, Any]] = {}
    for source_sha in sorted(bindings):
        folder = work_root / source_sha[:16]
        asr = read_json(folder / "asr.json")
        assert_no_generated_fields(asr, f"asr[{source_sha}]")
        if asr.get("source_sha256") != source_sha:
            raise EvidenceError(f"MiMo ASR 源 SHA 绑定失败：{folder}")
        asr_by_sha[source_sha] = asr
    return asr_by_sha


def load_ocr_documents(
    bindings: dict[str, dict[str, Any]], work_root: Path,
) -> dict[str, dict[str, Any]]:
    ocr_by_sha: dict[str, dict[str, Any]] = {}
    for source_sha in sorted(bindings):
        folder = work_root / source_sha[:16]
        ocr = read_json(folder / "ocr.json")
        assert_no_generated_fields(ocr, f"whole_second_ocr[{source_sha}]")
        fingerprint = ocr.get("source_fingerprint")
        if not isinstance(fingerprint, dict) or fingerprint.get("sha256") != source_sha:
            raise EvidenceError(f"整秒 OCR 源 SHA 绑定失败：{folder}")
        ocr_by_sha[source_sha] = ocr
    return ocr_by_sha


def mimo_result(
    document: dict[str, Any],
    source_sha: str,
    patches_by_index: dict[int, dict[str, Any]] | None = None,
    *,
    patch_document_sha256: str | None = None,
) -> dict[str, Any]:
    if document.get("model") != MIMO_MODEL or document.get("source_sha256") != source_sha:
        raise EvidenceError(f"MiMo ASR 模型或源 SHA 不符：{source_sha}")
    segments = document.get("segments")
    if not isinstance(segments, list) or document.get("coverage", {}).get("complete") is not True:
        raise EvidenceError(f"MiMo ASR 覆盖不完整：{source_sha}")
    previous_end = -1
    for order, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise EvidenceError(f"MiMo segment 不是对象：{source_sha}/{order}")
        start, end = segment.get("sample_start"), segment.get("sample_end")
        if start is not None or end is not None:
            if not isinstance(start, int) or not isinstance(end, int) or start < previous_end or end <= start:
                raise EvidenceError(f"MiMo segment 原序/样本区间非法：{source_sha}/{order}")
            previous_end = end
    no_audio = document.get("has_audio") is False and document.get("skipped_reason") == "no_audio_stream"
    if no_audio and segments:
        raise EvidenceError(f"无音轨 MiMo 结果不得含分段：{source_sha}")
    if not no_audio and document.get("configuration", {}).get("endpoint") != MIMO_ENDPOINT:
        raise EvidenceError(f"有音轨 MiMo ASR 不是 token-plan-cn 官方 endpoint：{source_sha}")
    patches_by_index = patches_by_index or {}
    if no_audio and patches_by_index:
        raise EvidenceError(f"无音轨源不得应用 MiMo 空段补丁：{source_sha}")

    effective_document = document
    traceability = None
    if patches_by_index:
        effective_segments: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        consumed: set[int] = set()
        for segment in segments:
            original_index = segment.get("index")
            patch = patches_by_index.get(original_index)
            if patch is None:
                effective_segments.append({
                    **segment,
                    "index": len(effective_segments),
                    "original_segment_index": original_index,
                    "evidence_origin": "original_mimo_asr",
                })
                continue
            consumed.add(original_index)
            for child in patch["children"]:
                effective_segments.append({
                    "index": len(effective_segments),
                    "original_segment_index": original_index,
                    "patch_child_index": child["index"],
                    "sample_start": child["sample_start"],
                    "sample_end": child["sample_end"],
                    "sample_count": child["sample_count"],
                    "start_seconds": child["start_seconds"],
                    "end_seconds": child["end_seconds"],
                    "duration_seconds": child["duration_seconds"],
                    "text": child["text"],
                    "model": child["model"],
                    "finish_reason": child["finish_reason"],
                    "request_count": child["request_count"],
                    "request_id_sha256": child["request_id_sha256"],
                    "request_id_digest": child["request_id_digest"],
                    "content_filter_split": child["content_filter_split"],
                    "evidence_origin": "approved_mimo_blank_segment_patch",
                    "patch_review_id": patch["review_id"],
                })
            applied.append(patch)
        if consumed != set(patches_by_index):
            raise EvidenceError(f"MiMo patch 存在未应用的原 segment：{source_sha}")
        effective_document = {
            **document,
            "segments": effective_segments,
            "segment_stream": "effective_mimo_segments_with_validated_blank_patches",
            "original_segment_count": len(segments),
            "effective_segment_count": len(effective_segments),
            "applied_patch_count": len(applied),
        }
        traceability = {
            "original_asr_sha256": canonical_sha256(document),
            "patch_document_sha256": patch_document_sha256,
            "original_segments": segments,
            "applied_mimo_patches": applied,
            "local_asr_text_included": False,
        }
    # Source JSON remains immutable.  When patches exist, ``result.segments`` is
    # the validated effective MiMo stream and ``traceability.original_segments``
    # retains the untouched source stream.
    result = {
        "status": "not_applicable" if no_audio else "complete",
        "provider": "Xiaomi MiMo",
        "model": MIMO_MODEL,
        "reason": "source_has_no_audio_track" if no_audio else None,
        "result": effective_document,
    }
    if traceability is not None:
        result["traceability"] = traceability
    return result


def cache_frame_sha(work_root: Path, source_sha: str, signature: str, index: int) -> str:
    path = work_root / source_sha[:16] / "ocr_cache" / signature / f"frame_{index:06d}.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            prefix = handle.read(2048)
    except OSError as exc:
        raise EvidenceError(f"整秒 OCR 帧缓存不存在：{path}") from exc
    signature_match = re.search(r'"signature"\s*:\s*"([0-9a-f]{64})"', prefix)
    sha_match = re.search(r'"image_sha256"\s*:\s*"([0-9a-f]{64})"', prefix)
    if not signature_match or signature_match.group(1) != signature or not sha_match:
        raise EvidenceError(f"整秒 OCR 帧缓存签名/SHA 无效：{path}")
    return sha_match.group(1)


def validate_lines(lines: Any, location: str) -> list[dict[str, Any]]:
    if not isinstance(lines, list):
        raise EvidenceError(f"OCR lines 必须是数组：{location}")
    for index, line in enumerate(lines):
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            raise EvidenceError(f"OCR 行结构无效：{location}/{index}")
        if not isinstance(line.get("box"), list):
            raise EvidenceError(f"OCR 行缺少原始框：{location}/{index}")
    return lines


def scene_records(scene: dict[str, Any] | None, known_shas: set[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if scene is None:
        return output
    sources = scene.get("sources")
    if not isinstance(sources, list):
        return output
    for index, record in enumerate(sources):
        if not isinstance(record, dict):
            raise EvidenceError(f"scene sources[{index}] 必须是对象")
        source_sha = valid_sha(record.get("source_sha256"), f"scene sources[{index}].source_sha256")
        if source_sha not in known_shas or source_sha in output:
            raise EvidenceError(f"场景 OCR 源 SHA 未绑定或重复：{source_sha}")
        frames = record.get("frames")
        if not isinstance(frames, list):
            raise EvidenceError(f"场景 OCR frames 缺失：{source_sha}")
        output[source_sha] = record
    return output


def _frame_time(frame: dict[str, Any]) -> Fraction:
    if frame.get("frame_source") == "scene_change":
        time_base = frame.get("time_base")
        if isinstance(frame.get("pts"), int) and isinstance(time_base, dict):
            return Fraction(frame["pts"] * int(time_base["numerator"]), int(time_base["denominator"]))
    value = frame.get("actual_timestamp_seconds", frame.get("timestamp_seconds"))
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EvidenceError("视频 OCR 帧缺少有限时间戳")
    return Fraction(str(value))


def video_ocr_result(
    document: dict[str, Any], source_sha: str, work_root: Path,
    scene_record: dict[str, Any] | None,
) -> dict[str, Any]:
    frames = document.get("frames")
    signature = document.get("cache_signature")
    fingerprint = document.get("source_fingerprint")
    if (not isinstance(frames, list) or not isinstance(signature, str)
            or not isinstance(fingerprint, dict) or fingerprint.get("sha256") != source_sha
            or document.get("coverage_verified") is not True
            or document.get("expected_frame_count") != len(frames)):
        raise EvidenceError(f"整秒 OCR 不完整：{source_sha}")
    merged: list[dict[str, Any]] = []
    base_hashes: set[str] = set()
    previous_index = -1
    for order, frame in enumerate(frames):
        if not isinstance(frame, dict) or not isinstance(frame.get("index"), int):
            raise EvidenceError(f"整秒 OCR 帧结构无效：{source_sha}/{order}")
        if frame["index"] <= previous_index:
            raise EvidenceError(f"整秒 OCR 帧未按原序排列：{source_sha}/{order}")
        previous_index = frame["index"]
        validate_lines(frame.get("lines"), f"whole-second/{source_sha}/{order}")
        image_sha = cache_frame_sha(work_root, source_sha, signature, frame["index"])
        base_hashes.add(image_sha)
        merged.append({**frame, "frame_source": "whole_second",
                       "frame_image_sha256": image_sha, "pts": None, "time_base": None})

    skipped_scene_duplicates = 0
    seen_scene_hashes: set[str] = set()
    if scene_record is not None:
        for order, frame in enumerate(scene_record["frames"]):
            if not isinstance(frame, dict):
                raise EvidenceError(f"场景 OCR 帧结构无效：{source_sha}/{order}")
            if frame.get("source_sha256") != source_sha:
                raise EvidenceError(f"场景 OCR 帧源 SHA 不符：{source_sha}/{order}")
            validate_lines(frame.get("lines"), f"scene/{source_sha}/{order}")
            image_sha = valid_sha(frame.get("frame_image_sha256"),
                                  f"scene/{source_sha}/{order}.frame_image_sha256")
            # Do not deduplicate repeated whole-second frames.  Only prevent a
            # supplemental physical image from being carried a second time.
            if image_sha in base_hashes or image_sha in seen_scene_hashes:
                skipped_scene_duplicates += 1
                continue
            seen_scene_hashes.add(image_sha)
            merged.append({**frame, "frame_source": "scene_change"})

    merged.sort(key=lambda frame: (
        _frame_time(frame), 0 if frame["frame_source"] == "whole_second" else 1,
        -1 if frame.get("pts") is None else frame["pts"],
    ))
    for index, frame in enumerate(merged):
        frame["merged_order"] = index
    whole_metadata = {key: value for key, value in document.items() if key != "frames"}
    scene_metadata = None
    if scene_record is not None:
        scene_metadata = {key: value for key, value in scene_record.items() if key != "frames"}
    return {
        "status": "complete" if scene_record is not None else "incomplete",
        "source_type": "video",
        "source_sha256": source_sha,
        "whole_second_metadata": whole_metadata,
        "supplemental_scene_metadata": scene_metadata,
        "whole_second_frame_count": len(frames),
        "supplemental_scene_frame_count": len(scene_record["frames"]) if scene_record else 0,
        "supplemental_physical_duplicates_merged": skipped_scene_duplicates,
        "merged_frame_count": len(merged),
        "frames": merged,
        "text_policy": {
            "global_text_deduplication": False,
            "text_correction": False,
            "keyword_filtering": False,
            "line_order": "original RapidOCR box order within each frame",
            "frame_order": "PTS/time order",
        },
    }


def courseware_records(
    document: dict[str, Any] | None,
    known_courseware_shas: set[str],
    catalog_sha: str,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if document is None:
        return output
    files = document.get("files")
    if not isinstance(files, list):
        raise EvidenceError("courseware OCR.files 必须是数组")
    allowed = known_courseware_shas | {catalog_sha}
    for file_order, record in enumerate(files):
        if not isinstance(record, dict):
            raise EvidenceError(f"courseware files[{file_order}] 必须是对象")
        source_sha = valid_sha(record.get("source_sha256"),
                               f"courseware files[{file_order}].source_sha256")
        if source_sha not in allowed or source_sha in output:
            raise EvidenceError(f"课件 OCR 源 SHA 未绑定或重复：{source_sha}")
        pages = record.get("pages")
        if not isinstance(pages, list):
            raise EvidenceError(f"课件 OCR pages 缺失：{source_sha}")
        previous = (-1, -1)
        for page_order, page in enumerate(pages):
            if not isinstance(page, dict):
                raise EvidenceError(f"课件页结构无效：{source_sha}/{page_order}")
            key = (int(page.get("page", 0)), int(page.get("part", 0)))
            if key < previous:
                raise EvidenceError(f"课件页未保持来源顺序：{source_sha}/{page_order}")
            previous = key
            validate_lines(page.get("raw_ocr_lines"), f"courseware/{source_sha}/{page_order}/raw")
            native = page.get("native_lines")
            if not isinstance(native, list):
                raise EvidenceError(f"课件页 native_lines 未独立保留：{source_sha}/{page_order}")
        if not isinstance(record.get("embedded_objects"), list):
            raise EvidenceError(f"课件附件/动图/工作簿清单缺失：{source_sha}")
        output[source_sha] = record
    return output


def _mimo_texts(result: dict[str, Any]) -> list[str]:
    document = result.get("result")
    if not isinstance(document, dict):
        return []
    # Empty MiMo segments intentionally contribute no invented replacement.
    return [segment["text"] for segment in document.get("segments", [])
            if isinstance(segment, dict) and isinstance(segment.get("text"), str)
            and segment["text"].strip()]


def _ocr_frame_texts(frames: Iterable[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    for frame in frames:
        for line in frame.get("lines", []):
            if isinstance(line, dict) and isinstance(line.get("text"), str):
                output.append(line["text"])
    return output


def _native_line_texts(lines: Any) -> list[str]:
    if not isinstance(lines, list):
        return []
    return [item["text"] for item in lines
            if isinstance(item, dict) and isinstance(item.get("text"), str)]


def _workbook_native_texts(native_content: Any) -> list[str]:
    """Mechanically walk sheet names and cell values in their stored order."""
    output: list[str] = []
    if not isinstance(native_content, list):
        return output
    for sheet in native_content:
        if not isinstance(sheet, dict):
            continue
        if isinstance(sheet.get("name"), str):
            output.append(sheet["name"])
        cells = sheet.get("cells")
        if not isinstance(cells, list):
            continue
        for cell in cells:
            if isinstance(cell, dict) and isinstance(cell.get("value"), str):
                output.append(cell["value"])
    return output


def mechanical_sections(raw_evidence: dict[str, Any]) -> dict[str, list[str]]:
    """Flatten raw evidence to exporter strings without semantic processing."""
    asr_result = raw_evidence["asr_result"]
    if raw_evidence["kind"] == "video":
        asr_texts = _mimo_texts(asr_result)
        if not asr_texts:
            asr_texts = (["不适用：源文件不含音轨。"]
                         if asr_result.get("status") == "not_applicable"
                         else ["MiMo ASR各分段均为空，未提供可搬运文本。"])
    else:
        embedded = asr_result.get("embedded_media", [])
        asr_texts = []
        for media in embedded:
            asr_texts.extend(_mimo_texts(media.get("asr_result", {})))
        if not asr_texts:
            asr_texts = (["MiMo ASR内嵌媒体各分段均为空，未提供可搬运文本。"]
                         if embedded else ["不适用：课件文件无独立音轨。"])

    ocr_result = raw_evidence["ocr_result"]
    if raw_evidence["kind"] == "video":
        ocr_texts = _ocr_frame_texts(ocr_result.get("frames", []))
    else:
        ocr_texts: list[str] = []
        courseware = ocr_result.get("courseware_record")
        if isinstance(courseware, dict):
            for page in courseware.get("pages", []):
                if not isinstance(page, dict):
                    continue
                # Raw visual OCR and native text remain separate in raw_evidence;
                # the Word stream carries both, in this fixed per-page order.
                for line in page.get("raw_ocr_lines", []):
                    if isinstance(line, dict) and isinstance(line.get("text"), str):
                        ocr_texts.append(line["text"])
                ocr_texts.extend(_native_line_texts(page.get("native_lines")))
            ocr_texts.extend(_native_line_texts(courseware.get("native_document_text")))
            for embedded_object in courseware.get("embedded_objects", []):
                if not isinstance(embedded_object, dict):
                    continue
                for frame in embedded_object.get("raw_ocr_frames", []):
                    if not isinstance(frame, dict):
                        continue
                    for line in frame.get("raw_ocr_lines", []):
                        if isinstance(line, dict) and isinstance(line.get("text"), str):
                            ocr_texts.append(line["text"])
                ocr_texts.extend(_workbook_native_texts(embedded_object.get("native_content")))
        for media in ocr_result.get("embedded_media", []):
            nested = media.get("ocr_result", {}) if isinstance(media, dict) else {}
            ocr_texts.extend(_ocr_frame_texts(nested.get("frames", [])))
    if not any(value.strip() for value in ocr_texts):
        ocr_texts = ["OCR未识别到可搬运文字。"]
    return {"ASR 结果": asr_texts, "OCR 结果": ocr_texts}


def build_corpus(
    *,
    root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
    asr_ocr_root: Path = DEFAULT_ASR_OCR_ROOT,
    embedded_manifest_path: Path = DEFAULT_EMBEDDED_MANIFEST,
    scene_path: Path = DEFAULT_SCENE_OCR,
    courseware_path: Path = DEFAULT_COURSEWARE_OCR,
    blank_review_path: Path = DEFAULT_BLANK_REVIEW,
    blank_adjudication_path: Path = DEFAULT_BLANK_ADJUDICATION,
    output_path: Path | None = DEFAULT_OUTPUT,
    allow_incomplete: bool = False,
    expected_assets: int = 91,
    expected_courseware_inputs: int = 29,
    expected_video_sources: int = 67,
    expected_no_audio: int = 2,
    show_progress: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    manifest, assets = load_assets(manifest_path, expected_assets=expected_assets)
    embedded_manifest = read_json(embedded_manifest_path)
    assert_no_generated_fields(manifest, "asset_manifest")
    assert_no_generated_fields(embedded_manifest, "embedded_manifest")
    bindings, embedded_by_parent = source_bindings(assets, embedded_manifest, root)
    if len(bindings) != expected_video_sources:
        raise EvidenceError(f"视频及内嵌媒体源应为 {expected_video_sources}，实际 {len(bindings)}")

    asr_by_sha = load_asr_documents(bindings, asr_ocr_root)
    prepared_asr = {sha: mimo_result(document, sha) for sha, document in asr_by_sha.items()}
    current_asr_corpus_sha256 = asr_corpus_sha256(asr_by_sha)
    no_audio_count = sum(item["status"] == "not_applicable" for item in prepared_asr.values())
    if no_audio_count != expected_no_audio:
        raise EvidenceError(f"明确无音轨 ASR 不适用项应为 {expected_no_audio}，实际 {no_audio_count}")

    courseware_doc = optional_json(courseware_path)
    scene_doc = optional_json(scene_path)
    blank_doc = optional_json(blank_review_path)
    adjudication_doc = optional_json(blank_adjudication_path)
    for label, document in (
        ("courseware_ocr", courseware_doc),
        ("video_scene_ocr", scene_doc),
        ("blank_asr_review", blank_doc),
        ("blank_asr_adjudication", adjudication_doc),
    ):
        if document is not None:
            assert_no_generated_fields(document, label)
    patches_by_source, validated_patch_ids = validate_completed_mimo_patches(
        adjudication_doc, asr_by_sha, current_asr_corpus_sha256,
    )
    blockers, readiness = assess_readiness(
        courseware_doc, scene_doc, blank_doc, adjudication_doc,
        expected_courseware_inputs=expected_courseware_inputs,
        expected_video_sources=expected_video_sources,
        expected_asr_corpus_sha256=current_asr_corpus_sha256,
        validated_patch_ids=validated_patch_ids,
    )
    if blockers and not allow_incomplete:
        raise IncompleteEvidenceError(blockers)

    patch_document_sha256 = canonical_sha256(adjudication_doc) if validated_patch_ids else None
    prepared_asr = {
        sha: mimo_result(
            document, sha, patches_by_source.get(sha),
            patch_document_sha256=patch_document_sha256,
        )
        for sha, document in asr_by_sha.items()
    }

    ocr_by_sha = load_ocr_documents(bindings, asr_ocr_root)
    scene_by_sha = scene_records(scene_doc, set(bindings))

    prepared_ocr: dict[str, dict[str, Any]] = {}
    for index, source_sha in enumerate(sorted(bindings), 1):
        prepared_ocr[source_sha] = video_ocr_result(
            ocr_by_sha[source_sha], source_sha, asr_ocr_root, scene_by_sha.get(source_sha),
        )
        if show_progress:
            print(f"视频证据 [{index:02d}/{len(bindings):02d}] {bindings[source_sha]['source_path'].name}",
                  flush=True)

    courseware_assets = [asset for asset in assets if asset.get("kind") == "courseware"]
    course_by_sha = courseware_records(
        courseware_doc,
        {asset["source_sha256"] for asset in courseware_assets},
        valid_sha(manifest.get("catalog", {}).get("source_sha256"), "catalog.source_sha256"),
    )

    files: list[dict[str, Any]] = []
    for index, asset in enumerate(assets, 1):
        source_sha = asset["source_sha256"]
        if asset.get("kind") == "video":
            asr_result = prepared_asr[source_sha]
            ocr_result = prepared_ocr[source_sha]
        else:
            embedded_results_asr = []
            embedded_results_ocr = []
            for binding in embedded_by_parent.get(asset["asset_id"], []):
                media_sha = binding["source_sha256"]
                media_identity = {
                    "source_sha256": media_sha,
                    "source_path": relative_path(binding["source_path"], root),
                    "slide": binding.get("slide"),
                    "member": binding.get("member"),
                }
                embedded_results_asr.append({**media_identity, "asr_result": prepared_asr[media_sha]})
                embedded_results_ocr.append({**media_identity, "ocr_result": prepared_ocr[media_sha]})
            asr_result = {
                "status": "embedded_media_only" if embedded_results_asr else "not_applicable",
                "reason": ("courseware_asr_is_carried_by_embedded_media"
                           if embedded_results_asr else "courseware_has_no_audio_evidence"),
                "embedded_media": embedded_results_asr,
            }
            course_record = course_by_sha.get(source_sha)
            ocr_result = {
                "status": "complete" if course_record and course_record.get("processing_complete") else "incomplete",
                "source_type": "courseware",
                "source_sha256": source_sha,
                "engine": courseware_doc.get("engine") if courseware_doc else None,
                # Copy the file record whole: pages keep raw_ocr_lines and
                # native_lines separate; embedded_objects keep raster/animated
                # frames and workbook native_content in generator order.
                "courseware_record": course_record,
                "embedded_media": embedded_results_ocr,
            }
        raw_evidence = {
            "asset_id": asset["asset_id"],
            "source_sha256": source_sha,
            "source_path": asset["source_path"],
            "source_name": asset["source_name"],
            "year": asset["year"],
            "kind": asset["kind"],
            "catalog_position": asset.get("catalog_position"),
            "catalog_suborder": asset.get("catalog_suborder"),
            "asset_manifest_record": asset,
            "asr_result": asr_result,
            "ocr_result": ocr_result,
        }
        evidence_sha = canonical_sha256(raw_evidence)
        files.append({
            "asset_id": asset["asset_id"],
            "source_sha256": source_sha,
            "evidence_sha256": evidence_sha,
            "sections": mechanical_sections(raw_evidence),
            "raw_evidence": raw_evidence,
        })
        if show_progress and asset.get("kind") == "courseware":
            print(f"课件证据 [{index:02d}/{len(assets):02d}] {asset['source_name']}", flush=True)

    if len(files) != expected_assets or len({item["asset_id"] for item in files}) != expected_assets:
        raise AssertionError("Every authoritative asset must produce exactly one evidence record")
    assert_no_generated_fields(files)
    status = "incomplete" if blockers else "complete"
    corpus = {
        "schema_version": "first-principles-evidence/v1",
        "status": status,
        "artifact_role": "debug_only_not_for_final_fusion" if blockers else "authoritative_evidence",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asset_count": len(files),
        "file_count": len(files),
        "source_counts": {
            "course_videos": sum(asset.get("kind") == "video" for asset in assets),
            "embedded_media": sum(binding["source_role"] == "embedded_media" for binding in bindings.values()),
            "courseware_assets": len(courseware_assets),
            "courseware_ocr_control_inclusive_required": expected_courseware_inputs,
        },
        "asr_corpus_sha256": current_asr_corpus_sha256,
        "readiness": readiness,
        "blockers": blockers,
        "content_policy": {
            "generated_prose_fields": False,
            "asr_source": "verified Xiaomi MiMo only",
            "local_asr_substitution": False,
            "ocr_global_text_deduplication": False,
            "ocr_text_correction": False,
            "ocr_keyword_filtering": False,
        },
        "files": files,
    }
    assert_no_generated_fields(corpus)
    if output_path is not None:
        atomic_json(output_path, corpus)
    return corpus


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--asr-ocr-root", type=Path, default=DEFAULT_ASR_OCR_ROOT)
    parser.add_argument("--embedded-manifest", type=Path, default=DEFAULT_EMBEDDED_MANIFEST)
    parser.add_argument("--video-scene-ocr", type=Path, default=DEFAULT_SCENE_OCR)
    parser.add_argument("--courseware-ocr", type=Path, default=DEFAULT_COURSEWARE_OCR)
    parser.add_argument("--blank-review", type=Path, default=DEFAULT_BLANK_REVIEW)
    parser.add_argument("--blank-adjudication", type=Path, default=DEFAULT_BLANK_ADJUDICATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="严格完成态输出；不完整时绝不写此文件")
    parser.add_argument("--debug-output", type=Path, default=DEFAULT_DEBUG_OUTPUT,
                        help="--allow-incomplete 的调试输出，不能用于最终融合")
    parser.add_argument("--allow-incomplete", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination = args.debug_output if args.allow_incomplete else args.output
    try:
        corpus = build_corpus(
            manifest_path=args.manifest,
            asr_ocr_root=args.asr_ocr_root,
            embedded_manifest_path=args.embedded_manifest,
            scene_path=args.video_scene_ocr,
            courseware_path=args.courseware_ocr,
            blank_review_path=args.blank_review,
            blank_adjudication_path=args.blank_adjudication,
            output_path=destination,
            allow_incomplete=args.allow_incomplete,
            expected_assets=91,
            expected_courseware_inputs=29,
            expected_video_sources=67,
            expected_no_audio=2,
            show_progress=True,
        )
    except IncompleteEvidenceError as exc:
        print(json.dumps({"status": "refused", "output_written": False,
                          "blockers": exc.blockers}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "status": corpus["status"],
        "artifact_role": corpus["artifact_role"],
        "output": str(destination.resolve()),
        "asset_count": corpus["asset_count"],
        "blockers": corpus["blockers"],
    }, ensure_ascii=False, indent=2))
    return 0 if corpus["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
