"""Offline batching and lease boundaries; use only generated inputs and test DB."""

import tempfile
import unittest
import os
import subprocess
import sys
from functools import wraps
from types import SimpleNamespace
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np

from .support import signature
from .test_performance import FakeDecoder, factory
from .test_gpu_inference import parameters
from video_filter.extraction import extract
from video_filter.features.batching import infer_batches
from video_filter.features.contract import DIMENSIONS
from video_filter.mil import WIDTH, fit, probability, serialize
from video_filter.worker_client import run_mil_prediction


class FixtureOOM(RuntimeError):
    pass


fake_torch = SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=FixtureOOM, empty_cache=lambda: None))


def isolated_torch(method):
    """Mirror production isolation/MKL settings instead of mixing Flask and torch."""
    @wraps(method)
    def check(self):
        if os.getenv("VIDEO_FILTER_BATCH_TEST_CHILD") == "1":
            return method(self)
        environment = {**os.environ, "MKL_THREADING_LAYER": "SEQUENTIAL", "VIDEO_FILTER_BATCH_TEST_CHILD": "1"}
        result = subprocess.run([sys.executable, "-m", "unittest", self.id()],
            cwd=Path(__file__).parents[2], env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
    return check


class PhaseSpy:
    def __init__(self):
        self.active = False
        self.events = []

    @contextmanager
    def __call__(self, phase, batch_size=1):
        if self.active:
            raise AssertionError("Nested GPU phase")
        self.active = True
        self.events.append(("acquire", phase, batch_size))
        try:
            yield
        finally:
            self.active = False
            self.events.append(("release", phase, batch_size))


class BatchExtractionTests(unittest.TestCase):
    def execute(self, *, batched, no_audio=False, broken=False, empty=False):
        phases, calls = PhaseSpy(), []
        class Decoder(FakeDecoder):
            def frames(self, *args, **kwargs):
                if batched:
                    self_outer.assertTrue(phases.active)
                result = super().frames(*args, **kwargs)
                result[:] = int(args[1]) + 1
                return result

            def audio_windows(self, path, windows, stream, task_id=None):
                for index, (start, end) in enumerate(windows):
                    self_outer.assertFalse(phases.active)
                    yield index, np.zeros(0, dtype=np.float32) if empty and index == 1 else self.audio(path, start, end - start, stream)
                if broken:
                    raise OSError("terminal_audio_error")

        class CpuModel:
            def to(self, device):
                return self

        def batched_factory(name):
            class Adapter:
                def __init__(self, artifact, config, device):
                    self_outer.assertFalse(phases.active)
                    self.torch, self.model = fake_torch, CpuModel()

                def extract_batch(self, items):
                    self_outer.assertTrue(phases.active)
                    calls.append((name, len(items)))
                    return np.stack([np.full(DIMENSIONS[name], np.mean(item), dtype=np.float32) for item in items])
            return Adapter

        class Acoustic(factory("egemaps")):
            def extract(self, data):
                self_outer.assertFalse(phases.active)
                return super().extract(data)

        self_outer = self
        factories = {name: batched_factory(name) if batched and name != "egemaps" else factory(name) for name in DIMENSIONS}
        if batched:
            factories["egemaps"] = Acoustic
        decoder = Decoder(no_audio=no_audio, duration=42.)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.mp4"
            path.write_bytes(b"generated test fixture")
            result = extract(path, decoder, signature(), {name: None for name in DIMENSIONS}, device="cpu",
                adapter_factories=factories, batch_sizes={"dino": 16, "videomae": 4, "beats": 8},
                gpu_phase=phases if batched else None)
        self.assertFalse(phases.active)
        return result, phases, calls, decoder

    def test_batch_equivalence_order_tail_and_cpu_audio_outside_lease(self):
        result, phases, calls, decoder = self.execute(batched=True)
        original, _, _, _ = self.execute(batched=False)
        for name in DIMENSIONS:
            np.testing.assert_allclose(result["vectors"][name], original["vectors"][name], rtol=1e-6)
            np.testing.assert_array_equal(result["validity"][name], original["validity"][name])
        self.assertEqual(5, decoder.audio_calls)
        self.assertIn(("dino", 15), calls)
        self.assertIn(("videomae", 4), calls)
        self.assertIn(("videomae", 1), calls)
        self.assertIn(("beats", 8), calls)
        self.assertEqual(len(phases.events) // 2, sum(event[0] == "release" for event in phases.events))

    def test_no_audio_never_admits_audio_gpu_phase(self):
        result, phases, calls, decoder = self.execute(batched=True, no_audio=True)
        self.assertEqual(0, decoder.audio_calls)
        self.assertFalse(result["validity"]["beats"].any())
        self.assertFalse(result["validity"]["egemaps"].any())
        self.assertNotIn("beats", [item[1] for item in phases.events])

    def test_empty_audio_keeps_missing_masks_and_terminal_error_rejects_summary(self):
        result, _, _, _ = self.execute(batched=True, empty=True)
        self.assertEqual([1], result["audio_missing_windows"])
        self.assertFalse(result["validity"]["beats"][1].any())
        self.assertFalse(result["validity"]["egemaps"][1].any())
        with self.assertRaisesRegex(OSError, "terminal_audio_error"):
            self.execute(batched=True, broken=True)


class AdapterBatchTests(unittest.TestCase):
    @isolated_torch
    def test_dino_batch_preserves_cls_vectors_and_mean(self):
        import torch
        from video_filter.features.dino import DinoAdapter
        class Model:
            def forward_features(self, inputs):
                return inputs.mean((1, 2, 3))[:, None, None].expand(-1, 1, 384)
        adapter = DinoAdapter.__new__(DinoAdapter)
        adapter.torch, adapter.device, adapter.model = torch, "cpu", Model()
        adapter.mean = torch.tensor([.485, .456, .406]).view(1, 3, 1, 1)
        adapter.std = torch.tensor([.229, .224, .225]).view(1, 3, 1, 1)
        frames = np.random.default_rng(8).integers(0, 255, (5, 8, 8, 3), dtype=np.uint8)
        batch = adapter.extract_batch(list(frames))
        np.testing.assert_allclose(batch, np.stack([adapter.extract(frame[None]) for frame in frames]), atol=1e-6)
        np.testing.assert_allclose(adapter.extract(frames), batch.mean(0), atol=1e-6)

    def test_oom_reduces_batch_without_losing_or_reordering_inputs(self):
        class Adapter:
            def __init__(self):
                self.torch, self.calls = fake_torch, []

            def extract_batch(self, items):
                self.calls.append(len(items))
                if len(items) > 2:
                    raise FixtureOOM("test out of memory")
                return np.asarray(items, dtype=np.float32).reshape(-1, 1)
        adapter = Adapter()
        np.testing.assert_array_equal(np.arange(7), infer_batches(adapter, list(range(7)), 7, modality="dino")[:, 0])
        self.assertEqual([7, 3, 1, 1, 1, 1, 1, 1, 1], adapter.calls)
        with patch.object(adapter, "extract_batch", side_effect=FixtureOOM("test out of memory")):
            with self.assertRaises(FixtureOOM):
                infer_batches(adapter, [1], 1, modality="dino")

    @isolated_torch
    def test_beats_groups_equal_lengths_preserving_short_padding(self):
        import torch
        from video_filter.features.beats import BeatsAdapter
        class Model:
            def __init__(self):
                self.shapes = []

            def extract_features(self, inputs):
                self.shapes.append(tuple(inputs.shape))
                return inputs.mean(1)[:, None, None].expand(-1, 2, 768), None
        adapter = BeatsAdapter.__new__(BeatsAdapter)
        adapter.torch, adapter.device, adapter.model = torch, "cpu", Model()
        chunks = [np.full(80000, 2., dtype=np.float32), np.ones(2000, dtype=np.float32), np.full(80000, 4., dtype=np.float32)]
        batch = adapter.extract_batch(chunks)
        np.testing.assert_allclose(batch[:, 0], [2., 2000 / 6400, 4.])
        self.assertEqual([(1, 6400), (2, 80000)], adapter.model.shapes)
        np.testing.assert_allclose(batch, np.stack([adapter.extract(chunk) for chunk in chunks]))

    @isolated_torch
    def test_videomae_nested_processor_batch_matches_single_clips(self):
        import torch
        from transformers import VideoMAEConfig, VideoMAEForVideoClassification, VideoMAEImageProcessor
        from video_filter.features.videomae import VideoMAEAdapter
        torch.set_num_threads(1)
        adapter = VideoMAEAdapter.__new__(VideoMAEAdapter)
        adapter.torch, adapter.device = torch, "cpu"
        adapter.processor = VideoMAEImageProcessor()
        adapter.model = VideoMAEForVideoClassification(VideoMAEConfig(image_size=8, patch_size=4,
            num_frames=4, tubelet_size=2, hidden_size=16, num_hidden_layers=1, num_attention_heads=2,
            intermediate_size=32)).eval()
        rng = np.random.default_rng(8)
        clips = [rng.integers(0, 255, (4, 8, 8, 3), dtype=np.uint8) for _ in range(3)]
        np.testing.assert_allclose(adapter.extract_batch(clips), np.stack([adapter.extract(clip) for clip in clips]), atol=1e-5)


class PhaseTransportTests(unittest.TestCase):
    def test_failed_phase_retains_ownership_and_reports_oom(self):
        from video_filter.gpu_phase import WorkerGpuPhases
        phases = WorkerGpuPhases({"task_id": "fixture"})
        with patch.object(phases, "exchange", return_value=True) as exchange:
            with self.assertRaisesRegex(RuntimeError, "out of memory"):
                with phases("beats", 8):
                    raise RuntimeError("fixture out of memory")
        self.assertEqual([("acquire", "beats", 8), ("failed", "beats", 8, True)],
                         [call.args for call in exchange.call_args_list])

    def test_waiting_phase_timeout_cleans_up_and_never_runs_inference(self):
        events = []
        def control(record):
            events.append(record["action"])
            return False
        x = np.zeros((1, WIDTH), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "prediction_timeout"):
                run_mil_prediction(serialize(parameters()), (x, np.ones_like(x, dtype=np.bool_)),
                    {"device": "cpu", "state_directory": directory, "task_timeout_seconds": 5,
                     "gpu_control": control}, "phase-timeout")
            self.assertEqual([], list(Path(directory).iterdir()))
        self.assertIn("acquire", events)
        self.assertEqual("cleanup", events[-1])
        self.assertNotIn("release", events)

    def test_real_worker_wait_acquire_release_and_cleanup(self):
        rng = np.random.default_rng(21)
        x = rng.normal(size=(3, WIDTH)).astype(np.float32)
        bag = (x, np.ones_like(x, dtype=np.bool_))
        values, events = parameters(), []
        def control(record):
            events.append(dict(record))
            return sum(item["action"] == "acquire" for item in events) >= 3
        with tempfile.TemporaryDirectory() as directory:
            result = run_mil_prediction(serialize(values), bag,
                {"device": "cpu", "state_directory": directory, "task_timeout_seconds": 30,
                 "gpu_control": control}, "phase-fixture")
            self.assertEqual([], list(Path(directory).iterdir()))
        self.assertAlmostEqual(probability(values, bag), result, places=5)
        self.assertEqual(["acquire", "acquire", "acquire", "release", "cleanup"], [event["action"] for event in events])

    def test_control_error_fails_worker_and_still_cleans_up(self):
        events = []
        def control(record):
            events.append(record["action"])
            if record["action"] == "acquire":
                raise ValueError("fixture_control_error")
            return True
        x = np.zeros((1, WIDTH), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "worker_log_transport_failed"):
                run_mil_prediction(serialize(parameters()), (x, np.ones_like(x, dtype=np.bool_)),
                    {"device": "cpu", "state_directory": directory, "task_timeout_seconds": 30,
                     "gpu_control": control}, "phase-fixture")
        self.assertEqual(["acquire", "cleanup"], events)

    @isolated_torch
    def test_training_standardizer_before_exclusive_phase_and_serialization_after(self):
        from video_filter import mil
        phases = PhaseSpy()
        rng = np.random.default_rng(42)
        bags = [(rng.normal(size=(2, WIDTH)).astype(np.float32), np.ones((2, WIDTH), dtype=np.bool_)) for _ in range(6)]
        original_standardizer, original_logit = mil.standardizer, mil.bag_logit
        def standardizer(bags):
            self.assertFalse(phases.active)
            return original_standardizer(bags)
        def logit(*args, **kwargs):
            self.assertTrue(phases.active)
            return original_logit(*args, **kwargs)
        with patch.object(mil, "standardizer", side_effect=standardizer), patch.object(mil, "bag_logit", side_effect=logit):
            parameters_, predictions, metrics = fit(bags, np.array([0, 1, 0, 1, 0, 1]), np.arange(4), np.arange(4, 6),
                epochs=1, device="cpu", gpu_phase=phases)
        self.assertFalse(phases.active)
        self.assertEqual([("acquire", "mil_train", 1), ("release", "mil_train", 1)], phases.events)
        self.assertEqual(2, len(predictions))
        self.assertEqual(1, metrics["epochs_completed"])
        self.assertTrue(serialize(parameters_))
