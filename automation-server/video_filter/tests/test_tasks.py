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

    def test_invalid_kind_or_unversioned_input_rejected(self):
        asset, variant, config = self.identities()
        with self.assertRaises(ValueError):
            enqueue_task(db.session, "delete", config.id, {"version": 1})
        with self.assertRaises(ValueError):
            enqueue_task(db.session, "scan", config.id, {})
