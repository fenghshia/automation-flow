import ast
import importlib
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy.dialects import postgresql

from app import scheduler
from video_compression.models import CompressionStatus
from video_compression.service import CompressionService


schedule_module = importlib.import_module(
    "video_compression.schedules.compress_videos"
)


class SchedulerLifecycleTests(unittest.TestCase):
    def test_import_does_not_start_scheduler(self):
        self.assertFalse(scheduler.running)

    def test_main_starts_scheduler_without_reloader(self):
        main_path = Path(__file__).parents[2] / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        run_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        calls = [node for node in ast.walk(run_function) if isinstance(node, ast.Call)]

        self.assertTrue(
            any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "start"
                for call in calls
            )
        )
        app_run = next(
            call
            for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == "run"
        )
        keywords = {keyword.arg: keyword.value for keyword in app_run.keywords}
        self.assertIs(keywords["use_reloader"].value, False)

    def test_recovery_runs_before_discovery_and_ready_claim(self):
        source_directory = Path("source").resolve()
        output_directory = Path("output").resolve()
        service = Mock()
        with patch.object(
            schedule_module.EnvConfig,
            "video_compression_source_directory",
            return_value=source_directory,
        ), patch.object(
            schedule_module.EnvConfig,
            "video_compression_output_directory",
            return_value=output_directory,
        ), patch.object(
            schedule_module.EnvConfig,
            "video_compression_ffmpeg_bin_directory",
            return_value=Path("ffmpeg").resolve(),
        ), patch.object(
            schedule_module,
            "CompressionService",
            return_value=service,
        ), patch.object(
            schedule_module,
            "finish_cleanup_pending",
            return_value=False,
        ) as cleanup, patch.object(
            schedule_module,
            "recover_validating_mission",
            return_value=True,
        ) as validating, patch.object(
            schedule_module,
            "recover_processing_mission",
        ) as processing, patch.object(
            schedule_module,
            "refresh_missions",
        ) as refresh:
            schedule_module.process_one_mission()

        cleanup.assert_called_once_with(service, source_directory)
        validating.assert_called_once_with(service, source_directory)
        processing.assert_not_called()
        refresh.assert_not_called()


class MissionDiscoveryTests(unittest.TestCase):
    def test_insert_uses_source_path_conflict_protection(self):
        session = Mock()
        session.execute.return_value.scalar_one_or_none.return_value = None
        path = Path("example.mp4")
        stat = SimpleNamespace(st_size=12, st_mtime_ns=34)

        inserted = schedule_module.insert_mission_if_missing(session, path, stat)

        self.assertFalse(inserted)
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (source_path) DO NOTHING", sql)


class InterruptedMissionRecoveryTests(unittest.TestCase):
    def make_fixture(self, directory, status, output_path=None):
        root = Path(directory)
        source_directory = root / "source"
        output_directory = root / "output"
        cache_directory = root / "cache"
        source_directory.mkdir()
        output_directory.mkdir()
        cache_directory.mkdir()
        source = source_directory / "example.mp4"
        source.write_bytes(b"source")
        service = CompressionService(root, output_directory, cache_directory)
        service.validate_output = Mock(return_value=None)
        mission = SimpleNamespace(
            id=7,
            source_path=str(source.resolve()),
            file_name=source.name,
            output_path=output_path,
            status=status,
            error_message=None,
        )
        return mission, service, source_directory, source

    def run_recovery(self, function, mission, service, source_directory):
        with schedule_module.app.app_context(), patch.object(
            schedule_module.CompressionMission, "query"
        ) as query, patch.object(
            schedule_module.db.session, "commit"
        ) as commit, patch("builtins.print"):
            query.filter_by.return_value.order_by.return_value.first.return_value = (
                mission
            )
            recovered = function(service, source_directory)
        self.assertTrue(recovered)
        commit.assert_called()

    def test_processing_without_artifacts_is_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            mission, service, source_directory, source = self.make_fixture(
                directory, CompressionStatus.PROCESSING
            )

            self.run_recovery(
                schedule_module.recover_processing_mission,
                mission,
                service,
                source_directory,
            )

            self.assertEqual(CompressionStatus.READY, mission.status)
            self.assertTrue(source.exists())
            self.assertEqual(
                str(service.destination_for(mission.file_name)), mission.output_path
            )

    def test_processing_with_unowned_output_fails_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            mission, service, source_directory, source = self.make_fixture(
                directory, CompressionStatus.PROCESSING
            )
            output = service.destination_for(mission.file_name)
            output.write_bytes(b"external")

            self.run_recovery(
                schedule_module.recover_processing_mission,
                mission,
                service,
                source_directory,
            )

            self.assertEqual(CompressionStatus.FAILED, mission.status)
            self.assertTrue(source.exists())
            self.assertEqual(b"external", output.read_bytes())

    def test_processing_with_published_hard_link_moves_to_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            mission, service, source_directory, source = self.make_fixture(
                directory, CompressionStatus.PROCESSING
            )
            output = service.destination_for(mission.file_name)
            staging = service.staging_path_for(mission.id, output)
            staging.write_bytes(b"video")
            os.link(staging, output)

            self.run_recovery(
                schedule_module.recover_processing_mission,
                mission,
                service,
                source_directory,
            )

            self.assertEqual(CompressionStatus.CLEANUP_PENDING, mission.status)
            self.assertTrue(source.exists())
            self.assertTrue(output.exists())
            self.assertFalse(staging.exists())

    def test_validating_staging_is_published_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            mission, service, source_directory, source = self.make_fixture(
                directory, CompressionStatus.VALIDATING
            )
            output = service.destination_for(mission.file_name)
            mission.output_path = str(output)
            staging = service.staging_path_for(mission.id, output)
            staging.write_bytes(b"video")

            self.run_recovery(
                schedule_module.recover_validating_mission,
                mission,
                service,
                source_directory,
            )

            self.assertEqual(CompressionStatus.CLEANUP_PENDING, mission.status)
            self.assertTrue(source.exists())
            self.assertEqual(b"video", output.read_bytes())
            self.assertFalse(staging.exists())

    def test_cleanup_pending_validates_output_before_deleting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            mission, service, source_directory, source = self.make_fixture(
                directory, CompressionStatus.CLEANUP_PENDING
            )
            output = service.destination_for(mission.file_name)
            mission.output_path = str(output)
            output.write_bytes(b"video")

            self.run_recovery(
                schedule_module.finish_cleanup_pending,
                mission,
                service,
                source_directory,
            )

            self.assertEqual(CompressionStatus.COMPLETED, mission.status)
            self.assertFalse(source.exists())
            self.assertTrue(output.exists())
            service.validate_output.assert_called_once_with(output)

    def test_cleanup_validation_failure_preserves_source_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            mission, service, source_directory, source = self.make_fixture(
                directory, CompressionStatus.CLEANUP_PENDING
            )
            output = service.destination_for(mission.file_name)
            mission.output_path = str(output)
            output.write_bytes(b"invalid")
            service.validate_output.side_effect = RuntimeError("invalid output")

            self.run_recovery(
                schedule_module.finish_cleanup_pending,
                mission,
                service,
                source_directory,
            )

            self.assertEqual(CompressionStatus.FAILED, mission.status)
            self.assertTrue(source.exists())
            self.assertEqual(b"invalid", output.read_bytes())


class CrossProcessLockTests(unittest.TestCase):
    @staticmethod
    def make_lock_engine(acquired):
        engine = MagicMock()
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = acquired
        engine.connect.return_value.__enter__.return_value = connection
        return engine, connection

    def test_lock_is_released_after_success(self):
        engine, connection = self.make_lock_engine(True)

        with schedule_module.video_compression_lock(engine) as acquired:
            self.assertTrue(acquired)

        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("pg_advisory_unlock", str(connection.execute.call_args.args[0]))

    def test_lock_is_released_after_error(self):
        engine, connection = self.make_lock_engine(True)

        with self.assertRaisesRegex(RuntimeError, "failed"):
            with schedule_module.video_compression_lock(engine):
                raise RuntimeError("failed")

        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("pg_advisory_unlock", str(connection.execute.call_args.args[0]))

    def test_job_skips_when_lock_is_held_elsewhere(self):
        with patch.object(
            schedule_module,
            "video_compression_lock",
            return_value=nullcontext(False),
        ), patch.object(schedule_module, "process_one_mission") as process:
            schedule_module.compress_videos()

        process.assert_not_called()

    def test_job_rolls_back_and_reraises_unhandled_error(self):
        with schedule_module.app.app_context(), patch.object(
            schedule_module,
            "video_compression_lock",
            return_value=nullcontext(True),
        ), patch.object(
            schedule_module,
            "process_one_mission",
            side_effect=RuntimeError("failed"),
        ), patch.object(schedule_module.db.session, "rollback") as rollback:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                schedule_module.compress_videos()

        rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
