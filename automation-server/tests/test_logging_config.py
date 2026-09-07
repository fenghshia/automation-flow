import io
import logging
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import logging_config


class PipelineError(RuntimeError):
    pass


class BrokenStream:
    def seek(self, *args):
        return 0

    def tell(self):
        return 0

    def write(self, value):
        raise OSError("simulated log sink failure")

    def flush(self):
        return None


def _raise_original(marker):
    raise ValueError(marker)


def _wrap_original(marker):
    try:
        _raise_original(marker)
    except ValueError as error:
        raise PipelineError("outer-pipeline-error") from error


class LoggingConfigurationTests(unittest.TestCase):
    def setUp(self):
        logging_config._reset_logging_for_tests()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.console = io.StringIO()
        logging_config.configure_logging(
            base_directory=self.base,
            stream=self.console,
        )

    def tearDown(self):
        logging_config._reset_logging_for_tests()
        self.temporary_directory.cleanup()

    def flush(self):
        state = logging.getLogger()._autoflow_logging_state
        for handler in state["handlers"]:
            handler.flush()

    def read(self, relative_path):
        self.flush()
        return (self.base / relative_path).read_text(encoding="utf-8")

    def test_records_are_routed_and_framework_files_reject_projects(self):
        image_marker = "image-route-marker"
        video_marker = "video-route-marker"
        framework_marker = "framework-route-marker"
        framework_error_marker = "framework-error-marker"
        third_party_marker = "third-party-error-marker"

        logging.getLogger("image_compression.worker").info(image_marker)
        try:
            raise RuntimeError(video_marker)
        except RuntimeError as error:
            logging_config.log_exception(
                logging.getLogger("video_compression.worker"),
                "video failed",
                error,
            )
        logging.getLogger("apscheduler.scheduler").warning(framework_marker)
        logging.getLogger("framework.http").error(framework_error_marker)
        logging.getLogger("third_party.worker").error(third_party_marker)

        image_runtime = self.read("image_compression/logs/runtime.log")
        video_runtime = self.read("video_compression/logs/runtime.log")
        video_error = self.read("video_compression/logs/error.log")
        framework_runtime = self.read("logs/runtime.log")
        framework_error = self.read("logs/error.log")

        self.assertIn(image_marker, image_runtime)
        self.assertNotIn(image_marker, framework_runtime + framework_error)
        self.assertIn(video_marker, video_runtime)
        self.assertIn(video_marker, video_error)
        self.assertNotIn(video_marker, framework_runtime + framework_error)
        self.assertIn(framework_marker, framework_runtime)
        self.assertIn(framework_error_marker, framework_runtime)
        self.assertIn(framework_error_marker, framework_error)
        self.assertIn(third_party_marker, framework_runtime)
        self.assertIn(third_party_marker, framework_error)
        self.assertNotIn(framework_marker, image_runtime + video_runtime)
        self.assertNotIn(third_party_marker, image_runtime + video_runtime)

    def test_framework_filter_still_isolates_accidentally_propagated_project(self):
        marker = "propagation-isolation-marker"
        project_logger = logging.getLogger("iwara")
        project_logger.propagate = True
        logging.getLogger("iwara.worker").error(marker)

        self.assertIn(marker, self.read("iwara/logs/error.log"))
        self.assertNotIn(marker, self.read("logs/runtime.log"))
        self.assertNotIn(marker, self.read("logs/error.log"))

    def test_complete_chained_traceback_is_written_to_runtime_and_error(self):
        marker = "complete-traceback-marker"
        try:
            _wrap_original(marker)
        except PipelineError as error:
            logging_config.log_exception(
                logging.getLogger("video_compression.pipeline"),
                "pipeline boundary",
                error,
            )

        runtime = self.read("video_compression/logs/runtime.log")
        error_log = self.read("video_compression/logs/error.log")
        for expected in (
            marker,
            "_raise_original",
            "_wrap_original",
            "ValueError",
            "PipelineError",
            "direct cause",
        ):
            self.assertIn(expected, runtime)
            self.assertIn(expected, error_log)
        self.assertNotIn(marker, self.read("logs/error.log"))

    def test_configuration_is_idempotent_and_does_not_duplicate_records(self):
        marker = "idempotence-marker"
        before = len(logging.getLogger("image_compression").handlers)
        logging_config.configure_logging(base_directory=self.base, stream=self.console)
        after = len(logging.getLogger("image_compression").handlers)
        logging.getLogger("image_compression.worker").info(marker)

        self.assertEqual(before, after)
        self.assertEqual(1, self.read("image_compression/logs/runtime.log").count(marker))

    def test_rotation_keeps_bounded_backup_files(self):
        logging_config._reset_logging_for_tests()
        logging_config.configure_logging(
            base_directory=self.base,
            max_bytes=300,
            backup_count=2,
            stream=self.console,
        )
        logger = logging.getLogger("image_compression.rotation")
        for number in range(30):
            logger.info("rotation-%02d-%s", number, "x" * 80)
        self.flush()

        files = sorted((self.base / "image_compression/logs").glob("runtime.log*"))
        self.assertGreater(len(files), 1)
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size > 0 for path in files))

    def test_known_secrets_and_secret_shapes_are_redacted(self):
        secret = "runtime-secret-998877"
        logging_config.register_redaction_values([secret])
        logging.getLogger("jd_auto_match.worker").error(
            "value=%s password=plain-secret Bearer abc.def http://user:pass@example.test",
            secret,
        )
        contents = self.read("jd_auto_match/logs/error.log")

        for raw_value in (secret, "plain-secret", "abc.def", "user:pass"):
            self.assertNotIn(raw_value, contents)
        self.assertIn("<redacted>", contents)

    def test_one_file_sink_failure_does_not_break_business_logging(self):
        marker = "resilient-handler-marker"
        state = logging.getLogger()._autoflow_logging_state
        error_handler = next(
            handler
            for handler in state["handlers"]
            if getattr(handler, "_autoflow_target", "")
            == "image_compression/error"
        )
        original_stream = error_handler.stream
        fallback = io.StringIO()
        error_handler.stream = BrokenStream()
        try:
            with patch.object(sys, "__stderr__", fallback):
                logging.getLogger("image_compression.worker").error(marker)
        finally:
            error_handler.stream = original_stream

        self.assertIn(marker, self.read("image_compression/logs/runtime.log"))
        self.assertIn("logging write failed", fallback.getvalue())

    def test_traceback_path_resolves_to_innermost_project(self):
        namespace = {}
        filename = logging_config.SERVER_DIRECTORY / "image_compression" / "fake.py"
        exec(compile("def crash():\n    raise RuntimeError('path-marker')\n", filename, "exec"), namespace)
        try:
            namespace["crash"]()
        except RuntimeError as error:
            project = logging_config.project_from_traceback(error.__traceback__)
        self.assertEqual("image_compression", project)

    def test_uncaught_hooks_route_main_and_background_exceptions(self):
        logging_config.install_uncaught_exception_hooks()
        image_namespace = {}
        image_filename = logging_config.SERVER_DIRECTORY / "image_compression" / "hook.py"
        exec(
            compile(
                "def crash():\n    raise RuntimeError('main-hook-marker')\n",
                image_filename,
                "exec",
            ),
            image_namespace,
        )
        try:
            image_namespace["crash"]()
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)

        thread_namespace = {}
        thread_filename = logging_config.SERVER_DIRECTORY / "jd_auto_match" / "hook.py"
        exec(
            compile(
                "def crash():\n    raise RuntimeError('thread-hook-marker')\n",
                thread_filename,
                "exec",
            ),
            thread_namespace,
        )
        thread = threading.Thread(target=thread_namespace["crash"], name="hook-test")
        thread.start()
        thread.join()

        self.assertIn("main-hook-marker", self.read("image_compression/logs/error.log"))
        self.assertIn("thread-hook-marker", self.read("jd_auto_match/logs/error.log"))
        framework_logs = self.read("logs/runtime.log") + self.read("logs/error.log")
        self.assertNotIn("main-hook-marker", framework_logs)
        self.assertNotIn("thread-hook-marker", framework_logs)

    def test_initialization_failure_cleans_up_and_aborts(self):
        logging_config._reset_logging_for_tests()
        invalid_base = self.base / "not-a-directory"
        invalid_base.write_text("file", encoding="utf-8")
        fallback = io.StringIO()

        with patch.object(sys, "__stderr__", fallback):
            with self.assertRaises(OSError):
                logging_config.configure_logging(base_directory=invalid_base)

        self.assertIn("startup aborted", fallback.getvalue())
        self.assertFalse(
            getattr(logging.getLogger(), "_autoflow_logging_configured", False)
        )
        for logger_name in ("", *logging_config.PROJECT_NAMES):
            self.assertFalse(
                any(
                    getattr(handler, "_autoflow_managed", False)
                    for handler in logging.getLogger(logger_name).handlers
                )
            )

    def test_partial_initialization_failure_closes_created_file_handlers(self):
        logging_config._reset_logging_for_tests()
        created = []
        original_factory = logging_config._make_file_handler

        def failing_factory(*args, **kwargs):
            if len(created) == 2:
                raise OSError("simulated partial initialization failure")
            handler = original_factory(*args, **kwargs)
            created.append(handler)
            return handler

        fallback = io.StringIO()
        with patch.object(
            logging_config,
            "_make_file_handler",
            side_effect=failing_factory,
        ), patch.object(sys, "__stderr__", fallback):
            with self.assertRaises(OSError):
                logging_config.configure_logging(base_directory=self.base)

        self.assertEqual(2, len(created))
        self.assertTrue(all(handler.stream is None for handler in created))
        self.assertIn("startup aborted", fallback.getvalue())
        self.assertFalse(
            getattr(logging.getLogger(), "_autoflow_logging_configured", False)
        )


if __name__ == "__main__":
    unittest.main()
