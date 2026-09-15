import unittest

from media_pipeline.full_fusion_qa import (
    build_full_fusion_qa,
    check_file,
    extract_anchors,
    has_usable_asr,
)


def record(asr, ocr, fused, name="课程.mp4"):
    return {
        "source_name": name,
        "sections": {
            "ASR 结果": asr,
            "OCR 结果": ocr,
            "GPT 融合校对结果": fused,
        },
    }


class FullFusionQATests(unittest.TestCase):
    def test_usable_video_asr_is_the_length_baseline(self):
        asr = "甲乙丙丁戊己庚辛壬癸安全生产管理要求必须逐项落实。"
        ocr = "课件补充说明。"
        result = check_file(record([asr], [ocr], [asr]), min_fused_chars=0)
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["asr_usable"])
        self.assertEqual(result["length_baseline_kind"], "asr")
        self.assertEqual(result["length_baseline"], result["asr_source_length"])
        self.assertLess(result["fused_length"], result["combined_source_length"])

        weak_asr = "片头音乐。"
        visual = "课程画面完整说明了第一项要求、第二项要求以及第三项要求。" * 20
        visual_result = check_file(record([weak_asr], [visual], [visual]),
                                   min_fused_chars=0)
        self.assertTrue(visual_result["passed"], visual_result)
        self.assertEqual(visual_result["length_baseline_kind"], "ocr_visual")
        self.assertFalse(visual_result["strong_asr_backbone"])

    def test_no_audio_placeholders_select_deduplicated_ocr_baseline(self):
        placeholders = [
            "不适用：该文件不含音轨。",
            "不适用：该视频不含音轨。",
            "本文件无音频",
            "未检测到可用音频流，因此跳过ASR",
            "no_audio_stream",
        ]
        ocr = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
        for placeholder in placeholders:
            with self.subTest(placeholder=placeholder):
                result = check_file(record([placeholder], [ocr], [ocr]), min_fused_chars=0)
                self.assertTrue(result["passed"], result)
                self.assertFalse(result["asr_usable"])
                self.assertEqual(result["asr_source_length"], 0)
                self.assertEqual(result["length_baseline_kind"], "ocr_visual")
                self.assertEqual(result["length_baseline"], result["ocr_source_length"])

    def test_no_audio_words_inside_real_speech_are_not_a_placeholder(self):
        speech = "本文件不含音轨时应使用OCR提取画面中的课程内容。"
        self.assertTrue(has_usable_asr([speech]))

    def test_ocr_only_anchor_is_mandatory_but_ocr_prose_does_not_inflate_length(self):
        asr = "课程完整讲解了平台建设目标和日常应用方法。"
        ocr = "课件补充设备型号GGYX-10，完成率99.5%，另有大量画面说明文字。"
        missing = check_file(record([asr], [ocr], [asr]), min_fused_chars=0)
        self.assertFalse(missing["passed"])
        self.assertTrue(all(item["source"] == "ocr"
                            for item in missing["anchors"]["missing"]))
        completed = check_file(
            record([asr], [ocr], [asr + "设备型号GGYX-10，完成率99.5%。"]),
            min_fused_chars=0,
        )
        self.assertTrue(completed["passed"], completed)

    def test_phone_visual_facts_are_hard_anchors_but_ocr_debris_is_not(self):
        asr = "电话礼仪要求语言简洁并礼貌结束通话。" * 30
        ocr = ("一般应在早上8点后、晚上10点前拨打；铃响3声前接起；"
               "超过3声应先道歉；每隔20分钟添水；CHINA SOUTHERN POWER GRID；0137")
        missing = check_file(record([asr], [ocr], [asr]), min_fused_chars=0)
        missing_values = {item["canonical"] for item in missing["anchors"]["missing"]}
        self.assertTrue({"8点", "10点", "3声", "20分钟"} <= missing_values)
        self.assertNotIn("CHINA", missing_values)
        self.assertNotIn("137", missing_values)
        completed_text = asr + "一般应在早上8点后、晚上10点前拨打；铃响3声前接起，超过3声应先道歉；每隔20分钟添水。"
        completed = check_file(record([asr], [ocr], [completed_text]),
                               min_fused_chars=0)
        self.assertTrue(completed["passed"], completed)

    def test_semicolon_heavy_raw_ocr_dump_is_rejected(self):
        paragraph = "；".join("识别碎片" for _ in range(70))
        result = check_file(record(["完整讲解。" * 100], [], [paragraph, paragraph]),
                            min_length_retention=0.10, min_fused_chars=0)
        self.assertFalse(result["passed"])
        self.assertTrue(result["raw_ocr_dump_detected"])
        self.assertIn("raw_ocr_dump_without_proofreading", result["failures"])

    def test_only_explicit_verified_ocr_noise_can_be_excluded(self):
        item = record(["完整讲解。" * 100], ["扫描背景误识57年。"],
                      ["完整讲解。" * 100])
        missing = check_file(item, min_fused_chars=0)
        self.assertFalse(missing["passed"])
        canonical = missing["anchors"]["missing"][0]["canonical"]
        item["evidence_status"] = {"verified_ocr_noise_anchors": [{
            "kind": "time", "canonical": canonical,
            "reason": "irrelevant_background",
        }]}
        accepted = check_file(item, min_fused_chars=0)
        self.assertTrue(accepted["passed"], accepted)
        self.assertEqual(accepted["verified_ocr_noise_anchor_count"], 1)

        item["evidence_status"]["verified_ocr_noise_anchors"][0]["canonical"] = "2099年"
        stale = check_file(item, min_fused_chars=0)
        self.assertFalse(stale["passed"])
        self.assertIn("invalid_or_stale_ocr_noise_exclusion", stale["failures"])

    def test_default_retention_threshold_is_ninety_percent(self):
        source = "甲乙丙丁戊己庚辛壬癸"
        result = check_file(record([source], [], [source[:8]]), min_fused_chars=0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["minimum_length_retention_ratio"], 0.90)

    def test_complete_copy_allows_deduplication_and_punctuation_changes(self):
        source = "安全生产责任制，必须落实。安全生产责任制，必须落实。"
        item = record([source], ["安全生产责任制；必须落实。"],
                      ["安全生产责任制必须落实！"])
        result = check_file(item, min_length_retention=0.75, min_fused_chars=0)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["length_retention_ratio"], 0.75)

    def test_chinese_and_arabic_forms_cover_exam_anchors(self):
        source = ("培训于二〇二四年十月九日14:30开展，共五十八条，"
                  "完成率百分之八十六点三，共一点五亿台设备，"
                  "电压为500kV，使用API和GIS。")
        fused = ("培训于2024年10月9日14：30开展，共58条；完成率86.3%，"
                 "共1.5亿台设备，电压为500 kV，使用 API 和 GIS。")
        result = check_file(record([source], [], [fused]),
                            min_length_retention=0.70, min_fused_chars=0)
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["length_retention_passed"])
        self.assertTrue(result["anchors"]["all_covered"])
        self.assertEqual(result["anchors"]["missing"], [])
        kinds = {item["kind"] for item in extract_anchors([source])}
        self.assertTrue({"time", "percentage", "number", "model", "acronym"} <= kinds)

    def test_full_date_covers_split_month_and_day_but_not_duration(self):
        source = "画面依次显示2月、10日和7年。"
        fused = "通知日期为1987年2月10日。"
        result = check_file(record(["完整正文。" * 100], [source],
                                   ["完整正文。" * 100 + fused]), min_fused_chars=0)
        missing = {item["canonical"] for item in result["anchors"]["missing"]}
        self.assertNotIn("2月", missing)
        self.assertNotIn("10日", missing)
        self.assertIn("7年", missing)

    def test_ordinary_yishi_is_not_a_time_but_arabic_hour_is(self):
        anchors = {(item["kind"], item["canonical"])
                   for item in extract_anchors(["一时难以完成，设备运行1时。"])}
        self.assertNotIn(("time", "1时"), {
            (item["kind"], item["canonical"])
            for item in extract_anchors(["一时难以完成。"])
        })
        self.assertIn(("time", "1时"), anchors)

    def test_pole_number_is_quantity_not_calendar_day(self):
        anchors = {(item["kind"], item["canonical"])
                   for item in extract_anchors(["91号杆与10号设备相连。"])}
        self.assertIn(("number", "91号"), anchors)
        self.assertNotIn(("time", "91日"), anchors)
        self.assertIn(("time", "10日"), anchors)

    def test_model_codes_accept_unicode_dashes(self):
        source = "执行Q/CSG211001—2016和Q/CSG-YNPG2150003–2017标准。"
        output = "执行Q/CSG211001-2016和Q/CSG-YNPG2150003-2017标准。"
        result = check_file(record([source], [], [output]),
                            min_length_retention=0.50, min_fused_chars=0)
        self.assertTrue(result["passed"], result)

    def test_summary_fails_and_lists_every_missing_anchor(self):
        source = "。".join(f"第{i}项设备{i}号完成率{i}%并使用API" for i in range(1, 31))
        result = check_file(record([source], [], ["本课程介绍了设备管理要点。"]),
                            min_length_retention=0.80, min_fused_chars=120)
        self.assertFalse(result["passed"])
        self.assertTrue(result["summary_like_short_output"])
        self.assertIn("length_retention_below_threshold", result["failures"])
        self.assertIn("summary_like_short_output", result["failures"])
        self.assertIn("missing_exam_anchors", result["failures"])
        self.assertFalse(result["anchors"]["all_covered"])
        missing = {(item["kind"], item["canonical"])
                   for item in result["anchors"]["missing"]}
        self.assertIn(("percentage", "30%"), missing)
        self.assertIn(("acronym", "API"), missing)

    def test_missing_model_and_acronym_are_reported(self):
        source = "设备型号GGYX-10接入CA平台，使用AOPA证书。"
        result = check_file(record([source], [], ["设备接入平台并使用证书。"]),
                            min_length_retention=0.10, min_fused_chars=0)
        missing = {(item["kind"], item["canonical"])
                   for item in result["anchors"]["missing"]}
        self.assertIn(("model", "GGYX10"), missing)
        self.assertIn(("acronym", "CA"), missing)
        self.assertIn(("acronym", "AOPA"), missing)

    def test_forbidden_fields_are_detected_without_echoing_secret(self):
        secret = "sk-" + "thisMustNeverAppearInReport123"
        fused = ("完整正文。frame_index: 12；timestamp_seconds=3.5；"
                 f"api_key={secret}")
        result = check_file(record(["完整正文。"], [], [fused]),
                            min_length_retention=0.10, min_fused_chars=0)
        self.assertFalse(result["passed"])
        self.assertIn("forbidden_technical_or_secret_fields", result["failures"])
        rendered = str(result)
        self.assertNotIn(secret, rendered)
        kinds = {item["kind"] for item in result["forbidden_matches"]}
        self.assertTrue({"secret_value", "secret_token", "frame_field",
                         "timestamp_field"} <= kinds)

    def test_tp_secret_token_is_rejected_and_never_becomes_an_anchor(self):
        secret = "tp-" + "anotherSecretToken123"
        result = check_file(record(["完整正文。"], [], [f"完整正文。{secret}"]),
                            min_length_retention=0.10, min_fused_chars=0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["forbidden_matches"],
                         [{"kind": "secret_token", "count": 1}])
        self.assertNotIn(secret, str(result))
        self.assertEqual(extract_anchors([secret]), [])

    def test_corpus_report_has_per_file_results_and_total_boolean(self):
        good = record(["第一条要求。"], [], ["第一条要求。"], "完整.mp4")
        bad = record(["第二条要求和API规范。"], [], ["要求。"], "摘要.mp4")
        result = build_full_fusion_qa(
            {"files": [good, bad]}, min_length_retention=0.75, min_fused_chars=0)
        self.assertEqual(result["file_count"], 2)
        self.assertEqual([item["source_name"] for item in result["files"]],
                         ["完整.mp4", "摘要.mp4"])
        self.assertTrue(result["files"][0]["passed"])
        self.assertFalse(result["files"][1]["passed"])
        self.assertFalse(result["passed"])

    def test_invalid_section_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "字符串"):
            check_file(record({"text": "错误"}, [], ["正文"]), min_fused_chars=0)


if __name__ == "__main__":
    unittest.main()
