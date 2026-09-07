import importlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from image_compression.manifest import ContentManifest
from image_compression.models import ImageCompressionStatus
from image_compression.policy import LEGACY_PASSTHROUGH_FAILURES

schedule_module = importlib.import_module(
    "image_compression.schedules.process_images"
)


class SchedulerTests(unittest.TestCase):
    @staticmethod
    def make_lock_engine(acquired):
        engine = MagicMock()
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = acquired
        engine.connect.return_value.__enter__.return_value = connection
        return engine, connection

    def test_lock_is_released_after_success(self):
        engine, connection = self.make_lock_engine(True)
        with schedule_module.image_compression_lock(engine) as acquired:
            self.assertTrue(acquired)
        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("pg_advisory_unlock", str(connection.execute.call_args.args[0]))

    def test_scheduler_registration_uses_required_interval(self):
        job = schedule_module.scheduler.get_job("image_compression_process_one")
        self.assertIsNotNone(job)
        self.assertEqual(30, job.trigger.interval.total_seconds())
        self.assertEqual(1, job.max_instances)
        self.assertFalse(schedule_module.scheduler.running)

    def test_job_skips_when_cross_process_lock_is_held(self):
        with patch.object(
            schedule_module,
            "image_compression_lock",
            return_value=nullcontext(False),
        ), patch.object(schedule_module, "process_one_mission") as process:
            schedule_module.process_images()
        process.assert_not_called()

    def test_job_logs_rolls_back_and_consumes_unhandled_error(self):
        error = RuntimeError("image-scheduler-marker")
        with patch.object(
            schedule_module,
            "image_compression_lock",
            return_value=nullcontext(True),
        ), patch.object(
            schedule_module,
            "process_one_mission",
            side_effect=error,
        ), patch.object(
            schedule_module.db.session,
            "rollback",
        ) as rollback, patch.object(
            schedule_module,
            "log_exception",
        ) as log_exception:
            result = schedule_module.process_images()

        self.assertIsNone(result)
        rollback.assert_called_once_with()
        self.assertIs(error, log_exception.call_args_list[0].args[2])

    def test_job_preserves_original_error_when_rollback_also_fails(self):
        original = RuntimeError("image-original-marker")
        rollback_error = RuntimeError("image-rollback-marker")
        with patch.object(
            schedule_module,
            "image_compression_lock",
            return_value=nullcontext(True),
        ), patch.object(
            schedule_module,
            "process_one_mission",
            side_effect=original,
        ), patch.object(
            schedule_module.db.session,
            "rollback",
            side_effect=rollback_error,
        ), patch.object(
            schedule_module,
            "log_exception",
        ) as log_exception:
            result = schedule_module.process_images()

        self.assertIsNone(result)
        self.assertEqual(2, log_exception.call_count)
        self.assertIs(original, log_exception.call_args_list[0].args[2])
        self.assertIs(rollback_error, log_exception.call_args_list[1].args[2])

    def test_discovery_is_shallow_and_ignores_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "group").mkdir()
            (root / "group" / "nested.txt").write_bytes(b"nested")
            (root / ".autoflow-image-1.pending").write_bytes(b"pending")
            discovered = schedule_module.discover_source_items(root)
            self.assertEqual(["group"], [path.name for path in discovered])

    def test_moving_mission_completes_all_recoverable_stages(self):
        manifest = ContentManifest("a" * 64, 1, 4)
        destination = Path("output.txt")
        mission = SimpleNamespace(
            id=1,
            source_path="source.txt",
            source_kind="file",
            source_name="source.txt",
            destination_key="output.txt",
            status=ImageCompressionStatus.MOVING,
            output_path=None,
            output_manifest_sha256=None,
            output_file_count=None,
            output_size_bytes=None,
            error_message=None,
        )
        prepared = SimpleNamespace()
        plan = SimpleNamespace(destination=destination, manifest=manifest)
        service = MagicMock()
        service.prepare_batch.return_value = prepared
        service.stage_for_publish.return_value = plan
        session = MagicMock()
        session.get.return_value = mission

        with patch.object(schedule_module.db, "session", session), patch("builtins.print"):
            result = schedule_module.process_mission(service, mission)

        self.assertEqual(destination, result)
        self.assertEqual(ImageCompressionStatus.COMPLETED, mission.status)
        service.ensure_ingested.assert_called()
        service.publish.assert_called_once_with(plan)
        service.validate_path.assert_called_once_with(str(destination), manifest)
        service.cleanup_completed.assert_called_once_with(1)

    def test_cleanup_os_error_remains_recoverable(self):
        mission = SimpleNamespace(
            id=2,
            source_name="source.txt",
            status=ImageCompressionStatus.CLEANUP_PENDING,
            output_path="output.txt",
            output_manifest_sha256="b" * 64,
            output_file_count=1,
            output_size_bytes=4,
            error_message=None,
        )
        service = MagicMock()
        service.cleanup_completed.side_effect = OSError("busy")
        session = MagicMock()
        session.get.return_value = mission

        with patch.object(schedule_module.db, "session", session), patch("builtins.print"):
            result = schedule_module.process_mission(service, mission)

        self.assertIsNone(result)
        self.assertEqual(ImageCompressionStatus.CLEANUP_PENDING, mission.status)
        self.assertEqual("busy", mission.error_message)

    def test_legacy_format_errors_are_exactly_scoped(self):
        self.assertEqual(
            {
                "Images of format GIF above 3 MiB are not supported",
                "Images of format WEBP above 3 MiB are not supported",
            },
            LEGACY_PASSTHROUGH_FAILURES,
        )

    def test_retry_assets_require_pending_source_and_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "pending"
            output = root / "output"
            (pending / "1").mkdir(parents=True)
            output.mkdir()
            mission = SimpleNamespace(
                id=1,
                source_path=str(root / "source.gif"),
                destination_key="source.gif",
                output_path=None,
            )
            service = SimpleNamespace(
                output_directory=output.resolve(),
                mission_directory=lambda mission_id: pending / str(mission_id),
                publish_staging_path=lambda mission_id: output
                / f".autoflow-image-{mission_id}.part",
                ensure_ingested=MagicMock(),
            )

            safe, reason = schedule_module._retry_assets_are_safe(service, mission)
            self.assertFalse(safe)
            self.assertEqual("pending source is missing", reason)

            (pending / "1" / "source").write_bytes(b"source")
            safe, reason = schedule_module._retry_assets_are_safe(service, mission)
            self.assertTrue(safe)
            self.assertIsNone(reason)
            service.ensure_ingested.assert_called_once_with(mission.source_path, 1)

            (output / "source.gif").write_bytes(b"existing")
            safe, reason = schedule_module._retry_assets_are_safe(service, mission)
            self.assertFalse(safe)
            self.assertEqual("final output already exists", reason)

            (output / "source.gif").unlink()
            (output / ".autoflow-image-1.part").write_bytes(b"staged")
            safe, reason = schedule_module._retry_assets_are_safe(service, mission)
            self.assertFalse(safe)
            self.assertEqual("publish staging already exists", reason)

    def test_retry_destination_detects_another_active_mission(self):
        mission = SimpleNamespace(id=4, destination_key_normalized="source.gif")
        query = MagicMock()
        query.filter.return_value.first.return_value = SimpleNamespace(id=5)

        with schedule_module.app.app_context():
            with patch.object(
                schedule_module.ImageCompressionMission, "query", query
            ):
                reserved = schedule_module._retry_destination_is_reserved(mission)

        self.assertTrue(reserved)
        query.filter.return_value.first.assert_called_once_with()

    def test_claim_retryable_format_failure_uses_conditional_update(self):
        candidate = SimpleNamespace(
            id=4,
            error_message="Images of format GIF above 3 MiB are not supported",
            destination_key_normalized="source.gif",
        )
        claimed_mission = SimpleNamespace(id=4, status=ImageCompressionStatus.PROCESSING)
        query = MagicMock()
        query.filter.return_value.order_by.return_value.all.return_value = [candidate]
        update_query = MagicMock()
        update_query.filter.return_value.update.return_value = 1
        session = MagicMock()
        session.query.return_value = update_query
        session.get.return_value = claimed_mission

        with schedule_module.app.app_context():
            with patch.object(
                schedule_module.ImageCompressionMission, "query", query
            ), patch.object(
                schedule_module.db, "session", session
            ), patch.object(
                schedule_module, "_retry_assets_are_safe", return_value=(True, None)
            ), patch.object(
                schedule_module, "_retry_destination_is_reserved", return_value=False
            ):
                result = schedule_module.claim_retryable_format_failure(MagicMock())

        self.assertIs(claimed_mission, result)
        update_query.filter.return_value.update.assert_called_once()
        updates = update_query.filter.return_value.update.call_args.args[0]
        self.assertEqual(
            ImageCompressionStatus.PROCESSING,
            updates[schedule_module.ImageCompressionMission.status],
        )
        self.assertIsNone(updates[schedule_module.ImageCompressionMission.error_message])
        self.assertIsNone(updates[schedule_module.ImageCompressionMission.output_path])
        session.commit.assert_called_once_with()
        session.get.assert_called_once_with(schedule_module.ImageCompressionMission, 4)

    def test_claim_retryable_format_failure_skips_unsafe_candidate(self):
        candidates = [
            SimpleNamespace(
                id=4, error_message="first", destination_key_normalized="first.gif"
            ),
            SimpleNamespace(
                id=5, error_message="second", destination_key_normalized="second.gif"
            ),
        ]
        claimed_mission = SimpleNamespace(id=5, status=ImageCompressionStatus.PROCESSING)
        query = MagicMock()
        query.filter.return_value.order_by.return_value.all.return_value = candidates
        update_query = MagicMock()
        update_query.filter.return_value.update.return_value = 1
        session = MagicMock()
        session.query.return_value = update_query
        session.get.return_value = claimed_mission

        with schedule_module.app.app_context():
            with patch.object(
                schedule_module.ImageCompressionMission, "query", query
            ), patch.object(
                schedule_module.db, "session", session
            ), patch.object(
                schedule_module,
                "_retry_assets_are_safe",
                side_effect=[(False, "missing"), (True, None)],
            ), patch.object(
                schedule_module, "_retry_destination_is_reserved", return_value=False
            ):
                result = schedule_module.claim_retryable_format_failure(MagicMock())

        self.assertIs(claimed_mission, result)
        session.get.assert_called_once_with(schedule_module.ImageCompressionMission, 5)


if __name__ == "__main__":
    unittest.main()
