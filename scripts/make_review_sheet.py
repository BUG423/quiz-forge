"""Make local evidence contact sheets from already decoded video frames."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ocr", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--indices", required=True, help="Comma separated frame indices")
    parser.add_argument("--columns", type=int, default=2)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--width", type=int, default=856)
    args = parser.parse_args()
    records = {f["index"]: f for f in json.loads(args.ocr.read_text(encoding="utf-8"))["frames"]}
    indices = [int(x) for x in args.indices.split(",")]
    args.output.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    per_sheet = args.columns * args.rows
    for sheet_no, offset in enumerate(range(0, len(indices), per_sheet), 1):
        chunk = indices[offset:offset + per_sheet]
        height = round(args.width * 480 / 856)
        cell_h = height + 36
        canvas = Image.new("RGB", (args.columns * args.width, math.ceil(len(chunk) / args.columns) * cell_h), "white")
        draw = ImageDraw.Draw(canvas)
        for position, index in enumerate(chunk):
            frame = records[index]
            x, y = position % args.columns * args.width, position // args.columns * cell_h
            with Image.open(frame["image_path"]) as original:
                original.thumbnail((args.width, height))
                canvas.paste(original, (x, y + 36))
            draw.text((x + 8, y + 5), f"Frame {index} / {frame['timestamp_seconds']:.0f}s", font=font, fill="black")
        path = args.output / f"sheet_{sheet_no:02d}.jpg"
        canvas.save(path, quality=96)
        print(path)


if __name__ == "__main__":
    main()
