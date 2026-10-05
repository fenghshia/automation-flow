import os
from pathlib import Path
from unittest.mock import patch

from env import EnvConfig
from video_filter import register_video_filter
from video_filter.tests.support import DatabaseTestCase
from app import app, db
from video_filter.models import Task


class ControlTests(DatabaseTestCase):
    def test_registration_is_idempotent_and_disabled_status_avoids_db(self):
        register_video_filter()
        register_video_filter()
        rules = [rule for rule in app.url_map.iter_rules() if rule.rule == "/video_filter/status"]
        self.assertEqual(1, len(rules))
        with patch.object(EnvConfig, "_loaded", True), patch.dict(os.environ, {}, clear=True), patch.object(db.session, "execute", side_effect=AssertionError("DB accessed")):
            response = app.test_client().get("/video_filter/status")
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json["enabled"])

    def test_invalid_config_returns_safe_error_without_paths(self):
        register_video_filter()
        with patch.object(EnvConfig, "_loaded", True), patch.dict(os.environ, {"VIDEO_FILTER_ENABLED": "bad"}, clear=True):
            response = app.test_client().get("/video_filter/status")
        self.assertEqual(503, response.status_code)
        self.assertEqual("invalid_configuration", response.json["error_code"])

    def test_missing_schema_is_reported_without_raw_db_error(self):
        register_video_filter()
        Task.__table__.drop(db.engine)
        settings = {"enabled": True, "directories": {"confirmed_like": Path("nonexistent-test-root")}}
        with patch.object(EnvConfig, "video_filter_settings", return_value=settings):
            response = app.test_client().get("/video_filter/status")
        self.assertEqual(503, response.status_code)
        self.assertEqual("schema_unavailable", response.json["error_code"])

    def test_status_counts_do_not_expose_paths(self):
        register_video_filter()
        self.identities()
        settings = {"enabled": True, "directories": {"confirmed_like": Path("private-test-path")}}
        with patch.object(EnvConfig, "video_filter_settings", return_value=settings):
            response = app.test_client().get("/video_filter/status")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json["counts"]["positive_assets"])
        self.assertEqual("database", response.json["summary_storage"])
        self.assertNotIn("private-test-path", response.get_data(as_text=True))
