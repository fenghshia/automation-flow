"""Persistent workers using generated numeric data and isolated local processes."""

import io
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from .support import app
from .test_gpu_inference import parameters
from .test_batch_phases import isolated_torch, fake_torch
from video_filter import persistent_client as pool
from video_filter.model_cache import ModelCache, adapter_key
from video_filter.mil import WIDTH, probability, serialize
from video_filter.worker_client import run_mil_prediction, worker_processes, cancel_worker
from video_filter.observability import configure_worker_logs, WorkerLogHandler


class CacheTests(unittest.TestCase):
    @isolated_torch
    def test_extraction_reuses_cpu_adapters_with_independent_summary_outputs(self):
        from uuid import uuid4
        from .support import signature
        from .test_performance import FakeDecoder
        from video_filter.features.contract import DIMENSIONS
        from video_filter.worker import execute
        import json

        class StreamingDecoder(FakeDecoder):
            def audio_windows(self, *args, **kwargs):
                return iter(())
        calls = []
        def adapter(name):
            class Adapter:
                def __init__(self, artifact, specification, device):
                    calls.append(name)
                    self.torch = fake_torch
                    self.model = SimpleNamespace(to=lambda device: None, parameters=lambda: [], buffers=lambda: [])

                def extract_batch(self, items):
                    return np.ones((len(items), DIMENSIONS[name]), dtype=np.float32)
            return Adapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "generated.mp4"
            path.write_bytes(b"Generated test data")
            for name in ("dino", "videomae", "beats", "egemaps", "config", "preprocessor_config"):
                (root / (name + ".json")).write_bytes(b"Generated model fixture")
            spec, cache = signature(), ModelCache(16)
            request = {"task_id": str(uuid4()), "variant_id": str(uuid4()), "asset_id": str(uuid4()),
                "device": "cpu", "path": str(path), "state_directory": str(root),
                "ffmpeg_directory": str(root), "model_manifest": str(root / "manifest.json"),
                "feature_signature": spec.digest, "resource_granted": True,
                "batch_sizes": {"dino": 4, "videomae": 2, "beats": 2}}
            with patch("video_filter.features.dino.DinoAdapter", adapter("dino")), \
                    patch("video_filter.features.videomae.VideoMAEAdapter", adapter("videomae")), \
                    patch("video_filter.features.manifest.load_model_manifest", return_value=(spec, {name: root / (name + ".json") for name in DIMENSIONS})), \
                    patch("video_filter.media.MediaDecoder", side_effect=lambda *args, **kwargs: StreamingDecoder(no_audio=True)), \
                    patch("video_filter.observability.configure_worker_logs"):
                execute(request, root / "first", model_cache=cache)
                next_request = {**request, "task_id": str(uuid4())}
                execute(next_request, root / "second", model_cache=cache)
            self.assertEqual(["dino", "videomae"], calls)
            first = json.loads((root / "first" / "metadata.json").read_text())
            second = json.loads((root / "second" / "metadata.json").read_text())
            self.assertNotEqual(first["manifest"]["task_id"], second["manifest"]["task_id"])
            self.assertEqual((root / "first" / "arrays.npz").read_bytes(), (root / "second" / "arrays.npz").read_bytes())

    def test_lru_budget_oversize_and_version_group_epoch_isolation(self):
        cache = ModelCache(1)
        loads = []
        def get(key, count=524288):
            def load():
                loads.append(key)
                return np.zeros(count, dtype=np.uint8)
            return cache.get(key, load, modality="fixture", task_id="fixture")
        a = get(("g1", "epoch1", "v1"))
        get(("g1", "epoch1", "v2"))
        self.assertIs(a, get(("g1", "epoch1", "v1")))
        get(("g2", "epoch1", "v1"))
        self.assertNotIn(("g1", "epoch1", "v2"), cache.entries)
        get(("g1", "epoch2", "v1"))
        entries = list(cache.entries)
        get("oversize", 2 * 1024**2)
        self.assertEqual(entries, list(cache.entries))
        self.assertLessEqual(cache.bytes, cache.limit)
        self.assertEqual(5, len(loads))

    def test_checkpoint_and_preprocessor_changes_invalidate_adapter_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("weights.bin", "config.json", "preprocessor_config.json"):
                (root / name).write_bytes(b"fixture")
            key = adapter_key("videomae", root / "weights.bin", {"input": {}})
            (root / "preprocessor_config.json").write_bytes(b"different generated fixture")
            self.assertNotEqual(key, adapter_key("videomae", root / "weights.bin", {"input": {}}))
            with self.assertRaisesRegex(ValueError, "source filename"):
                adapter_key("beats", root / "weights.bin", {"input": {"source_hashes": {"../bad.py": "a"}}})

    def test_each_task_replaces_worker_log_handler(self):
        first, second = io.StringIO(), io.StringIO()
        logger = logging.getLogger("video_filter")
        original = list(logger.handlers)
        try:
            configure_worker_logs({"task_id": "first"}, stream=first)
            logger.info("fixture-first")
            configure_worker_logs({"task_id": "second"}, stream=second)
            logger.info("fixture-second")
            self.assertNotIn("fixture-second", first.getvalue())
            self.assertEqual(1, second.getvalue().count("fixture-second"))
            self.assertEqual(1, sum(isinstance(handler, WorkerLogHandler) for handler in logger.handlers))
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                if handler not in original:
                    handler.close()
            for handler in original:
                logger.addHandler(handler)


class PersistentWorkerTests(unittest.TestCase):
    def setUp(self):
        pool.shutdown()
        pool._closed = False
        def close_pool():
            pool.shutdown()
            pool._closed = False
        self.addCleanup(close_pool)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.metrics, self.controls = [], []
        metric_patch = patch.object(pool, "log_performance", side_effect=lambda event, **fields: self.metrics.append((event, fields)))
        metric_patch.start()
        self.addCleanup(metric_patch.stop)
        self.values = parameters()
        self.x = np.random.default_rng(8).normal(size=(3, WIDTH)).astype(np.float32)
        self.bag = (self.x, np.ones_like(self.x, dtype=np.bool_))
        self.settings = {"device": "cpu", "state_directory": self.temp.name, "task_timeout_seconds": 30,
            "persistent_workers": True, "worker_model_cache_mib": 16, "worker_max_tasks": 100,
            "worker_idle_seconds": 120, "gpu_control": self.control}

    def control(self, record):
        self.controls.append(record["action"])
        return True

    def predict(self, identifier, **overrides):
        return run_mil_prediction(serialize(self.values), self.bag, {**self.settings, **overrides}, identifier)

    def test_reuses_process_and_cached_model_with_correct_results_and_clean_transport(self):
        expected = probability(self.values, self.bag)
        self.assertAlmostEqual(expected, self.predict("fixture-one"), places=5)
        worker = pool._workers[0]
        self.assertFalse(worker.busy)
        self.assertEqual([], worker_processes("fixture-one"))
        self.assertAlmostEqual(expected, self.predict("fixture-two"), places=5)
        self.assertIs(worker, pool._workers[0])
        starts = [fields for event, fields in self.metrics if event == "worker_started"]
        self.assertEqual([False, True], [fields["reused"] for fields in starts])
        self.assertEqual(starts[0]["worker_pid"], starts[1]["worker_pid"])
        caches = [fields for event, fields in self.metrics if event == "model_cache"]
        self.assertEqual([False, True], [fields["hit"] for fields in caches])
        self.assertEqual([], list(Path(self.temp.name).iterdir()))
        self.assertEqual(["acquire", "release", "cleanup"] * 2, self.controls)

    def test_model_version_change_misses_cache_and_periodic_retirement_rebuilds(self):
        self.predict("fixture-one", worker_max_tasks=2)
        self.values["state"]["classifier.bias"][0] += 1
        self.assertAlmostEqual(probability(self.values, self.bag), self.predict("fixture-two", worker_max_tasks=2), places=5)
        self.assertEqual([], pool._workers)
        self.predict("fixture-three", worker_max_tasks=2)
        self.assertEqual([False, False, False], [fields["hit"] for event, fields in self.metrics if event == "model_cache"])

    def test_idle_workers_are_reaped_and_settings_changes_replace_idle_process(self):
        self.predict("fixture-one")
        old = pool._workers[0]
        self.predict("fixture-two", worker_cpu_threads=2)
        # Two configured prediction slots allow compatible workers to coexist.
        self.assertEqual(2, len(pool._workers))
        for worker in pool._workers:
            worker.last_used = time.monotonic() - 121
        pool.reap_idle()
        self.assertEqual([], pool._workers)
        self.assertIsNotNone(old.process.poll())

    def test_wait_timeout_kills_before_lease_cleanup_then_next_job_rebuilds(self):
        def blocked(record):
            self.controls.append(record["action"])
            if record["action"] == "cleanup":
                self.assertTrue(pool._workers[0].stopped.is_set())
                self.assertIsNotNone(pool._workers[0].process.poll())
            return False
        with self.assertRaisesRegex(ValueError, "prediction_timeout"):
            self.predict("fixture-timeout", task_timeout_seconds=5, gpu_control=blocked)
        self.assertEqual([], pool._workers)
        self.assertAlmostEqual(probability(self.values, self.bag), self.predict("fixture-after"), places=5)

    def test_cancellation_kills_only_owned_task_and_rebuilds(self):
        entered = threading.Event()
        errors = []
        def waiting(record):
            if record["action"] == "acquire":
                entered.set()
                return False
            return True
        def run():
            try:
                self.predict("fixture-cancel", gpu_control=waiting)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            cancel_worker("fixture-cancel")
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual([], pool._workers)
        self.assertAlmostEqual(probability(self.values, self.bag), self.predict("fixture-after"), places=5)

    def test_invalid_output_retires_worker_before_reuse(self):
        from video_filter import worker_client
        original = worker_client._run_worker
        def invalid(request, timeout, **options):
            def reject(output):
                raise ValueError("fixture_invalid_output")
            return original(request, timeout, **{**options, "load": reject})
        with patch.object(worker_client, "_run_worker", side_effect=invalid):
            with self.assertRaisesRegex(ValueError, "fixture_invalid_output"):
                self.predict("fixture-invalid")
        self.assertEqual([], pool._workers)
        self.assertAlmostEqual(probability(self.values, self.bag), self.predict("fixture-after"), places=5)

    def test_capacity_wait_reuses_released_worker_without_starting_extra_process(self):
        fake = SimpleNamespace(key=("fixture", "cpu", 1, 16), busy=True,
            process=SimpleNamespace(poll=lambda: None), last_used=time.monotonic(), idle_seconds=120)
        pool._workers.append(fake)
        claimed, errors = [], []
        def acquire():
            try:
                claimed.append(pool._acquire(fake.key, 1, {}, Path(self.temp.name) / "gate", time.monotonic() + 3))
            except Exception as error:
                errors.append(error)
        with patch.object(pool, "PersistentWorker") as spawn:
            thread = threading.Thread(target=acquire)
            thread.start()
            try:
                with pool._condition:
                    fake.busy = False
                    pool._condition.notify_all()
                thread.join(timeout=5)
                self.assertEqual([], errors)
                self.assertEqual([fake], claimed)
                spawn.assert_not_called()
            finally:
                pool._workers.remove(fake)
