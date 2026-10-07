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
from video_filter.models import Asset, Location, ModelRun, ScanRun, Task, Variant
from video_filter.models.records import utc_now
from video_filter.configuration import record_configuration
from video_filter.feature_store import FeatureStore
from env import EnvConfig


class RuntimeTests(DatabaseTestCase):
    def extraction_fixture(self):
        settings = {"enabled": True, "model_manifest": Path("example-manifest.json"),
            "ffmpeg_directory": Path("example-tools"), "state_directory": Path("example-state"),
            "device": "cpu", "task_timeout_seconds": 30}
        config = record_configuration(db.session, settings)
        asset = Asset()
        db.session.add(asset)
        db.session.flush()
        variant = Variant(asset_id=asset.id, sha256="b" * 64, size_bytes=10)
        db.session.add(variant)
        db.session.commit()
        arguments = bundle_arguments(asset.id, variant.id)
        task = enqueue_task(db.session, "extract", config.id, {
            "feature_signature": signature().digest, "path": "synthetic.mp4",
            "source_snapshot": arguments["source_snapshot"]}, asset.id, variant.id)
        db.session.commit()
        from video_filter.tasks import claim_task
        task = claim_task(db.session, task.id)
        arguments["task_id"] = task.id
        return settings, task, FeatureStore().prepare(**arguments)

    def test_extraction_returns_connection_during_hashes_and_worker_wait(self):
        from video_filter.runtime import execute
        settings, task, prepared = self.extraction_fixture()
        identifier, variant_id = task.id, task.variant_id
        def check_connection():
            self.assertFalse(db.session().in_transaction())
            self.assertEqual(0, db.engine.pool.checkedout())
        def hash_source(*args):
            check_connection()
            return "b" * 64, {}
        def worker(request, timeout):
            check_connection()
            self.assertEqual(identifier, request["task_id"])
            self.assertEqual(variant_id, request["variant_id"])
            # The GPU controller must be able to borrow a connection while
            # extraction is waiting, including when there is only one free slot.
            with db.engine.connect() as connection:
                self.assertFalse(connection.closed)
            return prepared, {"media_metadata": {"duration": 12}, "measurements": {"fixture": True}}
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.hash_stable", side_effect=hash_source) as hashes, \
                patch("video_filter.worker_client.run_extraction", side_effect=worker):
            stored = execute(db.session, settings, task)
        self.assertEqual(2, hashes.call_count)
        self.assertEqual(identifier, stored.manifest["task_id"])
        self.assertEqual({"duration": 12, "extraction_measurements": {"fixture": True}},
            db.session.get(Variant, variant_id).media_metadata)
        FeatureStore().require_ready(db.session, stored.bundle_id)

    def test_changed_source_after_worker_does_not_publish_summary(self):
        from video_filter.runtime import execute
        from video_filter.models import FeatureBundle
        settings, task, prepared = self.extraction_fixture()
        variant_id = task.variant_id
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.hash_stable", side_effect=[("b" * 64, {}), ("c" * 64, {})]), \
                patch("video_filter.worker_client.run_extraction", return_value=(prepared,
                    {"media_metadata": {"duration": 12}, "measurements": {}})):
            with self.assertRaisesRegex(ValueError, "source_version_changed"):
                execute(db.session, settings, task)
        self.assertEqual(0, db.session.query(FeatureBundle).count())
        self.assertFalse(db.session.get(Variant, variant_id).media_metadata)

    def test_variant_changed_while_worker_runs_does_not_publish_summary(self):
        from sqlalchemy import update
        from video_filter.runtime import execute
        from video_filter.models import FeatureBundle
        settings, task, prepared = self.extraction_fixture()
        variant_id = task.variant_id
        def worker(*args):
            with db.engine.begin() as connection:
                connection.execute(update(Variant.__table__).where(Variant.id == variant_id)
                    .values(sha256="c" * 64))
            return prepared, {"media_metadata": {"duration": 12}, "measurements": {}}
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.hash_stable", return_value=("b" * 64, {})), \
                patch("video_filter.worker_client.run_extraction", side_effect=worker):
            with self.assertRaisesRegex(ValueError, "source_version_changed"):
                execute(db.session, settings, task)
        self.assertEqual(0, db.session.query(FeatureBundle).count())
        self.assertFalse(db.session.get(Variant, variant_id).media_metadata)

    def test_grouped_discovery_passes_failed_front_candidates_and_bounds_queue(self):
        from sqlalchemy import select, func
        from video_filter.runtime import _enqueue_extraction
        settings = {"enabled": True, "grouped": True, "name": "isolated_fixture", "directories": {}, "extract_concurrency": 6}
        config = record_configuration(db.session, settings)
        config.status, config.active_slot = "active", "active"
        asset = Asset(label=1)
        db.session.add(asset)
        db.session.flush()
        variants = []
        for index in range(40):
            variant = Variant(asset_id=asset.id, sha256=f"{index + 1:064x}", size_bytes=10,
                created_at=datetime(2020, 1, 1) + timedelta(seconds=index))
            db.session.add(variant)
            db.session.flush()
            db.session.add(Location(variant_id=variant.id, role="unclassified", path=f"synthetic-{index}.mp4",
                current_path_key=f"{index + 100:064x}", size_bytes=10, modified_ns=1, status="present"))
            variants.append(variant.id)
        db.session.commit()
        failed_ids = set(variants[:21])
        inputs = {"feature_signature": signature().digest, "label_revision": asset.label_revision}
        for identifier in variants[:22]:
            task = enqueue_task(db.session, "extract", config.id, inputs, asset.id, identifier)
            if identifier in failed_ids:
                task.status, task.error_code = "failed", "permanent_fixture_failure"
        enqueue_task(db.session, "predict", config.id, {"fixture": "resource waiting"})
        db.session.commit()
        def enqueue_fixture(session, config_settings, kind, identifier):
            task = enqueue_task(session, kind, config.id, inputs, asset.id, identifier)
            session.commit()
            return task
        with patch("video_filter.runtime.enqueue", side_effect=enqueue_fixture) as submit:
            _enqueue_extraction(db.session, settings, signature())
            self.assertIn(variants[32], [call.args[3] for call in submit.call_args_list])
            before = submit.call_count
            _enqueue_extraction(db.session, settings, signature())
            self.assertEqual(before, submit.call_count)
        self.assertEqual(12, db.session.scalar(select(func.count()).select_from(Task).where(Task.kind == "extract", Task.status == "queued")))

    def training_policy_fixture(self):
        from uuid import UUID
        settings = {"enabled": True, "model_manifest": Path("example-manifest.json"),
                    "ffmpeg_directory": Path("example-tools")}
        config = record_configuration(db.session, settings)
        config.status, config.active_slot = "active", "active"
        db.session.add(ScanRun(config_revision_id=config.id, complete=True))
        db.session.commit()
        data = [{"asset_id": str(UUID(int=index + 1)), "label": index % 2, "label_revision": 1,
                 "bundle_id": str(UUID(int=index + 1001)), "manifest_sha256": "a" * 64} for index in range(120)]
        return settings, data

    def completed_model(self, settings, kind, data, status="validated", created_at=None):
        from video_filter.training_config import digest
        run = ModelRun(model_type=kind, feature_signature=signature().digest, dataset_snapshot=data,
            status=status, training_config_digest=digest(settings, kind))
        if created_at is not None:
            run.created_at = created_at
        db.session.add(run)
        db.session.commit()
        return run

    def test_new_samples_never_trigger_automatic_training(self):
        settings, data = self.training_policy_fixture()
        for kind in ("logistic_regression", "mil"):
            self.completed_model(settings, kind, data[:20])
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=data[:119]), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, settings)
            submit.assert_not_called()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=data), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, settings)
            submit.assert_not_called()

    def test_first_model_requires_manual_training_even_with_enough_samples(self):
        settings, data = self.training_policy_fixture()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=data[:19]) as snapshot, \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, settings)
            submit.assert_not_called()
            snapshot.return_value = data[:20]
            enqueue_automatic(db.session, settings)
            submit.assert_not_called()

    def test_revision_or_bundle_changes_do_not_trigger_training(self):
        settings, data = self.training_policy_fixture()
        for kind in ("logistic_regression", "mil"):
            self.completed_model(settings, kind, data[:20], status="retired")
        changed = [dict(row) for row in data[:20]]
        for index in (0, 1):
            changed[index].update(label_revision=2,
                bundle_id=data[20 + index]["bundle_id"], manifest_sha256="b" * 64)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=changed), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, settings)
            submit.assert_not_called()

    def test_label_changes_never_trigger_automatic_training(self):
        settings, data = self.training_policy_fixture()
        for kind in ("logistic_regression", "mil"):
            self.completed_model(settings, kind, data[:20])
        changed = [dict(row) for row in data[:20]]
        for index in (0, 1):
            changed[index].update(label=1 - changed[index]["label"], label_revision=2)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=changed), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, settings)
            submit.assert_not_called()

    def test_manual_training_can_run_before_new_sample_threshold(self):
        from video_filter.runtime import enqueue
        settings, data = self.training_policy_fixture()
        self.completed_model(settings, "logistic_regression", data[:20])
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=data[:21]):
            task = enqueue(db.session, settings, "train", model_type="logistic_regression")
        self.assertEqual("queued", task.status)
        self.assertEqual("train", task.kind)

    def test_manual_training_freezes_acceptance_and_can_repeat_completed_data(self):
        from video_filter.runtime import enqueue, execute
        from video_filter.training_config import digest
        settings, data = self.training_policy_fixture()
        gates = {"like_precision": .7, "dislike_precision": .9}
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=data), \
                patch("video_filter.learning.train") as fit:
            task = enqueue(db.session, settings, "train", model_type="logistic_regression", acceptance=gates)
            self.assertEqual(gates, task.input_snapshot["acceptance"])
            self.assertEqual(digest(settings, "logistic_regression", acceptance=gates), task.input_snapshot["training_config_digest"])
            self.assertEqual(task.id, enqueue(db.session, settings, "train", model_type="logistic_regression", acceptance=gates).id)
            with self.assertRaisesRegex(ValueError, "training_pending_with_different_acceptance"):
                enqueue(db.session, settings, "train", model_type="logistic_regression", acceptance={"like_precision": .9, "dislike_precision": .7})
            execute(db.session, settings, task)
            self.assertEqual(gates, fit.call_args.kwargs["acceptance"])
            task.status = "succeeded"
            db.session.commit()
            next_task = enqueue(db.session, settings, "train", model_type="logistic_regression", acceptance=gates)
            self.assertNotEqual(task.id, next_task.id)
            self.assertEqual("queued", next_task.status)

    def test_manual_training_still_requires_ten_samples_in_each_class(self):
        from video_filter.runtime import enqueue
        settings, data = self.training_policy_fixture()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.learning.training_snapshot", return_value=data[:19]):
            with self.assertRaisesRegex(ValueError, "insufficient_confirmed_samples"):
                enqueue(db.session, settings, "train", model_type="mil")
        self.assertEqual(0, db.session.query(Task).filter_by(kind="train").count())

    def test_automatic_extraction_candidates_are_unique_bounded_and_postgresql_compatible(self):
        settings = {"enabled": True, "model_manifest": Path("example-manifest.json"),
                    "ffmpeg_directory": Path("example-tools")}
        config = record_configuration(db.session, settings)
        config.status, config.active_slot = "active", "active"
        db.session.add(ScanRun(config_revision_id=config.id, complete=True))
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

    def test_manual_lineage_wait_preserves_conflict_response_without_error_stack(self):
        register_video_filter()
        with patch.object(EnvConfig, "video_filter_settings", return_value={"enabled": True}), \
                patch("video_filter.runtime.enqueue", side_effect=ValueError("lineage_reconciliation_pending")), \
                patch("video_filter.apis.control.log_failure") as failures:
            response = app.test_client().post("/video_filter/train", json={})
        self.assertEqual(409, response.status_code)
        self.assertEqual("lineage_reconciliation_pending", response.json["error_code"])
        failures.assert_not_called()

    def test_lineage_arriving_during_extraction_discovery_defers_remaining_candidates(self):
        from media_lineage.models import LineageEvent
        from uuid import uuid4
        settings = {"enabled": True, "model_manifest": Path("example-manifest.json"),
                    "ffmpeg_directory": Path("example-tools")}
        config = record_configuration(db.session, settings)
        config.status, config.active_slot = "active", "active"
        db.session.add(ScanRun(config_revision_id=config.id, complete=True))
        asset = Asset()
        db.session.add(asset)
        db.session.flush()
        for index in range(2):
            variant = Variant(asset_id=asset.id, sha256=f"{index + 1:064x}", size_bytes=10)
            db.session.add(variant)
            db.session.flush()
            db.session.add(Location(variant_id=variant.id, role="unclassified", path=f"example-{index}.mp4",
                size_bytes=10, modified_ns=1, status="present"))
        db.session.commit()
        def new_event(*args):
            db.session.add(LineageEvent(operation_id=str(uuid4()), sequence=0, producer="video_filter",
                generation=str(uuid4()), phase="planned", source_sha256="a" * 64,
                source_path="example-source.mp4", destination_path="example-target.mp4", evidence={}))
            db.session.commit()
            raise ValueError("lineage_reconciliation_pending")
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.enqueue", side_effect=new_event) as submit, \
                patch("video_filter.runtime.log_exception") as failures:
            with self.assertLogs("video_filter.runtime", level="INFO") as logs:
                enqueue_automatic(db.session, settings)
        submit.assert_called_once()
        failures.assert_not_called()
        self.assertTrue(any("lineage_reconciliation_pending" in item for item in logs.output))

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
