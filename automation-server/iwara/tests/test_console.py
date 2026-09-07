import io
import importlib
import unittest
from unittest.mock import patch

from app import app
from iwara.console import safe_print


class SafePrintTests(unittest.TestCase):
    def test_unencodable_unicode_is_escaped_for_gbk_stream(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")

        safe_print("标题: ❤ ♠ •", file=stream, flush=True)

        output = buffer.getvalue().decode("gbk")
        self.assertEqual(["标题: \\u2764 \\u2660 \\u2022"], output.splitlines())

    def test_valid_unicode_is_preserved_for_utf8_stream(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")

        safe_print("标题: ❤ ♠ •", file=stream, flush=True)

        output = buffer.getvalue().decode("utf-8")
        self.assertEqual(["标题: ❤ ♠ •"], output.splitlines())

    def test_closed_stream_does_not_raise(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        stream.close()

        safe_print("标题: ♠", file=stream)

    def test_log_endpoint_accepts_characters_outside_gbk(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")

        with patch("iwara.console.sys.stdout", stream):
            response = app.test_client().post(
                "/iwara/log",
                json={"log": "INFO", "info": "标题: ❤ ♠ •"},
            )

        self.assertEqual(200, response.status_code)

    def test_prepare_download_commits_before_safe_output(self):
        prepare_module = importlib.import_module("iwara.apis.prepare_download")

        class PageUrl:
            def __eq__(self, other):
                return True

        class Query:
            def filter(self, condition):
                return self

            def first(self):
                return None

        class Mission:
            page_url = PageUrl()
            query = Query()

            def __init__(self, **data):
                self.title = data["title"]

        class Session:
            def __init__(self):
                self.committed = False

            def add(self, mission):
                return None

            def commit(self):
                self.committed = True

        class Database:
            session = Session()

        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        with (
            patch.object(prepare_module, "DownloadMission", Mission),
            patch.object(prepare_module, "db", Database),
            patch("iwara.console.sys.stdout", stream),
        ):
            response = app.test_client().post(
                "/iwara/prepare_download",
                json={
                    "page_url": "https://example.invalid/video/1",
                    "user_name": "示例用户",
                    "title": "标题 ♠",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(Database.session.committed)


if __name__ == "__main__":
    unittest.main()
