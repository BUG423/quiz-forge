"""Offline MiMo image adapter tests: all network calls are mocked."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from media_pipeline import vision


def response(indices=(0,), reason="stop", fenced=False, text="[字幕/subtitles] 测试"):
    result = {"frames": [{"frame_index": i, "visible_text": [text],
                          "supplementary_facts": [], "uncertain": []} for i in indices]}
    content = json.dumps(result, ensure_ascii=False)
    if fenced:
        content = "```json\n" + content + "\n```"
    return {"model": vision.MODEL, "id": "mock-response", "usage": {"total_tokens": 10},
            "choices": [{"finish_reason": reason, "message": {"content": content}}]}


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "image.jpg"
        self.image.write_bytes(b"\xff\xd8\xffoffline-image")
        self.frames = [{"index": 0, "timestamp_seconds": 0.0, "image_path": str(self.image),
                        "lines": [{"text": "故意错误的OCR候选"}]}]

    def run_review(self, frames=None, source="a" * 64, **kwargs):
        return vision.review_images(frames or self.frames, self.root / "work", "private-test-key",
                                    source_sha256=source, **kwargs)

    def test_json_and_markdown_wrapped_json(self):
        for wrapped in (False, True):
            result = vision._parse_response(response(indices=(2, 0), fenced=wrapped), [0, 2])
            self.assertEqual([f["frame_index"] for f in result["frames"]], [0, 2])

    def test_truncation_missing_duplicate_and_extra_frames_fail(self):
        for reason in ("length", "content_filter", None):
            with self.subTest(reason=reason), self.assertRaises(vision.VisionError):
                vision._parse_response(response(reason=reason), [0])
        for indices in ((), (0, 0), (0, 1), (1,)):
            with self.subTest(indices=indices), self.assertRaises(vision.VisionError):
                vision._parse_response(response(indices=indices), [0])

    def test_official_request_payload_and_no_candidate_leak(self):
        with patch.object(vision._OPENER, "open", return_value=io.BytesIO(json.dumps(response()).encode())) as send:
            result = self.run_review()
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, vision.ENDPOINT)
        self.assertEqual(request.get_header("Api-key"), "private-test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "mimo-v2.5")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_completion_tokens"], 7000)
        self.assertNotIn("故意错误", request.data.decode())
        self.assertEqual(sum(c["type"] == "image_url" for c in payload["messages"][0]["content"]), 1)
        self.assertEqual(result["scope"], "explicitly_selected_representative_frames_only")

    def test_retry_transient_but_never_auth(self):
        failures = [urllib.error.HTTPError(vision.ENDPOINT, 429, "private", {}, None),
                    urllib.error.HTTPError(vision.ENDPOINT, 503, "private", {}, None),
                    urllib.error.URLError("private")]
        with patch.object(vision._OPENER, "open", side_effect=failures + [io.BytesIO(json.dumps(response()).encode())]) as send:
            with patch.object(vision.time, "sleep"):
                vision._request_json({}, "private-test-key")
        self.assertEqual(send.call_count, 4)
        for status in (401, 403):
            error = urllib.error.HTTPError(vision.ENDPOINT, status, "private-test-key", {}, None)
            with patch.object(vision._OPENER, "open", side_effect=error) as send:
                with self.assertRaises(vision.VisionError) as caught:
                    vision._request_json({}, "private-test-key")
            self.assertEqual(send.call_count, 1)
            self.assertNotIn("private-test-key", str(caught.exception))

    def test_credentials_not_persisted_and_echo_rejected(self):
        with patch.object(vision, "_request_json", return_value=response(text="private-test-key")):
            with self.assertRaises(vision.VisionError):
                self.run_review()
        self.assertFalse(list((self.root / "work").rglob("*.json")))
        with patch.object(vision, "_request_json", return_value=response()):
            self.run_review()
        for path in (self.root / "work").rglob("*.json"):
            data = path.read_text()
            self.assertNotIn("private-test-key", data)
            self.assertNotIn("data:image", data)

    def test_cache_changes_with_source_image_prompt_and_frame_time(self):
        with patch.object(vision, "_request_json", return_value=response()) as send:
            self.run_review()
            self.run_review()
            self.assertEqual(send.call_count, 1)
            self.run_review(source="b" * 64)
            self.assertEqual(send.call_count, 2)
            self.image.write_bytes(b"\xff\xd8\xffchanged-image")
            self.run_review()
            self.assertEqual(send.call_count, 3)
            with patch.object(vision, "PROMPT", vision.PROMPT + "额外说明"):
                self.run_review()
            self.assertEqual(send.call_count, 4)
            self.frames[0]["timestamp_seconds"] = 1.0
            self.run_review()
            self.assertEqual(send.call_count, 5)

    def test_failed_batch_not_cached_and_completed_batch_resumes(self):
        frames = self.frames + [{**self.frames[0], "index": 2, "timestamp_seconds": 2.0}]
        with patch.object(vision, "_request_json", side_effect=[response(), response(indices=(2,), reason="length")]):
            with self.assertRaises(vision.VisionError):
                self.run_review(frames, batch_size=1)
        self.assertEqual(len(list((self.root / "work" / "vision_cache").glob("*.json"))), 1)
        with patch.object(vision, "_request_json", return_value=response(indices=(2,))) as send:
            result = self.run_review(frames, batch_size=1)
        send.assert_called_once()
        self.assertEqual(result["reviewed_frame_indices"], [0, 2])
        self.assertEqual(result["usage"]["total_tokens"], 20)

    def test_input_duplicate_indices_fails_before_request(self):
        with patch.object(vision, "_request_json") as send:
            with self.assertRaises(ValueError):
                self.run_review(self.frames * 2)
        send.assert_not_called()

    def test_invalid_key_and_network_exhaustion_are_sanitized(self):
        with self.assertRaises(vision.VisionError) as caught:
            vision.review_images(self.frames, self.root / "work", "private\nkey", source_sha256="a" * 64)
        self.assertNotIn("private", str(caught.exception))
        with patch.object(vision._OPENER, "open", side_effect=urllib.error.URLError("private-test-key")) as send:
            with patch.object(vision.time, "sleep"), self.assertRaises(vision.VisionError) as caught:
                vision._request_json({}, "private-test-key", max_attempts=2)
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("private-test-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
