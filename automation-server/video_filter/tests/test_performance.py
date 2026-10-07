"""Offline throughput contracts; no GPU, production DB or real media required."""
import tempfile
import json
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np

from .support import DatabaseTestCase, db, signature
from media_lineage.models import ResourceLease
from media_lineage import resources
from video_filter.extraction import extract
from video_filter.features.contract import DIMENSIONS


class RuntimeBudgetConfigTests(DatabaseTestCase):
    def test_grouped_budget_defaults_overrides_and_invalid_values(self):
        from env import EnvConfig
        from video_filter.group_config import ROLES
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "groups.json"
            path.write_text(json.dumps({"schema_version": 1, "groups": [{"name": "fixture",
                "directories": {role: str(root / "media" / role) for role in ROLES}}]}), encoding="utf-8")
            variables = {"VIDEO_FILTER_ENABLED": "true", "VIDEO_FILTER_GROUPS_CONFIG": str(path),
                "VIDEO_FILTER_STATE_DIR": str(root / "state")}
            with patch.dict(os.environ, variables, clear=True):
                settings = EnvConfig.video_filter_settings()
                self.assertEqual((1024, 256, 1024, 32), tuple(settings[key] for key in
                    ("gpu_extract_peak_mib", "gpu_predict_peak_mib", "gpu_safety_mib", "audio_cache_mib")))
                self.assertEqual((16, 4, 8), tuple(settings[key] for key in
                    ("dino_batch_size", "videomae_batch_size", "beats_batch_size")))
                self.assertTrue(settings["persistent_workers"])
                self.assertEqual((768, 120, 100), tuple(settings[key] for key in
                    ("worker_model_cache_mib", "worker_idle_seconds", "worker_max_tasks")))
                with patch.dict(os.environ, {"VIDEO_FILTER_PERSISTENT_WORKERS": "false"}):
                    self.assertFalse(EnvConfig.video_filter_settings()["persistent_workers"])
                with patch.dict(os.environ, {"VIDEO_FILTER_PERSISTENT_WORKERS": "invalid"}):
                    with self.assertRaises(RuntimeError):
                        EnvConfig.video_filter_settings()
                for key in ("DINO_BATCH_SIZE", "VIDEOMAE_BATCH_SIZE", "BEATS_BATCH_SIZE"):
                    with patch.dict(os.environ, {"VIDEO_FILTER_" + key: "2"}):
                        self.assertEqual(2, EnvConfig.video_filter_settings()["groups"][0][key.lower()])
                    for invalid in ("0", "65", "not-an-integer"):
                        with patch.dict(os.environ, {"VIDEO_FILTER_" + key: invalid}):
                            with self.assertRaises(RuntimeError):
                                EnvConfig.video_filter_settings()
                os.environ["VIDEO_FILTER_GPU_EXTRACT_PEAK_MIB"] = "1536"
                self.assertEqual(1536, EnvConfig.video_filter_settings()["groups"][0]["gpu_extract_peak_mib"])
                for key in ("GPU_EXTRACT_PEAK_MIB", "GPU_PREDICT_PEAK_MIB", "GPU_SAFETY_MIB", "AUDIO_CACHE_MIB",
                            "WORKER_MODEL_CACHE_MIB", "WORKER_IDLE_SECONDS", "WORKER_MAX_TASKS"):
                    with patch.dict(os.environ, {"VIDEO_FILTER_" + key: "0"}):
                        with self.assertRaises(RuntimeError):
                            EnvConfig.video_filter_settings()


class MemoryAdmissionTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("free_memory", 5120), ("process_memory", {}), ("process_identity", "fixture-start")):
            mock = patch.object(resources, name, return_value=value)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def request(self, owner, **kwargs):
        return resources.request_lease(db.session, "GPU-fixture", "extract_shared", owner, **kwargs)

    def test_cold_tasks_reserve_future_memory_and_release_is_idempotent(self):
        leases = [self.request(str(i)) for i in range(4)]
        self.assertTrue(all(leases))
        self.assertIsNone(self.request("fifth"))
        decision = resources.admission_decisions()["GPU-fixture"]
        self.assertEqual((4096, 1024, "memory_insufficient"),
            (decision["pending_reserved_mib"], decision["usable_free_mib"], decision["reason"]))
        resources.release(db.session, leases[0])
        resources.release(db.session, leases[0])
        self.assertIsNotNone(self.request("fifth"))

    def test_observed_pid_memory_is_not_counted_twice_and_predict_has_separate_quota(self):
        self.free_memory.return_value = 8192
        for i in range(6):
            lease = self.request(str(i))
            resources.heartbeat(db.session, lease, processes=[{"pid": 9000 + i, "identity": "fixture-start"}])
        self.free_memory.return_value = 5120
        self.process_memory.return_value = {9000 + i: 1024 for i in range(6)}
        self.assertIsNotNone(self.request("predict", workload_type="predict", memory_budget_mib=256))
        self.assertEqual(0, resources.admission_decisions()["GPU-fixture"]["pending_reserved_mib"])
        self.assertIsNone(self.request("extract-seven"))
        self.assertIsNotNone(self.request("predict-two", workload_type="predict", memory_budget_mib=256))
        self.assertIsNone(self.request("predict-three", workload_type="predict", memory_budget_mib=256))

    def test_unknown_wddm_pid_retains_budget_and_query_failure_defers(self):
        lease = self.request("one")
        resources.heartbeat(db.session, lease, processes=[{"pid": 9000, "identity": "old-start"}])
        self.process_memory.return_value = {9000: 4096}
        self.assertIsNotNone(self.request("two"))
        self.assertEqual(1024, resources.admission_decisions()["GPU-fixture"]["pending_reserved_mib"])
        self.process_memory.side_effect = OSError("unknown WDDM attribution")
        self.assertIsNotNone(self.request("three"))
        self.assertEqual(2048, resources.admission_decisions()["GPU-fixture"]["pending_reserved_mib"])
        self.free_memory.side_effect = OSError("driver unavailable")
        self.assertIsNone(self.request("four"))
        self.assertEqual("memory_query_unavailable", resources.admission_decisions()["GPU-fixture"]["reason"])

    def test_oom_and_driver_peak_raise_compatible_profile_budget(self):
        lease = self.request("one", profile_key="fixture-profile")
        resources.record_oom(db.session, lease)
        resources.release(db.session, lease)
        following = self.request("two", profile_key="fixture-profile")
        self.assertEqual(1536, db.session.get(ResourceLease, following).memory_budget_mib)
        resources.heartbeat(db.session, following, processes=[{"pid": 9000, "identity": "fixture-start"}])
        self.process_memory.return_value = {9000: 2048}
        self.request("probe", workload_type="predict", memory_budget_mib=256)
        self.assertGreater(db.session.get(ResourceLease, following, populate_existing=True).memory_budget_mib, 2048)
        resources.release(db.session, following)
        new = self.request("three", profile_key="fixture-profile")
        self.assertGreater(db.session.get(ResourceLease, new).memory_budget_mib, 2048)

    def test_legacy_budget_and_exclusive_waiting_remain_conservative(self):
        lease = self.request("legacy")
        row = db.session.get(ResourceLease, lease)
        row.memory_budget_mib, row.memory_observation = None, {}
        db.session.commit()
        self.assertIsNotNone(self.request("new"))
        self.assertEqual(1024, resources.admission_decisions()["GPU-fixture"]["pending_reserved_mib"])
        self.assertIsNone(resources.request_lease(db.session, "GPU-fixture", "exclusive_train", "train"))
        self.assertIsNone(self.request("later"))
        self.assertEqual("exclusive_waiting", resources.admission_decisions()["GPU-fixture"]["reason"])

    def test_admission_round_reuses_queries_but_reserves_every_new_task(self):
        with resources.admission_round():
            leases = [self.request(str(i)) for i in range(4)]
            self.assertTrue(all(leases))
            self.assertIsNone(self.request("fifth"))
            self.assertEqual(1, self.free_memory.call_count)
            self.assertEqual(1, self.process_memory.call_count)
            self.assertEqual(4096, resources.admission_decisions()["GPU-fixture"]["pending_reserved_mib"])
        with resources.admission_round():
            self.request("fifth")
        self.assertEqual(2, self.free_memory.call_count)

    def test_round_query_failure_is_not_cached_as_success(self):
        self.free_memory.side_effect = [OSError("driver unavailable"), 5120]
        with resources.admission_round():
            self.assertIsNone(self.request("first"))
            self.assertIsNotNone(self.request("first"))

    def test_slow_control_round_refreshes_driver_sample(self):
        with patch.object(resources.time, "monotonic", return_value=10) as clock, resources.admission_round():
            self.assertEqual(5120, resources.memory_snapshot("GPU-fixture")[0])
            clock.return_value = 10.5
            resources.memory_snapshot("GPU-fixture")
            self.assertEqual(1, self.free_memory.call_count)
            clock.return_value = 11.1
            resources.memory_snapshot("GPU-fixture")
            self.assertEqual(2, self.free_memory.call_count)


class FakeDecoder:
    def __init__(self, no_audio=False, duration=12.):
        self.no_audio, self.audio_calls, self.duration = no_audio, 0, duration

    def probe(self, path):
        return {"duration_seconds": self.duration, "audio_status": "no_audio" if self.no_audio else "present",
            "video_stream": 0, "audio_stream": 1}

    def frames(self, path, start, duration, stream, fps, count):
        return np.zeros((count, 2, 2, 3), dtype=np.uint8)

    def audio(self, path, start, duration, stream):
        self.audio_calls += 1
        return np.full(int(duration * 16000), start + 1, dtype=np.float32)


def factory(name):
    class Adapter:
        def __init__(self, artifact, config, device):
            pass

        def extract(self, data):
            values = np.full(DIMENSIONS[name], np.mean(data), dtype=np.float32)
            return (values, np.ones(DIMENSIONS[name], dtype=np.bool_)) if name == "egemaps" else values
    return Adapter


class AudioCacheTests(DatabaseTestCase):
    def test_continuous_windows_are_shared_even_when_video_exceeds_cache(self):
        class StreamingDecoder(FakeDecoder):
            def audio_windows(self, path, windows, stream, task_id=None):
                for index, (start, end) in enumerate(windows):
                    yield index, self.audio(path, start, end - start, stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.mp4"
            path.write_bytes(b"synthetic bytes, not video")
            decoder = StreamingDecoder(duration=30.)
            streamed = extract(path, decoder, signature(), {name: None for name in DIMENSIONS}, device="cpu",
                adapter_factories={name: factory(name) for name in DIMENSIONS}, audio_cache_mib=1)
        original, original_count = self.run_extract(0, duration=30.)
        self.assertEqual((3, 6), (decoder.audio_calls, original_count))
        self.assertTrue(streamed["measurements"]["audio_cache"]["continuous"])
        for name in DIMENSIONS:
            np.testing.assert_array_equal(streamed["vectors"][name], original["vectors"][name])
            np.testing.assert_array_equal(streamed["validity"][name], original["validity"][name])

    def test_continuous_decoder_terminal_failure_prevents_summary(self):
        class BrokenDecoder(FakeDecoder):
            def audio_windows(self, path, windows, stream, task_id=None):
                for index, (start, end) in enumerate(windows):
                    yield index, self.audio(path, start, end - start, stream)
                raise OSError("decoder terminal failure")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.mp4"
            path.write_bytes(b"synthetic bytes, not video")
            with self.assertRaisesRegex(OSError, "decoder terminal failure"):
                extract(path, BrokenDecoder(), signature(), {name: None for name in DIMENSIONS}, device="cpu",
                    adapter_factories={name: factory(name) for name in DIMENSIONS})

    def run_extract(self, budget, no_audio=False, duration=12.):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.mp4"
            path.write_bytes(b"synthetic bytes, not video")
            decoder = FakeDecoder(no_audio, duration)
            result = extract(path, decoder, signature(), {name: None for name in DIMENSIONS}, device="cpu",
                adapter_factories={name: factory(name) for name in DIMENSIONS}, audio_cache_mib=budget)
            return result, decoder.audio_calls

    def test_same_features_with_one_decode_per_window_and_short_tail(self):
        cached, count = self.run_extract(1)
        original, original_count = self.run_extract(0)
        self.assertEqual((2, 4), (count, original_count))
        self.assertEqual(2, cached["measurements"]["audio_cache"]["hits"])
        for name in DIMENSIONS:
            np.testing.assert_array_equal(cached["vectors"][name], original["vectors"][name])
            np.testing.assert_array_equal(cached["validity"][name], original["validity"][name])

    def test_no_audio_never_decodes_and_has_explicit_masks(self):
        result, count = self.run_extract(1, no_audio=True)
        self.assertEqual(0, count)
        self.assertFalse(result["validity"]["beats"].any())
        self.assertFalse(result["validity"]["egemaps"].any())

    def test_longer_audio_evicts_old_windows_with_identical_features(self):
        cached, count = self.run_extract(1, duration=30.)
        uncached, original_count = self.run_extract(0, duration=30.)
        self.assertEqual((5, 6), (count, original_count))
        self.assertEqual(1, cached["measurements"]["audio_cache"]["hits"])
        for name in DIMENSIONS:
            np.testing.assert_array_equal(cached["vectors"][name], uncached["vectors"][name])
