import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_pipeline.full_fusion_review import (
    CACHE_SCHEMA,
    MODEL,
    FusionReviewError,
    IncompleteResponseError,
    LosslessAuditError,
    acquire_api_key,
    audit_lossless,
    build_payload,
    build_messages,
    cache_path,
    load_cached,
    load_tasks,
    make_task,
    merge_results,
    review_one,
    run_pipeline,
    split_task,
    validate_response,
)


def source_record(*, fused="旧的摘要，不应进入输入哈希。"):
    return {
        "source_name": "培训.mp4",
        "source_sha256": "a" * 64,
        "sections": {
            "ASR 结果": ["2020年完成35项任务。第一步核对，第二步复查。"],
            "OCR 结果": ["2020年；35项；例外：停电时先报告。"],
            "GPT 融合校对结果": [fused],
        },
    }


def complete_response(paragraph=None):
    text = paragraph or "2020年完成35项任务。第一步核对，第二步复查。例外：停电时先报告。"
    return {
        "id": "request-1",
        "model": MODEL,
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"paragraphs": [text]}, ensure_ascii=False)},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "unsafe": "drop"},
    }


def filtered_response(reason="content_filter"):
    response = complete_response()
    response["choices"][0]["finish_reason"] = reason
    response["choices"][0]["message"]["content"] = ""
    return response


def response_for_payload(payload):
    evidence = json.loads(payload["messages"][1]["content"])
    text = "".join(evidence["ASR 结果（完整）"] + evidence["OCR 结果（完整）"])
    return complete_response(text)


class FullFusionReviewTests(unittest.TestCase):
    def test_task_hash_uses_only_asr_ocr_not_old_fusion(self):
        first = make_task(source_record(fused="旧稿甲"))
        second = make_task(source_record(fused="旧稿乙"))
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        changed = source_record()
        changed["sections"]["OCR 结果"].append("新增事实")
        self.assertNotEqual(first["input_sha256"], make_task(changed)["input_sha256"])

    def test_messages_include_complete_both_streams_and_forbid_summary(self):
        task = make_task(source_record())
        messages = build_messages(task)
        self.assertIn("不是摘要", messages[0]["content"])
        self.assertIn("每个事实", messages[0]["content"])
        user = json.loads(messages[1]["content"])
        self.assertEqual(user["ASR 结果（完整）"], task["asr"])
        self.assertEqual(user["OCR 结果（完整）"], task["ocr"])

    def test_payload_reserves_long_output_and_requests_json(self):
        payload = build_payload(make_task(source_record()))
        self.assertEqual(payload["max_completion_tokens"], 65536)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_recursive_child_disables_thinking_but_keeps_full_budget(self):
        child = split_task(make_task(source_record()))[0]
        payload = build_payload(child)
        self.assertEqual(payload["max_completion_tokens"], 65536)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_child_tolerance_never_lowers_the_root_audit(self):
        task = make_task(source_record())
        children = split_task(task)
        self.assertIsNotNone(children)
        # The public audit remains the final-document 90% gate.  Recursive
        # tolerance is applied only inside review_one to small child chunks.
        with self.assertRaises(LosslessAuditError):
            audit_lossless(task, ["2020年完成35项任务。"])

    def test_loader_rejects_malformed_sections_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fused.json"
            value = {"files": [source_record(), source_record()]}
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复"):
                load_tasks(path)
            value = {"files": [source_record()]}
            value["files"][0]["sections"]["ASR 结果"] = "不是数组"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "字符串数组"):
                load_tasks(path)

    def test_response_requires_stop_and_exact_model(self):
        response = complete_response()
        response["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(IncompleteResponseError):
            validate_response(response, api_key="secret")
        response = complete_response()
        response["model"] = "another-model"
        with self.assertRaisesRegex(FusionReviewError, "模型"):
            validate_response(response, api_key="secret")

    def test_response_rejects_credential_echo_and_extra_content_fields(self):
        response = complete_response("结果中不应出现 key-very-secret")
        with self.assertRaisesRegex(FusionReviewError, "认证信息"):
            validate_response(response, api_key="key-very-secret")
        response = complete_response()
        response["choices"][0]["message"]["content"] = json.dumps({
            "paragraphs": ["正文"], "summary": "摘要"
        }, ensure_ascii=False)
        with self.assertRaisesRegex(FusionReviewError, "只包含"):
            validate_response(response, api_key="key-very-secret")

    def test_lossless_audit_rejects_short_summary_and_missing_number(self):
        task = make_task(source_record())
        with self.assertRaises(LosslessAuditError):
            audit_lossless(task, ["任务完成。"])
        with self.assertRaisesRegex(LosslessAuditError, "缺少考试锚点"):
            audit_lossless(task, ["2020年完成工作。第一步核对，第二步复查。例外：停电时先报告。"],
                           minimum_ratio=0.01)
        audit = audit_lossless(
            task, ["2020年完成35项任务。第一步核对，第二步复查。例外：停电时先报告。"])
        self.assertTrue(audit["exam_anchors_preserved"])

    def test_review_one_caches_safe_selected_fields_and_resumes(self):
        task = make_task(source_record())
        calls = []

        def request(payload, key, **kwargs):
            calls.append((payload, key, kwargs))
            return complete_response()

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            result, hit = review_one(task, cache_dir, "key-very-secret",
                                     request_fn=request)
            self.assertFalse(hit)
            self.assertEqual(len(calls), 1)
            saved = cache_path(cache_dir, task).read_text(encoding="utf-8")
            self.assertNotIn("key-very-secret", saved)
            self.assertEqual(json.loads(saved)["schema_version"], CACHE_SCHEMA)
            resumed, hit = review_one(task, cache_dir, "key-very-secret",
                                      request_fn=request)
            self.assertTrue(hit)
            self.assertEqual(resumed["paragraphs"], result["paragraphs"])
            self.assertEqual(len(calls), 1)

    def test_lossless_failure_gets_one_focused_repair_before_splitting(self):
        task = make_task(source_record())
        calls = []

        def request(payload, key, **kwargs):
            calls.append(payload)
            if len(calls) == 1:
                return complete_response("2020年完成任务。")
            return complete_response()

        with tempfile.TemporaryDirectory() as directory:
            result, hit = review_one(task, Path(directory), "secret-key",
                                     request_fn=request)
            self.assertFalse(hit)
            self.assertEqual(result["strategy"], "single-call-repair")
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(calls[1]["messages"]), 3)
            self.assertIn("35项", calls[1]["messages"][2]["content"])

    def test_incomplete_response_is_never_cached(self):
        task = make_task(source_record())
        response = complete_response()
        response["choices"][0]["finish_reason"] = "length"
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with self.assertRaises(IncompleteResponseError):
                review_one(task, cache_dir, "secret-key",
                           request_fn=lambda *args, **kwargs: response)
            self.assertFalse(cache_path(cache_dir, task).exists())

    def test_parent_filter_falls_back_to_two_successful_relative_chunks(self):
        task = make_task(source_record())
        calls = []

        def request(payload, key, **kwargs):
            calls.append(payload)
            if len(calls) == 1:
                return filtered_response()
            return response_for_payload(payload)

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            result, hit = review_one(
                task, cache_dir, "secret-key", request_fn=request,
                min_chunk_characters=1)
            self.assertFalse(hit)
            self.assertEqual(len(calls), 3)
            self.assertEqual(result["strategy"], "recursive-split")
            children = split_task(task)
            self.assertIsNotNone(children)
            self.assertNotEqual(children[0]["input_sha256"], children[1]["input_sha256"])
            self.assertTrue(all(cache_path(cache_dir, child).is_file()
                                for child in children))
            self.assertTrue(all(
                json.loads(payload["messages"][1]["content"])["当前证据块"]["相对位置"]
                in {"前半", "后半"} for payload in calls[1:]))

    def test_child_caches_are_reused_when_parent_cache_is_missing(self):
        task = make_task(source_record())

        def first_request(payload, key, **kwargs):
            if "当前证据块" not in json.loads(payload["messages"][1]["content"]):
                return filtered_response()
            return response_for_payload(payload)

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            review_one(task, cache_dir, "secret-key", request_fn=first_request,
                       min_chunk_characters=1)
            cache_path(cache_dir, task).unlink()
            second_calls = []

            def second_request(payload, key, **kwargs):
                second_calls.append(payload)
                return filtered_response()

            result, hit = review_one(
                task, cache_dir, "secret-key", request_fn=second_request,
                min_chunk_characters=1)
            self.assertFalse(hit)
            self.assertEqual(len(second_calls), 0)
            self.assertEqual(result["strategy"], "proactive-split")

    def test_recursive_fallback_stops_at_configured_minimum(self):
        task = make_task(source_record())
        calls = []

        def request(*args, **kwargs):
            calls.append(True)
            return filtered_response()

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(IncompleteResponseError):
                review_one(
                    task, Path(directory), "secret-key", request_fn=request,
                    min_chunk_characters=1, max_split_depth=1)
            # Root is split, then its first minimum-depth child fails.  The
            # unvisited sibling is not requested blindly.
            self.assertEqual(len(calls), 2)

    def test_non_filter_provider_errors_do_not_trigger_splitting(self):
        task = make_task(source_record())
        calls = []

        def request(*args, **kwargs):
            calls.append(True)
            raise FusionReviewError("MiMo HTTP 400：请求被拒绝。")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FusionReviewError, "HTTP 400"):
                review_one(task, Path(directory), "secret-key",
                           request_fn=request, min_chunk_characters=1)
            self.assertEqual(len(calls), 1)

    def test_load_cached_rejects_wrong_input_hash(self):
        task = make_task(source_record())
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            path = cache_path(cache_dir, task)
            path.parent.mkdir(parents=True)
            value = {
                "schema_version": CACHE_SCHEMA,
                "model": MODEL,
                "prompt_version": "lossless-full-fusion-zh-v4-recursive",
                "source_name": task["source_name"],
                "source_sha256": task["source_sha256"],
                "input_sha256": "wrong",
                "finish_reason": "stop",
                "paragraphs": ["2020年完成35项任务。第一步核对，第二步复查。例外：停电时先报告。"],
            }
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(load_cached(task, cache_dir))

    def test_merge_keeps_input_order_and_only_fused_section(self):
        first = make_task(source_record())
        other_record = source_record()
        other_record["source_name"] = "另一文件.mp4"
        second = make_task(other_record)
        results = {}
        for task in (first, second):
            results[task["source_name"]] = {
                "input_sha256": task["input_sha256"],
                "finish_reason": "stop",
                "paragraphs": ["校对全文"],
            }
        merged = merge_results({"source_document_sha256": "digest"},
                               [second, first], results)
        self.assertEqual([item["source_name"] for item in merged["files"]],
                         [second["source_name"], first["source_name"]])
        self.assertEqual(set(merged["files"][0]["sections"]), {"GPT 融合校对结果"})

    def test_default_pipeline_is_plan_only_and_does_not_request_key_or_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fused.json"
            source.write_text(json.dumps({"schema_version": "x", "file_count": 1,
                                          "files": [source_record()]}, ensure_ascii=False),
                              encoding="utf-8")
            called = []

            def forbidden(*args, **kwargs):
                called.append(True)
                raise AssertionError("must not call")

            with patch("media_pipeline.full_fusion_review.acquire_api_key",
                       side_effect=AssertionError("must not read key")):
                plan = run_pipeline(source, root / "cache", root / "output.json",
                                    request_fn=forbidden)
            self.assertEqual(plan["mode"], "plan")
            self.assertEqual(plan["pending"], 1)
            self.assertFalse(called)
            self.assertFalse((root / "output.json").exists())

    def test_execute_runs_workers_and_writes_final_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [source_record()]
            second = source_record()
            second["source_name"] = "第二课.mp4"
            records.append(second)
            source = root / "fused.json"
            source.write_text(json.dumps({"schema_version": "x", "file_count": 2,
                                          "files": records}, ensure_ascii=False),
                              encoding="utf-8")
            seen_models = []

            def request(payload, key, **kwargs):
                seen_models.append(payload["model"])
                self.assertEqual(key, "secret-key")
                return complete_response()

            output = root / "output.json"
            result = run_pipeline(source, root / "cache", output, execute=True,
                                  workers=2, api_key="secret-key", request_fn=request)
            self.assertTrue(result["output_ready"])
            self.assertEqual(seen_models, [MODEL, MODEL])
            merged = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(merged["file_count"], 2)
            self.assertNotIn("secret-key", output.read_text(encoding="utf-8"))

    def test_key_comes_from_environment_or_hidden_prompt(self):
        self.assertEqual(acquire_api_key(env={"MIMO_API_KEY": " env-secret "},
                                         stdin_isatty=False), "env-secret")
        with patch("media_pipeline.full_fusion_review.getpass.getpass",
                   return_value="prompt-secret"):
            self.assertEqual(acquire_api_key(env={}, stdin_isatty=True), "prompt-secret")
        with self.assertRaises(FusionReviewError):
            acquire_api_key(env={}, stdin_isatty=False)


if __name__ == "__main__":
    unittest.main()
