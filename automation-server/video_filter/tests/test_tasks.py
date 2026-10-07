from sqlalchemy import func, select

from video_filter.tests.support import DatabaseTestCase
from app import db
from video_filter.models import Task
from video_filter.tasks import enqueue_task


class TaskTests(DatabaseTestCase):
    def test_duplicate_task_returns_same_record(self):
        asset, variant, config = self.identities()
        arguments = dict(kind="extract", config_revision_id=config.id, asset_id=asset.id,
                         variant_id=variant.id, input_snapshot={"source_sha256": variant.sha256})
        first = enqueue_task(db.session, **arguments)
        db.session.commit()
        second = enqueue_task(db.session, **arguments)
        db.session.commit()
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Task)))

    def test_changed_label_revision_produces_new_task(self):
        asset, variant, config = self.identities()
        first = enqueue_task(db.session, "classify", config.id, {"label_revision": 0}, asset.id, variant.id)
        second = enqueue_task(db.session, "classify", config.id, {"label_revision": 1}, asset.id, variant.id)
        db.session.commit()
        self.assertNotEqual(first.id, second.id)

    def test_transient_failure_with_identical_inputs_requeues_but_stops_after_three_attempts(self):
        _, _, config = self.identities()
        for code in ("OperationalError", "database_deadlock", "database_serialization_failure"):
            with self.subTest(code=code):
                task = enqueue_task(db.session, "predict", config.id, {"fixture": code})
                db.session.commit()
                task.status, task.error_code, task.attempts = "failed", code, 1
                db.session.commit()
                recovered = enqueue_task(db.session, "predict", config.id, {"fixture": code})
                db.session.commit()
                self.assertEqual(task.id, recovered.id)
                self.assertEqual(("queued", 1, None), (recovered.status, recovered.attempts, recovered.error_code))
                recovered.status, recovered.error_code, recovered.attempts = "failed", code, 3
                db.session.commit()
                repeated = enqueue_task(db.session, "predict", config.id, {"fixture": code})
                self.assertEqual("failed", repeated.status)

    def test_failed_worker_or_unverified_transfer_is_not_automatically_requeued(self):
        _, _, config = self.identities()
        for kind, code in (("extract", "extraction_worker_failed"), ("classify", "OperationalError")):
            task = enqueue_task(db.session, kind, config.id, {"fixture": kind})
            db.session.commit()
            task.status, task.error_code, task.attempts = "failed", code, 1
            db.session.commit()
            self.assertEqual("failed", enqueue_task(db.session, kind, config.id, {"fixture": kind}).status)

    def test_invalid_kind_or_unversioned_input_rejected(self):
        asset, variant, config = self.identities()
        with self.assertRaises(ValueError):
            enqueue_task(db.session, "delete", config.id, {"version": 1})
        with self.assertRaises(ValueError):
            enqueue_task(db.session, "scan", config.id, {})
