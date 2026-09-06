import ast
import importlib
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy.dialects import postgresql

from app import scheduler


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
