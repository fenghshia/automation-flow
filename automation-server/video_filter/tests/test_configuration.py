from pathlib import Path

from video_filter.tests.support import DatabaseTestCase
from app import db
from video_filter.configuration import record_configuration


class ConfigurationRevisionTests(DatabaseTestCase):
    def test_changed_path_creates_pending_generation_without_relabeling(self):
        asset, variant, initial = self.identities()
        settings = {"enabled": True, "directories": {"unclassified": Path("test-root")}}
        first = record_configuration(db.session, settings)
        db.session.commit()
        self.assertEqual(first.id, record_configuration(db.session, settings).id)
        changed = {"enabled": True, "directories": {"unclassified": Path("new-test-root")}}
        second = record_configuration(db.session, changed)
        db.session.commit()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual("pending", second.status)
        self.assertEqual(1, asset.label)
        self.assertEqual(0, asset.label_revision)
