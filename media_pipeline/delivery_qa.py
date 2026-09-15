"""Cross-check source coverage and the final 2025 delivery artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .batch_2025 import _valid_asr, _valid_ocr
from .bulk_fusion import validate_corpus
from .combined_word import qa_word
from .full_fusion_qa import build_full_fusion_qa
from .runner import atomic_json, read_json


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_qa(inventory: dict, courseware: dict, corpus: dict, word: Path,
             work_root: Path, *, rehash: bool = True) -> dict:
    validate_corpus(corpus, inventory)
    by_name = {item["source_name"]: item for item in corpus["files"]}
    current_hashes = {}
    if rehash:
        for item in inventory["videos"] + inventory["courseware"]:
            source = Path(item["source_path"])
            current_hashes[item["source_name"]] = source.is_file() and _sha256(source) == item["sha256"]
        if not all(current_hashes.values()):
            raise ValueError("一个或多个源文件在处理期间发生变化")

    mimo_asr, silent, fallback, ocr_complete, frame_count, vision = 0, 0, 0, 0, 0, 0
    for manifest in inventory["videos"]:
        work = work_root / manifest["sha256"][:16]
        if _valid_asr(work / "asr.json", manifest):
            if manifest["has_audio"]:
                mimo_asr += 1
            else:
                silent += 1
        elif by_name[manifest["source_name"]].get("evidence_status", {}).get("asr_source") == "prior-reviewed-asr-fallback":
            fallback += 1
        if _valid_ocr(work / "ocr.json", manifest):
            value = read_json(work / "ocr.json")
            ocr_complete += 1
            frame_count += len(value["frames"])
        vision_path = work / "vision.json"
        if vision_path.is_file():
            value = read_json(vision_path)
            if value.get("model") == "mimo-v2.5" and value.get("source_sha256") == manifest["sha256"]:
                vision += 1

    courseware_by_name = {item["source"]: item for item in courseware.get("files", [])}
    bound_courseware = sum(
        name in courseware_by_name and courseware_by_name[name].get("source_sha256") == item["sha256"]
        for name, item in ((value["source_name"], value) for value in inventory["courseware"])
    )
    word_result = qa_word(word, corpus)
    fusion_result = build_full_fusion_qa(corpus)
    required_audio = inventory["audio_video_count"]
    result = {
        "source_file_count": len(inventory["videos"]) + len(inventory["courseware"]),
        "source_hashes_rechecked": rehash,
        "source_hash_matches": sum(current_hashes.values()) if rehash else None,
        "video_count": len(inventory["videos"]),
        "audio_video_count": required_audio,
        "silent_video_count": inventory["silent_video_count"],
        "mimo_asr_complete": mimo_asr,
        "silent_asr_skips_complete": silent,
        "prior_reviewed_asr_fallback": fallback,
        "video_rapidocr_complete": ocr_complete,
        "video_ocr_frame_count": frame_count,
        "expected_video_ocr_frame_count": sum(item["expected_frame_count"] for item in inventory["videos"]),
        "mimo_visual_complete": vision,
        "courseware_count": len(courseware.get("files", [])),
        "courseware_source_hash_matches": bound_courseware,
        "courseware_page_or_slice_count": courseware.get("page_count"),
        "word": word_result,
        "full_fusion": fusion_result,
    }
    result["structural_delivery_complete"] = (
        ocr_complete == len(inventory["videos"])
        and frame_count == result["expected_video_ocr_frame_count"]
        and bound_courseware == len(inventory["courseware"])
        and word_result["file_heading_count"] == result["source_file_count"]
    )
    result["specified_mimo_asr_complete"] = (
        mimo_asr == required_audio and silent == inventory["silent_video_count"]
        and fallback == 0
    )
    result["specified_rapidocr_complete"] = (
        ocr_complete == len(inventory["videos"])
        and frame_count == result["expected_video_ocr_frame_count"]
        and bound_courseware == len(inventory["courseware"])
    )
    result["mimo_visual_supplement_complete"] = vision == len(inventory["videos"])
    result["specified_processing_complete"] = (
        result["structural_delivery_complete"]
        and result["specified_mimo_asr_complete"]
        and result["specified_rapidocr_complete"]
        and fusion_result["passed"]
        and fusion_result["file_count"] == result["source_file_count"]
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="核验2025总Word及其源证据覆盖")
    parser.add_argument("--inventory", type=Path, default=Path(".work/batch-2025/inventory.json"))
    parser.add_argument("--courseware", type=Path, default=Path(".work/batch-2025/courseware_ocr.json"))
    parser.add_argument("--content", type=Path, default=Path(".work/batch-2025/fused_content.json"))
    parser.add_argument("--work-root", type=Path, default=Path(".work/single-video"))
    parser.add_argument("--word", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".work/batch-2025/final_qa.json"))
    parser.add_argument("--no-rehash", action="store_true")
    args = parser.parse_args()
    try:
        result = build_qa(
            read_json(args.inventory.resolve()), read_json(args.courseware.resolve()),
            read_json(args.content.resolve()), args.word.resolve(strict=True),
            args.work_root.resolve(), rehash=not args.no_rehash)
        atomic_json(args.output.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
