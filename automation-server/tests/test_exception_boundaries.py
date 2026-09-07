import io
import logging
from pathlib import Path
import tempfile
import unittest

try:
    from flask import Flask, abort
except ImportError:
    Flask = None

import logging_config


@unittest.skipIf(Flask is None, "Flask is not installed in this test environment")
class FlaskExceptionBoundaryTests(unittest.TestCase):
    def setUp(self):
        logging_config._reset_logging_for_tests()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        logging_config.configure_logging(
            base_directory=self.base,
            stream=io.StringIO(),
        )
        self.app = Flask(__name__)
        self.app.testing = False
        logging_config.install_flask_exception_handler(self.app)

    def tearDown(self):
        logging_config._reset_logging_for_tests()
        self.temporary_directory.cleanup()

    def read(self, relative_path):
        state = logging.getLogger()._autoflow_logging_state
        for handler in state["handlers"]:
            handler.flush()
        return (self.base / relative_path).read_text(encoding="utf-8")

    def test_child_view_exception_returns_generic_500_and_stays_in_child_logs(self):
        marker = "iwara-http-exception-marker"

        def failing_view():
            raise RuntimeError(marker)

        failing_view.__module__ = "iwara.apis.testing"
        self.app.add_url_rule("/failure", "failure", failing_view)
        response = self.app.test_client().get("/failure")

        self.assertEqual(500, response.status_code)
        self.assertNotIn(marker.encode(), response.data)
        self.assertIn(marker, self.read("iwara/logs/error.log"))
        self.assertNotIn(marker, self.read("logs/runtime.log"))
        self.assertNotIn(marker, self.read("logs/error.log"))

    def test_http_exceptions_keep_status_and_are_not_logged_as_program_errors(self):
        def bad_request_view():
            abort(400)

        bad_request_view.__module__ = "iwara.apis.testing"
        self.app.add_url_rule("/bad-request", "bad_request", bad_request_view)
        client = self.app.test_client()

        self.assertEqual(400, client.get("/bad-request").status_code)
        self.assertEqual(404, client.get("/missing").status_code)
        self.assertEqual("", self.read("iwara/logs/error.log"))


if __name__ == "__main__":
    unittest.main()
