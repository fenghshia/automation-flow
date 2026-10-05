import io
import logging
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from video_filter.tests.support import DatabaseTestCase, db, signature
from video_filter.tests import test_workflow as workflow
from video_filter.models import Asset, Variant
from video_filter.learning import train
from video_filter.observability import log_failure, progress_phase
from video_filter.worker_client import run_extraction
from sqlalchemy.exc import StatementError
import logging_config


def original_failure():
    raise ValueError("original-failure-marker")


class LoggingTests(DatabaseTestCase):
    save_bundle = workflow.WorkflowTests.save_bundle

    def setUp(self):
        super().setUp()
        logging_config._reset_logging_for_tests()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.console = io.StringIO()
        logging_config.configure_logging(base_directory=self.root / "logs-root", stream=self.console)

    def tearDown(self):
        logging_config._reset_logging_for_tests()
        self.temporary.cleanup()
        super().tearDown()

    def contents(self, name):
        for handler in logging.getLogger()._autoflow_logging_state["handlers"]:
            handler.flush()
        return (self.root / "logs-root" / name).read_text(encoding="utf-8")

    def test_info_and_complete_chained_errors_route_to_separate_project_files(self):
        logger = logging.getLogger("video_filter.runtime")
        logger.info("normal-progress-marker")
        try:
            try:
                original_failure()
            except ValueError as original:
                raise RuntimeError("outer-failure-marker") from original
        except RuntimeError as error:
            log_failure(logger, "task failed", error)
        runtime = self.contents("video_filter/logs/runtime.log")
        errors = self.contents("video_filter/logs/error.log")
        self.assertIn("normal-progress-marker", runtime)
        self.assertNotIn("normal-progress-marker", errors)
        for marker in ("original_failure", "original-failure-marker", "outer-failure-marker", "direct cause", "Traceback"):
            self.assertIn(marker, runtime)
            self.assertIn(marker, errors)
        self.assertNotIn("outer-failure-marker", self.contents("logs/error.log"))

    def test_sql_bound_feature_payload_is_hidden_without_losing_stack(self):
        failure = StatementError("database-write-failed", "INSERT INTO summary VALUES (:blob)",
            {"blob": "private-vector-payload"}, RuntimeError("database unavailable"), hide_parameters=False)
        try:
            raise failure
        except StatementError as error:
            log_failure(logging.getLogger("video_filter.runtime"), "summary commit failed", error)
            self.assertFalse(error.hide_parameters)
        contents = self.contents("video_filter/logs/error.log")
        self.assertIn("Traceback", contents)
        self.assertIn("database-write-failed", contents)
        self.assertIn("SQL parameters hidden", contents)
        self.assertNotIn("private-vector-payload", contents)

    def test_actual_worker_failure_preserves_remote_and_parent_stack(self):
        # No GPU or model download: fail while loading a nonexistent local manifest.
        request = {"task_id": str(uuid4()), "asset_id": str(uuid4()), "variant_id": str(uuid4()),
            "path": str(self.root / "private-video.mp4"), "model_manifest": str(self.root / "missing-manifest.json"),
            "state_directory": str(self.root / "state"), "ffmpeg_directory": str(self.root / "tools"),
            "device": "cpu", "feature_signature": "a" * 64}
        try:
            run_extraction(request, timeout=30)
        except ValueError as error:
            self.assertEqual("extraction_worker_failed", str(error))
            log_failure(logging.getLogger("video_filter.runtime"), "task worker failed", error)
        else:
            self.fail("Missing model manifest must fail.")
        runtime = self.contents("video_filter/logs/runtime.log")
        errors = self.contents("video_filter/logs/error.log")
        self.assertIn("提取子进程启动", runtime)
        for marker in ("FileNotFoundError", "load_model_manifest", "run_extraction", "RemoteWorkerError", "Traceback"):
            self.assertIn(marker, errors)
        self.assertNotIn(str(self.root / "missing-manifest.json"), errors)
        self.assertNotIn("private-video.mp4", errors)

    def test_fit_heartbeat_and_training_completion_metrics_are_logged(self):
        logger = logging.getLogger("video_filter.learning")
        with progress_phase(logger, "test-fit", task_id="test-task", interval=0.01):
            time.sleep(0.04)
        workflow.WorkflowTests.trained_model(self)
        contents = self.contents("video_filter/logs/runtime.log")
        for marker in ("阶段仍在运行", "个人分类器训练开始", "positives=10", "negatives=10", "iterations=", "balanced_accuracy=", "roc_auc=", "模型已保存到数据库", "status=active"):
            self.assertIn(marker, contents)

    def test_worker_progress_is_forwarded_before_process_exit(self):
        request = {"task_id": "streaming-task", "state_directory": str(self.root / "state")}
        forwarded = threading.Event()
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO(json.dumps({"type": "video_filter_log", "level": "INFO",
            "message": "live-progress-marker", "task_id": request["task_id"]}) + "\n")
        process.poll.return_value = 1

        def wait(timeout=None):
            self.assertTrue(forwarded.wait(2), "Progress must arrive while the worker is still running.")
            self.assertIn("live-progress-marker", self.contents("video_filter/logs/runtime.log"))
            return 1

        original_log = logging.getLogger("video_filter.worker_client").log
        def log(*args, **kwargs):
            original_log(*args, **kwargs)
            forwarded.set()

        process.wait.side_effect = wait
        with patch("video_filter.worker_client.subprocess.Popen", return_value=process), patch("video_filter.worker_client.logger.log", side_effect=log), patch("video_filter.worker_client.ProcessTree"):
            with self.assertRaisesRegex(ValueError, "extraction_worker_failed"):
                run_extraction(request, timeout=3)

    def test_worker_timeout_kills_process_and_keeps_exception_chain(self):
        request = {"task_id": "timeout-task", "state_directory": str(self.root / "state")}
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO("")
        process.poll.return_value = -9
        process.wait.side_effect = [subprocess.TimeoutExpired("local-worker", 0.01), -9]
        with patch("video_filter.worker_client.subprocess.Popen", return_value=process), patch("video_filter.worker_client.ProcessTree") as tree:
            try:
                run_extraction(request, timeout=0.01)
            except ValueError as error:
                self.assertEqual("extraction_timeout", str(error))
                self.assertIsInstance(error.__cause__, subprocess.TimeoutExpired)
                log_failure(logging.getLogger("video_filter.runtime"), "worker timeout", error)
            else:
                self.fail("Timed-out worker must fail.")
        tree.return_value.terminate.assert_called_once()
        self.assertEqual(2, process.wait.call_count)
        self.assertIn("TimeoutExpired", self.contents("video_filter/logs/error.log"))
        self.assertIn("extraction_timeout", self.contents("video_filter/logs/error.log"))

    def test_runtime_task_failure_is_error_with_traceback_and_terminal_state(self):
        from video_filter.tasks import enqueue_task
        from video_filter.runtime import process_round

        _, _, config = self.identities()
        task = enqueue_task(db.session, "scan", config.id, {"version": 1})
        db.session.commit()
        settings = {"enabled": True, "state_directory": self.root / "state", "directories": {}, "task_timeout_seconds": 30}
        with patch("video_filter.tracking.reconcile"), patch("video_filter.runtime.execute", side_effect=RuntimeError("task-fault-marker")):
            process_round(db.session, settings)
        contents = self.contents("video_filter/logs/error.log")
        self.assertIn("task-fault-marker", contents)
        self.assertIn("Traceback", contents)
        self.assertIn(task.id, contents)
        self.assertEqual("failed", task.status)
