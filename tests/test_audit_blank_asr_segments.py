import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from scripts import audit_blank_asr_segments as audit
from scripts import confirm_blank_asr_with_whisper as whisper_audit


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeReviewer:
    engine = "fake-local"
    model_id = "fake-sensevoice"

    def recognize(self, samples, sample_rate):
        assert sample_rate == audit.SAMPLE_RATE
        return "<|zh|> 本地检测到语音 " if np.any(samples) else "<|nospeech|>"


class BlankAsrAuditTests(unittest.TestCase):
    def test_control_tags_classification_and_exact_partition(self):
        self.assertEqual(audit.clean_sensevoice_text("<|zh|><|NEUTRAL|> 测 试 "), "测 试")
        self.assertEqual(audit.classify_review("检测到语音", 0.02, silence_rms=0.001), "speech_detected")
        self.assertEqual(audit.classify_review("", 0.0, silence_rms=0.001), "no_speech")
        self.assertEqual(audit.classify_review("", 0.02, silence_rms=0.001), "needs_manual")
        self.assertEqual(audit.classify_review("啊", 0.02, silence_rms=0.001), "needs_manual")
        self.assertEqual(audit.classify_review("我何", 0.02, silence_rms=0.001), "needs_manual")
        self.assertEqual(audit.split_exact(11, 4), [(0, 4), (4, 8), (8, 11)])

    def test_fixture_audit_keeps_mimo_immutable_and_queues_only_speech(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            direct_bytes, parent_bytes, embedded_bytes = b"direct", b"parent", b"embedded"
            direct_sha, parent_sha, embedded_sha = map(digest, (direct_bytes, parent_bytes, embedded_bytes))
            direct = root / "data/2025/direct.mp4"
            parent = root / "data/2025/slides.pptx"
            embedded = root / ".work/embedded-office/2025/clip.wmv"
            for path, content in ((direct, direct_bytes), (parent, parent_bytes), (embedded, embedded_bytes)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            asset_manifest = root / "asset_manifest.json"
            asset_manifest.write_text(json.dumps({"assets": [
                {
                    "asset_id": f"2025/video/{direct_sha}",
                    "source_path": "data/2025/direct.mp4",
                    "source_sha256": direct_sha,
                    "kind": "video",
                },
                {
                    "asset_id": f"2025/courseware/{parent_sha}",
                    "source_path": "data/2025/slides.pptx",
                    "source_sha256": parent_sha,
                    "kind": "courseware",
                },
            ]}), encoding="utf-8")
            embedded_manifest = root / "embedded_manifest.json"
            embedded_manifest.write_text(json.dumps({"files": [{
                "parent_source": str(parent),
                "output_path": str(embedded),
                "sha256": embedded_sha,
                "slide": 3,
                "member": "ppt/media/media1.wmv",
            }]}), encoding="utf-8")

            asr_root = root / ".work/single-video"
            asr_paths = []
            for source_sha, start, end in ((direct_sha, 4, 12), (embedded_sha, 0, 8)):
                path = asr_root / source_sha[:16] / "asr.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "model": audit.MIMO_MODEL,
                    "source_sha256": source_sha,
                    "configuration": {"endpoint": audit.MIMO_ENDPOINT},
                    "segments": [{
                        "index": 0,
                        "sample_start": start,
                        "sample_end": end,
                        "start_seconds": start / audit.SAMPLE_RATE,
                        "end_seconds": end / audit.SAMPLE_RATE,
                        "text": "",
                    }],
                    "audio": {"sample_rate": audit.SAMPLE_RATE, "sample_count": 16},
                    "coverage": {"complete": True},
                }), encoding="utf-8")
                asr_paths.append(path)
            before = [path.read_bytes() for path in asr_paths]

            def decoder(path):
                values = np.zeros(16, dtype=np.int16)
                if path == direct:
                    values[4:12] = 8192
                return values, {
                    "sample_rate": audit.SAMPLE_RATE,
                    "channels": 1,
                    "sample_count": len(values),
                }

            result = audit.build_review(
                root=root,
                asr_root=asr_root,
                asset_manifest_path=asset_manifest,
                embedded_manifest_path=embedded_manifest,
                output_path=None,
                reviewer=FakeReviewer(),
                decoder=decoder,
                local_chunk_seconds=0.00025,
                expected_asr_files=2,
                expected_blank_segments=2,
                expected_course_videos=1,
                expected_embedded_media=1,
            )
            self.assertEqual(before, [path.read_bytes() for path in asr_paths])
            self.assertEqual(result["summary"]["blank_segment_count"], 2)
            self.assertEqual(result["asr_corpus_sha256"],
                             result["inputs"]["asr_corpus_sha256"])
            self.assertEqual(len(result["inputs"]["asr_documents"]), 2)
            self.assertEqual(result["summary"]["judgments"], {
                "no_speech": 1, "speech_detected": 1, "needs_manual": 0,
            })
            self.assertEqual(len(result["mimo_resegmentation_rerun_queue"]), 1)
            self.assertEqual(result["mimo_resegmentation_rerun_queue"][0]["source_sha256"], direct_sha)
            self.assertTrue(all(
                item["judgment"] == "speech_detected"
                for item in result["reviews"]
                if item["mimo_action"] == "resegment_and_rerun"
            ))
            embedded_review = next(item for item in result["reviews"] if item["source_role"] == "embedded_media")
            self.assertEqual(embedded_review["asset_sha256"], parent_sha)
            self.assertEqual(embedded_review["source_sha256"], embedded_sha)
            self.assertEqual(embedded_review["local_review"]["provenance"],
                             "local_sensevoice_audit_only_not_mimo")
            speech_review = next(item for item in result["reviews"] if item["judgment"] == "speech_detected")
            chunks = speech_review["local_review"]["chunks"]
            self.assertEqual(chunks[0]["source_sample_start"], speech_review["sample_start"])
            self.assertEqual(chunks[-1]["source_sample_end"], speech_review["sample_end"])
            self.assertEqual(sum(c["source_sample_end"] - c["source_sample_start"] for c in chunks),
                             speech_review["sample_count"])

    def test_asr_corpus_binding_changes_with_any_asr_content(self):
        first_sha, second_sha = "a" * 64, "b" * 64
        records = [
            (Path("a/asr.json"), {"source_sha256": first_sha, "segments": [{"text": "旧"}]}),
            (Path("b/asr.json"), {"source_sha256": second_sha, "segments": [{"text": "相同"}]}),
        ]
        before, entries = audit.asr_corpus_binding(records)
        records[0][1]["segments"][0]["text"] = "新"
        after, _ = audit.asr_corpus_binding(records)
        self.assertNotEqual(before, after)
        self.assertEqual([item["source_sha256"] for item in entries], [first_sha, second_sha])

    def test_verified_asr_rejects_non_token_plan_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_sha = "c" * 64
            path = root / source_sha[:16] / "asr.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "model": audit.MIMO_MODEL, "source_sha256": source_sha,
                "configuration": {"endpoint": "https://api.xiaomimimo.com/v1/chat/completions"},
                "coverage": {"complete": True}, "segments": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(audit.AuditError, "token-plan-cn"):
                audit.load_verified_asr(root)

    def test_whisper_review_requires_and_propagates_sensevoice_asr_binding(self):
        binding = "d" * 64
        document = {"asr_corpus_sha256": binding,
                    "inputs": {"asr_corpus_sha256": binding}}
        self.assertEqual(whisper_audit.input_asr_corpus_sha256(document), binding)
        with self.assertRaisesRegex(ValueError, "ASR 语料绑定"):
            whisper_audit.input_asr_corpus_sha256({"inputs": {}})
        with self.assertRaisesRegex(ValueError, "ASR 语料绑定"):
            whisper_audit.input_asr_corpus_sha256({
                "asr_corpus_sha256": binding,
                "inputs": {"asr_corpus_sha256": "e" * 64},
            })

    def test_whisper_jointly_reviews_all_40_blanks_without_sensevoice_prefilter(self):
        class FakeWhisper:
            def transcribe(self, interval, **kwargs):
                return iter(()), SimpleNamespace(language="zh", language_probability=1.0)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / "source.wav"
            media.write_bytes(b"fixture")
            binding = "f" * 64
            sense_judgments = (["speech_detected"] * 4 + ["no_speech"]
                               + ["needs_manual"] * 35)
            reviews = [{
                "review_id": f"review-{index}",
                "asset_id": "asset",
                "asset_sha256": "a" * 64,
                "source_sha256": "b" * 64,
                "source_path": str(media),
                "sample_start": index,
                "sample_end": index + 1,
                "local_review": {"text": f"sense-{index}"},
                "judgment": judgment,
            } for index, judgment in enumerate(sense_judgments)]
            source_path = root / "blank_asr_review.json"
            output_path = root / "blank_asr_whisper_review.json"
            source_path.write_text(json.dumps({
                "asr_corpus_sha256": binding,
                "inputs": {"asr_corpus_sha256": binding},
                "reviews": reviews,
            }), encoding="utf-8")

            def decoder(_path):
                samples = np.ones(40, dtype=np.int16)
                return samples, {"sample_rate": audit.SAMPLE_RATE, "channels": 1}

            result = whisper_audit.run(
                source_path, output_path, root / "fake-model",
                threads=1, model=FakeWhisper(), decoder=decoder, progress=lambda _: None,
            )
            self.assertEqual(result["asr_corpus_sha256"], binding)
            self.assertEqual(result["summary"]["input_blank_segment_count"], 40)
            self.assertEqual(result["summary"]["joint_reviewed_blank_segment_count"], 40)
            self.assertEqual(len(result["reviews"]), 40)
            self.assertEqual(result["summary"]["sensevoice_judgments"], {
                "no_speech": 1, "speech_detected": 4, "needs_manual": 35,
            })
            self.assertEqual({item["sensevoice_judgment"] for item in result["reviews"]},
                             {"no_speech", "speech_detected", "needs_manual"})
            self.assertTrue(all(item["whisper_judgment"] == "no_speech"
                                for item in result["reviews"]))
            self.assertEqual(sum(result["summary"]["cross_judgments"].values()), 40)


if __name__ == "__main__":
    unittest.main()
