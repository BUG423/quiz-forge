from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("multimodal_review", ROOT / "scripts" / "multimodal_review.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_clean_text() -> None:
    assert MODULE.clean_text("  中国 南方 电网 , 安全!  ", terminal=True) == "中国南方电网，安全！"
    assert MODULE.clean_text("<|zh|>供电可靠", terminal=True) == "供电可靠。"
    assert MODULE.clean_text("配电开关柜常见的有，", terminal=True) == "配电开关柜常见的有。"
    assert MODULE.clean_text("严明一令一动，。", terminal=True) == "严明一令一动。"


def test_similarity() -> None:
    assert MODULE.similarity("中国南方电网。", "中国南方电网") == 1.0
    assert MODULE.similarity("", "文本") is None


def test_sample_times() -> None:
    assert MODULE.sample_times(9, 30) == [1.0, 3.0, 6.0, 8.0]
    assert 30.0 in MODULE.sample_times(70, 30)


def test_quiet_split_points() -> None:
    samples = MODULE.np.ones(16000 * 50, dtype=MODULE.np.float32)
    samples[16000 * 21:16000 * 22] = 0
    chunks = MODULE.quiet_split_points(samples, target_seconds=22)
    assert len(chunks) >= 2
    assert chunks[0][0] == 0
    assert chunks[-1][1] == samples.size
    assert all(left_end == right_start for (_, left_end), (right_start, _) in zip(chunks, chunks[1:]))


def test_proofread_text() -> None:
    text, corrections = MODULE.proofread_text("提前十五天运工，西电东宋书店线路")
    assert text == "提前十五天竣工，西电东送输电线路。"
    assert len(corrections) == 3


def test_ocr_subtitle_selection_is_conservative() -> None:
    frames = [
        {
            "timestamp_seconds": 10.0,
            "frame": "frame.jpg",
            "lines": [{"text": "安全等级最高的", "confidence": 0.99}],
        }
    ]
    # 语音结果可靠且比 OCR 完整时，不用残缺字幕覆盖全文。
    assert MODULE.select_ocr_subtitle(
        frames, 9.0, 11.0, "安全等级最高的悬崖施工防护工程", "安全等级最高的悬崖施工防护工程", 0.95, 1.0
    ) is None
    # 语音低置信且附近只有一条字幕时，可以采用 OCR。
    selected = MODULE.select_ocr_subtitle(
        frames, 9.0, 11.0, "安全等急最高的", "安全等级高", 0.5, 0.5
    )
    assert selected and selected["text"] == "安全等级最高的"


if __name__ == "__main__":
    test_clean_text()
    test_similarity()
    test_sample_times()
    test_quiet_split_points()
    test_proofread_text()
    test_ocr_subtitle_selection_is_conservative()
    print("multimodal review tests passed")
