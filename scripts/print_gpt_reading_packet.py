#!/usr/bin/env python3
"""Print a loss-aware reading view of one GPT fusion asset.

The source of truth remains gpt-reading-chunks.  This helper only removes exact
repetitions of the same text string (normally persistent video overlays) and
prints every first occurrence in source order; it never drafts fusion prose.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".work" / "first-principles-2026"


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("order", type=int)
    args = parser.parse_args()

    index = json.loads((WORK / "gpt-tasks" / "index.json").read_text(encoding="utf-8"))
    item = next(entry for entry in index["tasks"] if entry["order"] == args.order)
    task = json.loads((WORK / "gpt-tasks" / item["task_file"]).read_text(encoding="utf-8"))
    paths = sorted(glob.glob(str(WORK / "gpt-reading-chunks" / f"{args.order:04d}-*-chunk-*.json")))

    print(f"ORDER={args.order} SOURCE={task['source_name']}")
    print(f"ASSET={task['asset_id']}")
    print(f"SOURCE_SHA={task['source_sha256']}")
    print(f"EVIDENCE_SHA={task['evidence_sha256']}")
    print(f"TASK_SHA={item['task_sha256']}")
    print(f"CHUNKS={len(paths)}")

    events = []
    for path in paths:
        chunk = json.loads(Path(path).read_text(encoding="utf-8"))
        rng = chunk["event_index_range"]
        print(
            f"CHUNK {chunk['chunk_index']}/{chunk['chunk_count']} "
            f"EVENTS={rng['first']}..{rng['last']} CHARS={chunk['original_text_character_count']} "
            f"SHA={chunk['chunk_sha256']}"
        )
        events.extend(
            record["event"] for record in chunk["records"]
            if record.get("record_type") == "event"
        )

    print("\n[ASR：按事件顺序，空文本略去]")
    for event in events:
        if event.get("channel") != "ASR":
            continue
        values = [clean(part.get("text", "")) for part in event.get("texts", [])]
        values = [value for value in values if value]
        if values:
            print(f"E{event['event_index']}: {' '.join(values)}")

    print("\n[OCR：按首次出现顺序，仅去除完全相同的重复字符串]")
    seen: set[str] = set()
    for event in events:
        if event.get("channel") != "OCR":
            continue
        fresh = []
        for part in event.get("texts", []):
            value = clean(part.get("text", ""))
            if not value or value in seen:
                continue
            seen.add(value)
            fresh.append(value)
        if fresh:
            print(f"E{event['event_index']} {event.get('record_kind')}: {' | '.join(fresh)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
