"""Isolated contract tests; CUDA/FFmpeg hardware checks are run separately."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from video_filter.tests.support import app  # Install isolated extensions before ORM imports.
from video_filter.media import MediaDecoder
from video_filter.mil import ARCHITECTURE, WIDTH, probability, serialize
from video_filter.worker_client import run_mil_prediction
from video_filter.supervisor import resource_mode


def parameters():
    rng = np.random.default_rng(102)
    shapes = {"encoder.0.weight": (128, WIDTH * 2), "encoder.0.bias": (128,),
        "attention_v.weight": (64, 128), "attention_v.bias": (64,),
        "attention_u.weight": (64, 128), "attention_u.bias": (64,),
        "attention_w.weight": (1, 64), "attention_w.bias": (1,),
        "classifier.weight": (1, 128), "classifier.bias": (1,)}
    return {"schema": 2, "model_type": "mil", "architecture": ARCHITECTURE,
        "mean": rng.normal(size=WIDTH).astype(np.float32),
        "scale": rng.uniform(.5, 2, size=WIDTH).astype(np.float32),
        "state": {name: rng.normal(scale=.03, size=shape).astype(np.float32) for name, shape in shapes.items()}}


class NvdecCommandTests(unittest.TestCase):
    def probe_result(self, codec="h264"):
        return subprocess.CompletedProcess([], 0, json.dumps({"streams": [
            {"index": 1, "codec_type": "video", "codec_name": codec, "duration": "2"},
            {"index": 0, "codec_type": "audio"}], "format": {"duration": "2"}}).encode(), b"")

    def test_hardware_decoder_and_device_precede_input_preserving_frame_contract(self):
        decoder = MediaDecoder("fixture-tools", device="cuda:2")
        pixels = np.zeros((1, 224, 224, 3), dtype=np.uint8).tobytes()
        with patch("video_filter.media.run_media_tool", side_effect=[self.probe_result(), subprocess.CompletedProcess([], 0, pixels, b"")]) as run:
            info = decoder.probe("fixture.mp4")
            frames = decoder.frames("fixture.mp4", 0, 2, info["video_stream"], count=16)
        args = run.call_args.args[0]
        self.assertEqual("h264_cuvid", args[args.index("-c:1") + 1])
        self.assertEqual("cuda", args[args.index("-hwaccel:1") + 1])
        self.assertEqual("2", args[args.index("-hwaccel_device:1") + 1])
        self.assertLess(args.index("-hwaccel:1"), args.index("-i"))
        self.assertEqual((16, 224, 224, 3), frames.shape)
        self.assertEqual("nvidia_nvdec", info["video_decode_backend"])

    def test_audio_does_not_initialize_a_video_decoder(self):
        decoder = MediaDecoder("fixture-tools")
        samples = np.zeros(16000, dtype=np.float32)
        with patch("video_filter.media.run_media_tool", return_value=subprocess.CompletedProcess([], 0, samples.tobytes(), b"")) as run:
            actual = decoder.audio("fixture.mp4", 0, 1, 0)
        self.assertFalse(any("hwaccel" in arg or "cuvid" in arg for arg in run.call_args.args[0]))
        self.assertEqual((16000,), actual.shape)

    def test_unsupported_codec_or_hardware_failure_does_not_retry_on_cpu(self):
        decoder = MediaDecoder("fixture-tools")
        with patch("video_filter.media.run_media_tool", return_value=self.probe_result("ffv1")) as run:
            with self.assertRaisesRegex(ValueError, "nvdec_codec_unsupported"):
                decoder.frames("fixture.mkv", 0, 1, 1)
        self.assertEqual(1, run.call_count)
        error = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"hardware failure")
        with patch("video_filter.media.run_media_tool", side_effect=[self.probe_result(), error]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                decoder.frames("fixture.mp4", 0, 1, 1)
        self.assertEqual(2, run.call_count)

    def test_scope_guard_runs_before_probe_and_each_decode(self):
        decoder = MediaDecoder("fixture-tools", scope_guard=lambda _: (_ for _ in ()).throw(ValueError("outside_scope")))
        with patch("video_filter.media.run_media_tool") as run:
            with self.assertRaisesRegex(ValueError, "outside_scope"):
                decoder.frames("fixture.mp4", 0, 1, 1)
            with self.assertRaisesRegex(ValueError, "outside_scope"):
                decoder.audio("fixture.mp4", 0, 1, 0)
        run.assert_not_called()


class MilGpuTransportTests(unittest.TestCase):
    def test_torch_worker_matches_reference_for_long_masked_bag_and_cleans_transport(self):
        rng = np.random.default_rng(8)
        x = rng.normal(size=(519, WIDTH)).astype(np.float32)
        valid = rng.random(x.shape) > .15
        valid[:, 768:] = False  # Explicit missing audio.
        x[~valid] = 0
        values = parameters()
        with tempfile.TemporaryDirectory() as directory:
            result = run_mil_prediction(serialize(values), (x, valid),
                {"device": "cpu", "state_directory": directory, "task_timeout_seconds": 60, "worker_cpu_threads": 1}, "fixture")
            self.assertAlmostEqual(probability(values, (x, valid), chunk_size=17), result, places=5)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_mil_prediction_requires_grouped_resource_and_does_not_fall_back(self):
        with patch("video_filter.worker_client._run_worker") as run:
            with self.assertRaisesRegex(ValueError, "prediction_resource_required"):
                run_mil_prediction(b"fixture", (None, None), {"grouped": True})
        run.assert_not_called()

    def test_invalid_cuda_device_reports_remote_error_and_never_uses_cpu(self):
        values = parameters()
        x = np.zeros((1, WIDTH), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "prediction_worker_failed") as failure:
                run_mil_prediction(serialize(values), (x, np.ones_like(x, dtype=np.bool_)),
                    {"device": "cuda:9999", "state_directory": directory, "task_timeout_seconds": 60}, "unavailable-fixture")
            cause = str(failure.exception.__cause__)
            self.assertTrue("invalid device ordinal" in cause or "mil_cuda_unavailable" in cause, cause)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_prediction_result_must_match_model_device_and_probability(self):
        def fake_run(request, timeout, **options):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for change in ({"device": "cpu"}, {"probability": float("nan")}, {"model_sha256": "incorrect"}):
                    result = {"probability": .5, "device": "cuda:0", "backend": "pytorch", "model_sha256": hashlib.sha256(b"fixture").hexdigest(), **change}
                    (root / "result.json").write_text(json.dumps(result), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "invalid_prediction_worker_result"):
                        options["load"](root)
            return .5
        with patch("video_filter.worker_client._run_worker", side_effect=fake_run):
            run_mil_prediction(b"fixture", (None, None), {"device": "cuda:0", "state_directory": "fixture", "task_timeout_seconds": 1})

    def test_resource_modes_include_shadow_mil_and_preserve_cpu_lr(self):
        for kind in ("predict", "classify"):
            self.assertEqual("extract_shared", resource_mode(kind, {"model_ids": {"logistic_regression": "a", "mil": "b"}, "selected_classifier": "logistic_regression"}))
            self.assertIsNone(resource_mode(kind, {"model_ids": {"logistic_regression": "a"}}))
        self.assertEqual("exclusive_train", resource_mode("train", {"model_type": "mil"}))


if __name__ == "__main__":
    unittest.main()
