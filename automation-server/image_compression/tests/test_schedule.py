import importlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from image_compression.manifest import ContentManifest
from image_compression.models import ImageCompressionStatus

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


if __name__ == "__main__":
    unittest.main()
