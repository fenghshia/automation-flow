from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from video_filter.tests.support import DatabaseTestCase, app, db, bundle_arguments, signature
from video_filter import register_video_filter
from video_filter.tasks import enqueue_task
from video_filter.runtime import enqueue_automatic, process_round, register_schedules
from video_filter.models import Asset, Location, Task, Variant
from video_filter.configuration import record_configuration
from video_filter.feature_store import FeatureStore
from env import EnvConfig


class RuntimeTests(DatabaseTestCase):
    def test_automatic_extraction_candidates_are_unique_bounded_and_postgresql_compatible(self):
        settings = {"enabled": True, "model_manifest": Path("example-manifest.json"),
                    "ffmpeg_directory": Path("example-tools")}
        config = record_configuration(db.session, settings)
        config.status, config.active_slot = "active", "active"
        asset = Asset(label=1)
        db.session.add(asset)
        db.session.flush()
        variants = []
        for index in range(25):
            variant = Variant(asset_id=asset.id, sha256=f"{index + 1:064x}", size_bytes=10,
                              created_at=datetime(2020, 1, 1) + timedelta(seconds=index))
            db.session.add(variant)
            db.session.flush()
            variants.append(variant)
            if index == 2:  # No managed location.
                continue
            for copy in range(2):
                db.session.add(Location(variant_id=variant.id, role="unclassified",
                    path=f"example-{index}-{copy}.mp4", current_path_key=f"{100 + index * 2 + copy:064x}",
                    size_bytes=10, modified_ns=1, status="missing" if index == 1 else "present"))
        db.session.commit()
        arguments = bundle_arguments(asset.id, variants[0].id)
        arguments["source_sha256"] = variants[0].sha256
        FeatureStore().save(db.session, FeatureStore().prepare(**arguments))

        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="succeeded")) as submit, \
                patch.object(db.session, "execute", wraps=db.session.execute) as queries:
            enqueue_automatic(db.session, settings)
        extracted = [call.args[3] for call in submit.call_args_list if call.args[2] == "extract"]
        self.assertEqual([variant.id for variant in variants[3:23]], extracted)
        self.assertEqual(20, len(set(extracted)))
        candidate = next(call.args[0] for call in queries.call_args_list
            if isinstance(call.args[0], Select) and "id" in call.args[0].selected_columns
            and call.args[0].selected_columns["id"].table.name == Variant.__tablename__)
        # SQLite accepts the original invalid query; explicitly enforce the
        # PostgreSQL DISTINCT/ORDER BY rule on the actual production statement.
        selected = set(candidate.selected_columns.keys())
        self.assertTrue(all(column.key in selected for column in candidate._order_by_clauses))
        self.assertIn("SELECT DISTINCT", str(candidate.compile(dialect=postgresql.dialect())))

    def test_routes_enqueue_without_executing_worker_and_reject_arbitrary_paths(self):
        register_video_filter()
        _, _, config = self.identities()
        task = enqueue_task(db.session, "scan", config.id, {"version": 1})
        db.session.commit()
        settings = {"enabled": True}
        with patch.object(EnvConfig, "video_filter_settings", return_value=settings), patch("video_filter.runtime.enqueue", return_value=task) as submit:
            response = app.test_client().post("/video_filter/scan", json={})
            self.assertEqual(202, response.status_code)
            submit.assert_called_once()
            response = app.test_client().post("/video_filter/classify", json={"path": "arbitrary.mp4"})
            self.assertEqual(400, response.status_code)
        status = app.test_client().get("/video_filter/tasks/" + task.id)
        self.assertNotIn("input_snapshot", status.json)

    def test_feedback_rejects_boolean_label_before_persisting_journal(self):
        register_video_filter()
        asset, _, _ = self.identities()
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            settings = {"enabled": True, "state_directory": Path(root)}
            with patch.object(EnvConfig, "video_filter_settings", return_value=settings):
                response = app.test_client().post("/video_filter/feedback", json={
                    "asset_id": asset.id, "label": True, "expected_revision": 0, "event_key": "a" * 64})
            self.assertEqual(409, response.status_code)
            self.assertFalse((Path(root) / "feedback").exists())

    def test_registration_does_not_start_scheduler_and_is_idempotent(self):
        from app import scheduler

        register_schedules(scheduler, app)
        register_schedules(scheduler, app)
        jobs = [job for job in scheduler.get_jobs() if job.id == "video_filter_process_one"]
        self.assertEqual(1, len(jobs))
        self.assertFalse(scheduler.running)
        scheduler.remove_job("video_filter_process_one")

    def test_disabled_round_does_not_touch_database(self):
        with patch.object(db.session, "execute", side_effect=AssertionError("DB accessed")):
            self.assertIsNone(process_round(db.session, {"enabled": False}))
