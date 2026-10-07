"""Isolated GPU arbitration, encoder lifetime and CPU-only publishing checks."""
import io
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from video_filter.tests.support import DatabaseTestCase, db
from env import EnvConfig
from media_lineage import resources
from media_lineage.integration import compression_gpu
from media_lineage.models import ResourceLease


class CompressionBudgetTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        # Register routes before any dashboard request locks Flask's setup.
        from video_filter import register_video_filter
        with patch.object(EnvConfig, "video_filter_enabled", return_value=False):
            register_video_filter()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.free = self.stack.enter_context(patch.object(resources, "free_memory", return_value=16384))
        self.stack.enter_context(patch.object(resources, "process_memory", return_value={}))
        self.stack.enter_context(patch.object(resources, "process_identity", return_value="fixture-start"))
        self.stack.enter_context(patch.object(resources, "gpu_identity", return_value="GPU-fixture"))
        self.stack.enter_context(patch.object(EnvConfig, "video_filter_enabled", return_value=True))
        self.stack.enter_context(patch.object(EnvConfig, "video_filter_settings", return_value={
            "grouped": True, "gpu_safety_mib": 2048}))
        self.stack.enter_context(patch.object(EnvConfig, "video_compression_gpu_settings", return_value={
            "peak_mib": 1024, "wait_seconds": 10}))

    def request(self, owner, workload="extract", **kwargs):
        return resources.request_lease(db.session, "GPU-fixture", "extract_shared", owner,
            workload_type=workload, **kwargs)

    def test_compression_and_six_extracts_share_memory_with_independent_quotas(self):
        extracts = [self.request("extract-" + str(i)) for i in range(6)]
        self.assertTrue(all(extracts))
        compression = self.request("encode", "compression")
        self.assertIsNotNone(compression)
        self.assertIsNone(self.request("encode-two", "compression"))
        self.assertIsNone(self.request("extract-seven"))
        self.assertIsNotNone(self.request("predict", "predict", memory_budget_mib=256))
        resources.release(db.session, extracts[0])
        self.assertIsNotNone(self.request("replacement"))
        resources.release(db.session, compression)
        self.assertIsNotNone(self.request("encode-two", "compression"))

    def test_memory_budget_and_one_safety_margin_cover_compression(self):
        self.free.return_value = 4096
        self.assertIsNotNone(self.request("encode", "compression", memory_budget_mib=1536, reserve_mib=2048))
        self.assertIsNone(self.request("extract", reserve_mib=2048))
        decision = resources.admission_decisions()["GPU-fixture"]
        self.assertEqual((1536, 2560, "memory_insufficient"),
            (decision["pending_reserved_mib"], decision["usable_free_mib"], decision["reason"]))
        self.free.return_value = 8192
        self.assertIsNotNone(self.request("extract", reserve_mib=2048))

    def test_training_and_legacy_compression_remain_exclusive(self):
        encoder = self.request("encode", "compression")
        self.assertIsNone(resources.request_lease(db.session, "GPU-fixture", "exclusive_train", "train"))
        self.assertIsNone(self.request("late-extract"))
        resources.release(db.session, encoder)
        train = resources.request_lease(db.session, "GPU-fixture", "exclusive_train", "train")
        self.assertIsNotNone(train)
        self.assertIsNone(self.request("late-encode", "compression"))
        resources.release(db.session, train)
        resources.release(db.session, owner="late-extract")
        resources.release(db.session, owner="late-encode")
        legacy = resources.request_lease(db.session, "GPU-fixture", "exclusive_compression", "legacy")
        self.assertIsNotNone(legacy)
        self.assertIsNone(self.request("late-encode", "compression"))

    def test_waiting_compression_does_not_block_new_extract_as_exclusive(self):
        self.assertIsNotNone(self.request("encode", "compression"))
        self.assertIsNone(self.request("encode-two", "compression"))
        self.assertIsNotNone(self.request("late-extract"))

    def test_oldest_waiting_encoder_retains_room_while_extract_uses_spare_memory(self):
        self.free.return_value = 2048
        self.assertIsNone(self.request("encode", "compression", reserve_mib=2048))
        self.assertIsNone(self.request("encode-two", "compression", reserve_mib=2048))
        self.free.return_value = 4096
        self.assertIsNotNone(self.request("spare-extract", reserve_mib=2048))
        decision = resources.admission_decisions()["GPU-fixture"]
        self.assertEqual(1024, decision["waiting_compression_reserved_mib"])
        self.assertEqual(1024, decision["pending_reserved_mib"])
        self.assertIsNone(self.request("replacement-extract", reserve_mib=2048))
        self.assertEqual("memory_insufficient", resources.admission_decisions()["GPU-fixture"]["reason"])
        self.assertIsNotNone(self.request("encode", "compression", reserve_mib=2048))

    def test_expired_encoder_request_no_longer_reserves_memory(self):
        from datetime import timedelta
        from media_lineage.models import utc_now
        self.free.return_value = 2048
        self.assertIsNone(self.request("encode", "compression", reserve_mib=2048))
        row = db.session.query(ResourceLease).one()
        row.heartbeat_at = utc_now() - timedelta(seconds=301)
        db.session.commit()
        self.free.return_value = 3072
        self.assertIsNotNone(self.request("extract", reserve_mib=2048))
        self.assertEqual(0, resources.admission_decisions()["GPU-fixture"]["waiting_compression_reserved_mib"])

    def test_context_releases_on_success_and_encoder_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                try:
                    with compression_gpu() as lease:
                        self.assertEqual(lease, resources.current_lease())
                        row = db.session.get(ResourceLease, lease)
                        self.assertEqual(("extract_shared", "active", "compression", 1024),
                            (row.mode, row.status, row.memory_observation["workload_type"], row.memory_budget_mib))
                        if fail:
                            raise ValueError("fixture-encoder-error")
                except ValueError:
                    self.assertTrue(fail)
                self.assertIsNone(resources.current_lease())
                self.assertEqual("released", db.session.get(ResourceLease, lease, populate_existing=True).status)

    def test_timeout_releases_waiting_request_and_never_enters_gpu_body(self):
        self.free.return_value = 100
        with patch("time.monotonic", return_value=0), patch("time.sleep") as pause:
            def expire(_):
                raise RuntimeError("fixture-wait-interrupted")
            pause.side_effect = expire
            with self.assertRaisesRegex(RuntimeError, "fixture-wait-interrupted"):
                with compression_gpu():
                    self.fail("insufficient memory must not start encoding")
        rows = db.session.query(ResourceLease).all()
        self.assertEqual(1, len(rows))
        self.assertEqual(("extract_shared", "released"), (rows[0].mode, rows[0].status))
        with patch.object(resources, "request_lease", return_value=None), \
                patch("time.monotonic", side_effect=[0, 11]), \
                patch.object(resources, "release") as released:
            with self.assertRaisesRegex(RuntimeError, "compression_gpu_admission_timeout"):
                with compression_gpu():
                    self.fail("timed out admission must not start encoding")
            released.assert_called_once()

    def test_wait_retries_until_memory_is_available_without_exclusive_request(self):
        self.free.return_value = 100
        with patch("time.sleep", side_effect=lambda _: setattr(self.free, "return_value", 8192)) as pause:
            with compression_gpu() as lease:
                pause.assert_called_once()
                self.assertEqual("active", db.session.get(ResourceLease, lease).status)
        self.assertEqual(1, db.session.query(ResourceLease).count())
        self.assertEqual("released", db.session.query(ResourceLease).one().status)

    def test_direct_low_bitrate_copy_never_requests_gpu(self):
        from video_compression.service import CompressionService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            source.write_bytes(b"synthetic")
            output = root / "output"
            output.mkdir()
            service = CompressionService(root, output)
            service._validate_tools_and_paths = Mock()
            service.validate_deliverable = Mock()
            with patch("media_lineage.integration.enabled", return_value=False), \
                    patch("video_compression.service.probe_video", return_value=SimpleNamespace(
                        codec_name="h264", display_width=3840, display_height=2160, fps=60.,
                        bit_rate=4_000_000, duration=10.)), \
                    patch.object(resources, "request_lease") as admission:
                result = service.process(source)
            admission.assert_not_called()
            self.assertFalse(result.transcoded)
            self.assertEqual(b"synthetic", result.destination.read_bytes())
            self.assertFalse(source.exists())

    def test_encoder_registers_process_and_releases_before_validation_and_copy(self):
        from video_compression.service import CompressionService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            source.write_bytes(b"synthetic")
            output, cache = root / "output", root / "cache"
            output.mkdir()
            cache.mkdir()
            service = CompressionService(root, output, cache)
            service._validate_tools_and_paths = Mock()
            def outside_gpu(*args, **kwargs):
                self.assertIsNone(resources.current_lease())
                self.assertFalse(db.session.query(ResourceLease).filter_by(status="active").count())
            service.validate_transcoded_output = Mock(side_effect=outside_gpu)
            service.validate_deliverable = Mock(side_effect=outside_gpu)
            original_copy = service._copy_without_overwrite
            def copy(*args):
                outside_gpu()
                original_copy(*args)
            service._copy_without_overwrite = copy
            process = Mock()
            process.stdout = io.StringIO("progress=end\n")
            process.wait.return_value = process.poll.return_value = 0
            def start(command, **kwargs):
                lease = resources.current_lease()
                self.assertIsNotNone(lease)
                self.assertEqual("active", db.session.get(ResourceLease, lease).status)
                Path(command[-1]).write_bytes(b"encoded")
                return process
            identities = [{"pid": 12345, "identity": "fixture-start"}]
            with patch("video_compression.service.probe_video", return_value=SimpleNamespace(
                    codec_name="h264", display_width=1920, display_height=1080, fps=60.,
                    bit_rate=10_000_000, duration=10.)), \
                    patch("video_compression.transcoder.subprocess.Popen", side_effect=start), \
                    patch("media_lineage.process_tree.ProcessTree") as tree:
                tree.return_value.identities.return_value = identities
                prepared = service.prepare(source, 7, service.destination_for(source.name))
            self.assertEqual(b"encoded", prepared.staging.read_bytes())
            self.assertTrue(source.exists())
            service.publish_prepared(7, prepared.destination)
            row = db.session.query(ResourceLease).one()
            self.assertEqual("released", row.status)
            self.assertEqual(identities, row.memory_observation["processes"])
            tree.return_value.close.assert_called_once()

    def test_lost_lease_terminates_encoder_before_release(self):
        from video_compression.policy import CompressionPlan
        from video_compression.transcoder import transcode, TranscodeError
        process = Mock()
        process.stdout = io.StringIO("")
        process.poll.return_value = None
        with patch("video_compression.transcoder.subprocess.Popen", return_value=process), \
                patch("media_lineage.process_tree.ProcessTree") as tree, \
                patch.object(resources, "heartbeat", return_value=False):
            tree.return_value.identities.return_value = []
            with self.assertRaisesRegex(TranscodeError, "compression_resource_lease_lost"):
                transcode("ffmpeg.exe", "synthetic.mp4", "output.mp4",
                    CompressionPlan(True, 1920, 1080, False, ("fixture",)), 60)
            tree.return_value.terminate.assert_called_once()
            process.wait.assert_called_once()
            tree.return_value.close.assert_called_once()
        self.assertEqual("released", db.session.query(ResourceLease).one().status)

    def test_long_encoder_execution_refreshes_heartbeat(self):
        from itertools import chain, repeat
        from video_compression.policy import CompressionPlan
        from video_compression.transcoder import transcode
        process = Mock()
        process.stdout = io.StringIO("progress=end\n")
        process.wait.return_value = process.poll.return_value = 0
        clock = Mock()
        clock.monotonic.side_effect = chain((0, 0), repeat(11))
        with patch("video_compression.transcoder.subprocess.Popen", return_value=process), \
                patch("video_compression.transcoder.time", clock), \
                patch("media_lineage.process_tree.ProcessTree") as tree, \
                patch.object(resources, "heartbeat", wraps=resources.heartbeat) as heartbeat:
            tree.return_value.identities.return_value = [{"pid": 12345, "identity": "fixture-start"}]
            transcode("ffmpeg.exe", "synthetic.mp4", "output.mp4",
                CompressionPlan(True, 1920, 1080, False, ("fixture",)), 60)
            self.assertEqual(2, heartbeat.call_count)
        self.assertEqual("released", db.session.query(ResourceLease).one().status)

    def test_dashboard_counts_shared_compression_and_explains_unqueried_memory(self):
        from video_filter.dashboard import register_dashboard
        from video_filter.tests.support import app
        from video_filter.reporting import groups_snapshot
        register_dashboard(app)
        self.assertIsNotNone(self.request("encode", "compression"))
        self.assertIsNone(self.request("encode-two", "compression"))
        settings = {"enabled": True, "grouped": True, "groups": [], "extract_concurrency": 6}
        with patch.object(EnvConfig, "video_filter_settings", return_value=settings):
            with patch.object(db.session, "commit", side_effect=AssertionError("dashboard must be read only")):
                snapshot = groups_snapshot(db.session, settings)["resources"]
                self.assertEqual((1, 1, 0, 0), tuple(snapshot[key] for key in
                    ("compression_active", "compression_waiting", "exclusive_active", "exclusive_waiting")))
                response = app.test_client().get("/video_filter/dashboard/fragment")
        self.assertEqual(200, response.status_code)
        page = response.get_data(as_text=True)
        self.assertIn("其中压缩运行 1 / 1", page)
        self.assertIn("未查询（本轮未进入显存检查）", page)
        self.assertNotIn("未知 MiB", page)

    def test_dashboard_distinguishes_driver_query_failure(self):
        from video_filter.dashboard import register_dashboard
        from video_filter.tests.support import app
        register_dashboard(app)
        self.free.side_effect = OSError("fixture-driver-error")
        self.assertIsNone(self.request("encode", "compression"))
        with patch.object(EnvConfig, "video_filter_settings", return_value={
                "enabled": True, "grouped": True, "groups": [], "extract_concurrency": 6}):
            response = app.test_client().get("/video_filter/dashboard/fragment")
        self.assertEqual(200, response.status_code)
        self.assertIn("查询失败", response.get_data(as_text=True))


class CompressionConfigTests(DatabaseTestCase):
    def test_defaults_overrides_and_positive_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual({"peak_mib": 1024, "wait_seconds": 1800}, EnvConfig.video_compression_gpu_settings())
            with patch.dict(os.environ, {"VIDEO_COMPRESSION_GPU_PEAK_MIB": "1536", "VIDEO_COMPRESSION_GPU_WAIT_SECONDS": "60"}):
                self.assertEqual({"peak_mib": 1536, "wait_seconds": 60}, EnvConfig.video_compression_gpu_settings())
            for key in ("VIDEO_COMPRESSION_GPU_PEAK_MIB", "VIDEO_COMPRESSION_GPU_WAIT_SECONDS"):
                for value in ("0", "-1", "invalid"):
                    with patch.dict(os.environ, {key: value}):
                        with self.assertRaisesRegex(RuntimeError, "GPU configuration"):
                            EnvConfig.video_compression_gpu_settings()
