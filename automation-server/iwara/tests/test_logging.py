import importlib
import logging
import unittest
from contextlib import nullcontext
from unittest.mock import patch


log_api = importlib.import_module("iwara.apis.log")
schedule_module = importlib.import_module("iwara.schedules.add_download")


class BrowserLogTests(unittest.TestCase):
    def test_browser_message_is_bounded_and_cannot_forge_a_new_line(self):
        message = "first\nsecond\r" + "x" * 5000
        with log_api.app.test_request_context(
            "/iwara/log",
            method="POST",
            json={"log": "ERROR", "info": message},
        ), patch.object(log_api.logger, "log") as write_log:
            response = log_api.log()

        self.assertEqual(200, response[1])
        write_log.assert_called_once()
        self.assertEqual(logging.ERROR, write_log.call_args.args[0])
        sanitized = write_log.call_args.args[2]
        self.assertNotIn("\n", sanitized)
        self.assertNotIn("\r", sanitized)
        self.assertIn("\\n", sanitized)
        self.assertLessEqual(len(sanitized), 4000)

    def test_browser_log_rejects_non_string_messages(self):
        with log_api.app.test_request_context(
            "/iwara/log",
            method="POST",
            json={"log": "info", "info": {"unexpected": "object"}},
        ), patch.object(log_api.logger, "log") as write_log:
            response = log_api.log()

        self.assertEqual(400, response[1])
        write_log.assert_not_called()


class SchedulerBoundaryTests(unittest.TestCase):
    def test_scheduler_logs_rolls_back_and_consumes_unhandled_error(self):
        error = RuntimeError("iwara-scheduler-marker")
        with patch.object(
            schedule_module.app,
            "app_context",
            return_value=nullcontext(),
        ), patch.object(
            schedule_module,
            "_do_add_download",
            side_effect=error,
        ), patch.object(
            schedule_module.db.session,
            "rollback",
        ) as rollback, patch.object(
            schedule_module,
            "log_exception",
        ) as log_exception:
            result = schedule_module.do_add_download()

        self.assertIsNone(result)
        rollback.assert_called_once_with()
        self.assertIs(error, log_exception.call_args_list[0].args[2])


if __name__ == "__main__":
    unittest.main()
