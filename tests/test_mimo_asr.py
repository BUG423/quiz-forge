"""Offline reliability tests: never calls a real endpoint."""
import base64
import io
import json
import tempfile
import unittest
import urllib.error
import wave
from pathlib import Path
from unittest.mock import patch

import av
import numpy as np

from media_pipeline import asr


def response(text="测试原文。", reason="stop"):
    return {"model": asr.MODEL, "id": "test-request", "choices": [
        {"finish_reason": reason, "message": {"content": text}}],
        "usage": {"total_tokens": 12, "prompt_tokens_details": {"audio_tokens": 10}}}


class TestMimoASR(unittest.TestCase):
    def test_token_plan_base_url_is_normalized_and_untrusted_hosts_are_rejected(self):
        endpoint = asr._configured_endpoint("https://token-plan-cn.xiaomimimo.com/v1")
        self.assertEqual(endpoint, "https://token-plan-cn.xiaomimimo.com/v1/chat/completions")
        with self.assertRaises(asr.ASRError):
            asr._configured_endpoint("https://example.com/v1")
        with self.assertRaises(asr.ASRError):
            asr._configured_endpoint("http://token-plan-cn.xiaomimimo.com/v1")

    def test_timestamp_overlap_trims_only_already_covered_samples(self):
        values = np.arange(341, dtype=np.int16)
        trimmed, count = asr._trim_timestamp_overlap(values, -100)
        self.assertEqual(count, 100)
        np.testing.assert_array_equal(trimmed, values[100:])
        unchanged, count = asr._trim_timestamp_overlap(values, -2)
        self.assertEqual(count, 0)
        np.testing.assert_array_equal(unchanged, values)
        with self.assertRaises(asr.ASRError):
            asr._trim_timestamp_overlap(values, -342)

    def test_payload_matches_official_schema(self):
        wav = asr._wav_bytes(np.zeros(1600, dtype=np.int16))
        with patch.object(asr._OPENER, "open", return_value=io.BytesIO(json.dumps(response()).encode())) as request:
            result = asr._request_json(wav, "private-test-key")
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, asr.ENDPOINT)
        self.assertEqual(req.get_header("Api-key"), "private-test-key")
        payload = json.loads(req.data)
        self.assertEqual(payload["asr_options"], {"language": "zh"})
        self.assertEqual(len(payload["messages"]), 1)
        content = payload["messages"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "input_audio")
        self.assertEqual(base64.b64decode(content[0]["input_audio"]["data"].split(",", 1)[1]), wav)
        self.assertEqual(result["model"], asr.MODEL)

    def test_retries_transient_http_and_network(self):
        failures = [urllib.error.HTTPError(asr.ENDPOINT, 429, "private detail", {"Retry-After": "0"}, None),
                    urllib.error.HTTPError(asr.ENDPOINT, 503, "private detail", {}, None),
                    urllib.error.URLError("private detail")]
        with patch.object(asr._OPENER, "open", side_effect=failures + [io.BytesIO(json.dumps(response()).encode())]) as send:
            with patch.object(asr.time, "sleep") as sleep:
                self.assertEqual(asr._request_json(b"wav", "private-key")["id"], "test-request")
        self.assertEqual(send.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_auth_and_permanent_errors_not_retried_or_leaked(self):
        for status in (400, 401, 403, 302):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(asr.ENDPOINT, status, "private-key", {}, io.BytesIO(b"private-key"))
                with patch.object(asr._OPENER, "open", side_effect=error) as send:
                    with self.assertRaises(asr.ASRError) as caught:
                        asr._request_json(b"wav", "private-key")
                self.assertEqual(send.call_count, 1)
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn("private-key", str(caught.exception))

    def test_network_retry_is_finite(self):
        with patch.object(asr._OPENER, "open", side_effect=urllib.error.URLError("secret")) as send:
            with patch.object(asr.time, "sleep"), self.assertRaises(asr.ASRError) as caught:
                asr._request_json(b"wav", "secret", max_attempts=3)
        self.assertEqual(send.call_count, 3)
        self.assertNotIn("secret", str(caught.exception))

    def test_truncation_and_filtering_fail(self):
        for reason in ("length", "content_filter", None, "unexpected"):
            with self.subTest(reason=reason), self.assertRaises(asr.ASRError):
                asr._validate_response(response(reason=reason))
        self.assertEqual(asr._validate_response(response(text=""))["text"], "")

    def test_malformed_response_and_corrupt_cache_are_not_accepted(self):
        for malformed in ([], None, {}, response(reason=["stop"])):
            with self.subTest(malformed=malformed), self.assertRaises(asr.ASRError):
                asr._validate_response(malformed)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "test.wav"
            source.write_bytes(asr._wav_bytes(np.zeros(1600, dtype=np.int16)))
            work = Path(folder) / "work"
            with patch.object(asr, "_request_json", return_value=response()):
                asr.transcribe(source, work, "test-key")
            cache_path = next(work.rglob("segment-*.json"))
            cache = json.loads(cache_path.read_text())
            cache["response"] = []
            cache_path.write_text(json.dumps(cache))
            with patch.object(asr, "_request_json", return_value=response()) as send:
                asr.transcribe(source, work, "test-key")
            send.assert_called_once()

    def test_credential_echo_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "test.wav"
            source.write_bytes(asr._wav_bytes(np.zeros(1600, dtype=np.int16)))
            work = Path(folder) / "work"
            with patch.object(asr, "_request_json", return_value=response(text="private-key")):
                with self.assertRaises(asr.ASRError):
                    asr.transcribe(source, work, "private-key")
            self.assertFalse(list(work.rglob("*.json")))

    def test_segmentation_exactly_covers_samples_and_chooses_quiet(self):
        samples = np.full(125 * asr.SAMPLE_RATE + 137, 5000, dtype=np.int16)
        samples[51 * asr.SAMPLE_RATE:52 * asr.SAMPLE_RATE] = 0
        intervals = asr._split_audio(samples, 55)
        self.assertGreater(intervals[0][1], 51 * asr.SAMPLE_RATE)
        self.assertLess(intervals[0][1], 52 * asr.SAMPLE_RATE)
        self.assertEqual(intervals[0][0], 0)
        self.assertEqual(intervals[-1][1], len(samples))
        self.assertEqual(sum(end - begin for begin, end in intervals), len(samples))
        for previous, current in zip(intervals, intervals[1:]):
            self.assertEqual(previous[1], current[0])
        self.assertTrue(all(0 < end - begin <= 55 * asr.SAMPLE_RATE for begin, end in intervals))

    def test_no_audio_file_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "silent.mkv"
            with av.open(str(source), "w") as container:
                stream = container.add_stream("ffv1", rate=1)
                stream.width, stream.height = 16, 16
                for packet in stream.encode(av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format="rgb24")):
                    container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            with self.assertRaises(asr.NoAudioError):
                asr._decode_audio(source)

    def test_real_decode_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "test.wav"
            original = np.arange(asr.SAMPLE_RATE * 3 + 37, dtype=np.int16)
            source.write_bytes(asr._wav_bytes(original))
            decoded, metadata = asr._decode_audio(source)
            np.testing.assert_array_equal(decoded, original)
            self.assertEqual(metadata["decoded_sample_count"], len(original))
            with patch.object(asr, "_request_json", return_value=response()) as send:
                first = asr.transcribe(source, Path(folder) / "work", "private-key", chunk_seconds=2)
                self.assertEqual(send.call_count, len(first["segments"]))
                send.reset_mock()
                second = asr.transcribe(source, Path(folder) / "work", "private-key", chunk_seconds=2)
                send.assert_not_called()
                self.assertEqual(second["coverage"]["cached_segment_count"], len(first["segments"]))
                asr.transcribe(source, Path(folder) / "work", "private-key", chunk_seconds=3)
                self.assertGreater(send.call_count, 0)
                send.reset_mock()
                original[0] += 1
                source.write_bytes(asr._wav_bytes(original))
                asr.transcribe(source, Path(folder) / "work", "private-key", chunk_seconds=2)
                self.assertGreater(send.call_count, 0)
            for path in (Path(folder) / "work").rglob("*.json"):
                self.assertNotIn("private-key", path.read_text())
            self.assertTrue(first["coverage"]["complete"])
            self.assertEqual(first["coverage"]["covered_sample_count"], len(original))

    def test_content_filtered_chunk_is_split_and_completed_chunks_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "test.wav"
            source.write_bytes(asr._wav_bytes(np.zeros(asr.SAMPLE_RATE * 20, dtype=np.int16)))
            work = Path(folder) / "work"
            with patch.object(asr, "_request_json", side_effect=[
                    response(), response(reason="content_filter"), response(), response(), response()]):
                result = asr.transcribe(source, work, "test-key", chunk_seconds=10)
            self.assertEqual(len(list(work.rglob("segment-*.json"))), 3)
            self.assertTrue(result["segments"][1]["content_filter_split"])
            with patch.object(asr, "_request_json", return_value=response()) as send:
                resumed = asr.transcribe(source, work, "test-key", chunk_seconds=10)
            send.assert_not_called()
            self.assertEqual(len(resumed["segments"]), 3)
            self.assertEqual(result["usage"]["total_tokens"], 48)


if __name__ == "__main__":
    unittest.main()
