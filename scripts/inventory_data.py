from __future__ import annotations

import json
import mimetypes
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
DATA_LIST = OUTPUT_DIR / "data文件清单.txt"
ARCHIVE_LIST = OUTPUT_DIR / "压缩包内容清单.txt"
COMBINED_MARKDOWN = OUTPUT_DIR / "文件清单.md"
PURE_NAME_LIST = OUTPUT_DIR / "纯文件名列表.txt"
TRANSCRIPTION_STATUS = OUTPUT_DIR / "transcription_status.json"
ARCHIVE_SUFFIXES = {".zip"}


def readable_type(path: Path) -> str:
    suffix = path.suffix.lower()
    common = {
        ".mp4": "MP4 视频",
        ".zip": "ZIP 压缩包",
        ".pdf": "PDF 文档",
        ".png": "PNG 图片",
        ".jpg": "JPEG 图片",
        ".jpeg": "JPEG 图片",
        ".pptx": "PowerPoint 文档",
        ".docx": "Word 文档",
    }
    return common.get(suffix, mimetypes.guess_type(path.name)[0] or suffix.lstrip(".").upper() or "未知")


def readable_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def readable_duration(seconds: float) -> str:
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} 小时 {minutes:02d} 分" if hours else f"{minutes}:{seconds:02d}"


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|")


def load_transcription_status() -> dict:
    if not TRANSCRIPTION_STATUS.is_file():
        return {}
    try:
        return json.loads(TRANSCRIPTION_STATUS.read_text(encoding="utf-8")).get("items", {})
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> None:
    if not DATA_DIR.is_dir():
        raise SystemExit(f"目录不存在：{DATA_DIR}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    files = sorted(
        (path for path in DATA_DIR.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(DATA_DIR).as_posix(),
    )

    data_lines = [
        "data 文件清单",
        f"文件总数：{len(files)}",
        f"总大小：{sum(path.stat().st_size for path in files)} bytes",
        "",
        "序号\t相对路径\t大小（bytes）\t文件类型",
    ]
    for index, path in enumerate(files, 1):
        relative = path.relative_to(DATA_DIR).as_posix()
        data_lines.append(f"{index}\t{relative}\t{path.stat().st_size}\t{readable_type(path)}")
    DATA_LIST.write_text("\n".join(data_lines) + "\n", encoding="utf-8")

    archives = [path for path in files if path.suffix.lower() in ARCHIVE_SUFFIXES]
    archive_lines = [
        "压缩包内容清单",
        f"压缩包总数：{len(archives)}",
        "说明：仅读取压缩包目录，没有解压文件。",
        "",
    ]

    entry_total = 0
    archive_entries: dict[Path, list] = {}
    for archive_index, archive in enumerate(archives, 1):
        relative = archive.relative_to(DATA_DIR).as_posix()
        archive_lines.extend([f"压缩包 {archive_index}：{relative}", "序号\t包内路径\t大小（bytes）\t文件类型"])
        try:
            with ZipFile(archive) as zip_file:
                entries = [item for item in zip_file.infolist() if not item.is_dir()]
                archive_entries[archive] = entries
                entry_total += len(entries)
                for index, item in enumerate(entries, 1):
                    archive_lines.append(
                        f"{index}\t{item.filename}\t{item.file_size}\t{readable_type(Path(item.filename))}"
                    )
                archive_lines.append(f"包内文件数：{len(entries)}")
        except BadZipFile as exc:
            archive_lines.append(f"读取失败：{exc}")
        archive_lines.append("")

    ARCHIVE_LIST.write_text("\n".join(archive_lines), encoding="utf-8")

    media_files = [path for path in files if path.suffix.lower() != ".zip"]
    transcription_items = load_transcription_status()
    total_duration = sum(item.get("duration_seconds", 0) for item in transcription_items.values())
    audio_duration = sum(
        item.get("duration_seconds", 0) for item in transcription_items.values() if item.get("has_audio")
    )
    status_labels = {
        "completed": "已完成",
        "processing": "**转写中**",
        "pending": "待处理",
        "no_audio": "无音轨",
        "error": "**失败**",
        "interrupted": "已中断",
    }
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    markdown_lines = [
        "# 文件资产清单",
        "",
        f"> 盘点目录：`data/`　·　生成时间：{generated_at}　·　压缩包仅查看目录，未解压",
        "",
        "## 资产概览",
        "",
        "| 资产类别 | 数量 | 容量 / 时长 | 备注 |",
        "| :--- | ---: | ---: | :--- |",
        f"| 音视频文件 | **{len(media_files)}** | {readable_duration(total_duration) if total_duration else '—'} | 其中有声视频 {sum(item.get('has_audio', False) for item in transcription_items.values())} 个 |",
        f"| 压缩包 | **{len(archives)}** | {readable_size(sum(path.stat().st_size for path in archives))} | 包内资料 {entry_total} 份 |",
        f"| **合计** | **{len(files)}** | **{readable_size(sum(path.stat().st_size for path in files))}** | 有声内容 {readable_duration(audio_duration) if audio_duration else '—'} |",
        "",
        "---",
        "",
        "## 一、音视频文件",
        "",
        "来源目录：`data/1.线上训练营视频/`",
        "",
        "转写状态：**转写中** / 已完成 / 待处理 / 无音轨 / **失败**",
        "",
        "| 序号 | 文件名称 | 时长 | 文件大小 | 转写状态 |",
        "| ---: | :--- | ---: | ---: | :---: |",
    ]
    for index, path in enumerate(media_files, 1):
        relative = path.relative_to(DATA_DIR).as_posix()
        item = transcription_items.get(relative, {})
        duration = readable_duration(item.get("duration_seconds", 0)) if item else "—"
        status = status_labels.get(item.get("status"), "未纳入转写")
        filename = markdown_escape(path.name)
        markdown_lines.append(
            f"| {index} | {filename} | {duration} | {readable_size(path.stat().st_size)} | {status} |"
        )

    markdown_lines.extend(["", "---", "", "## 二、压缩包及包内文件", ""])
    for archive_index, archive in enumerate(archives, 1):
        relative = archive.relative_to(DATA_DIR).as_posix()
        entries = archive_entries.get(archive, [])
        markdown_lines.extend(
            [
                f"### {archive_index}. {markdown_escape(archive.name)}",
                "",
                f"- 所在目录：`{markdown_escape(relative)}`",
                f"- 压缩包大小：**{readable_size(archive.stat().st_size)}**",
                f"- 包内文件：**{len(entries)}** 个",
                "",
                "| 序号 | 包内文件名称 | 格式 | 文件大小 |",
                "| ---: | :--- | :--- | ---: |",
            ]
        )
        for entry_index, item in enumerate(entries, 1):
            entry_name = markdown_escape(item.filename)
            markdown_lines.append(
                f"| {entry_index} | {entry_name} | {readable_type(Path(item.filename))} | {readable_size(item.file_size)} |"
            )
        markdown_lines.append("")

    COMBINED_MARKDOWN.write_text("\n".join(markdown_lines), encoding="utf-8")

    pure_names = [path.name for path in files]
    for archive in archives:
        pure_names.extend(Path(item.filename).name for item in archive_entries.get(archive, []))
    PURE_NAME_LIST.write_text("\n".join(pure_names) + "\n", encoding="utf-8")
    print(f"data_list={DATA_LIST}")
    print(f"archive_list={ARCHIVE_LIST}")
    print(f"combined_markdown={COMBINED_MARKDOWN}")
    print(f"pure_name_list={PURE_NAME_LIST}")
    print(f"files={len(files)} archives={len(archives)} archive_entries={entry_total}")


if __name__ == "__main__":
    main()
