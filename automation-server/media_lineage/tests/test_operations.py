import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_filter.tests.support import DatabaseTestCase
from app import db
from media_lineage.integration import compression_begin, compression_published, compression_cleaned
from media_lineage.service import operation_events, record_published
from media_lineage.models import LineageEvent
from env import EnvConfig


class OperationTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "source").mkdir()
        (root / "output").mkdir()
        self.source = root / "source" / "source.mp4"
        self.target = root / "output" / "target.mp4"
        self.source.write_bytes(b"source-video")
        self.mission = SimpleNamespace(id=7, attempts=1)
        self.enabled = patch("media_lineage.integration.enabled", return_value=True)
        self.enabled.start()
        self.settings = {"directories": {"confirmed_like": root / "confirmed",
                                         "compressed_like": self.target.parent}}
        self.configuration = patch.object(EnvConfig, "video_filter_settings", return_value=self.settings)
        self.configuration.start()

    def tearDown(self):
        self.enabled.stop()
        self.configuration.stop()
        self.temporary.cleanup()
        super().tearDown()

    def test_reencode_records_durable_mapping_before_cleanup(self):
        compression_begin(db.session, self.mission, self.source, self.target)
        self.target.write_bytes(b"encoded-video")
        compression_published(db.session, self.mission)
        events = db.session.query(LineageEvent).order_by(LineageEvent.id).all()
        self.assertEqual(["planned", "destination_verified", "published"], [event.phase for event in events])
        self.assertNotEqual(events[0].source_sha256, events[2].destination_sha256)
        self.assertIsNone(events[0].evidence["source_role"])
        self.assertEqual("compressed_like", events[0].evidence["destination_role"])
        self.source.unlink()
        compression_cleaned(db.session, self.mission)
        compression_cleaned(db.session, self.mission)
        self.assertEqual(4, db.session.query(LineageEvent).count())

    def test_source_replacement_and_unregistered_recovery_are_rejected(self):
        compression_begin(db.session, self.mission, self.source, self.target)
        self.target.write_bytes(b"encoded-video")
        self.source.write_bytes(b"replacement")
        with self.assertRaises(ValueError):
            compression_published(db.session, self.mission)
        self.assertTrue(self.source.exists())
        self.mission.attempts += 1
        with self.assertRaises(ValueError):
            compression_published(db.session, self.mission)

    def test_missing_source_cannot_fabricate_published_mapping(self):
        compression_begin(db.session, self.mission, self.source, self.target)
        self.target.write_bytes(b"unowned-target")
        self.source.unlink()
        with self.assertRaises(ValueError):
            compression_published(db.session, self.mission, allow_missing=True)
        self.assertEqual(1, db.session.query(LineageEvent).count())

    def test_new_execution_has_independent_operation_identity(self):
        compression_begin(db.session, self.mission, self.source, self.target)
        self.mission.attempts += 1
        compression_begin(db.session, self.mission, self.source, self.target)
        events = db.session.query(LineageEvent).all()
        self.assertEqual(2, len({event.operation_id for event in events}))
        self.assertEqual(2, len({event.generation for event in events}))

    def test_source_in_confirmed_directory_records_real_positive_evidence(self):
        self.settings["directories"]["confirmed_like"] = self.source.parent
        compression_begin(db.session, self.mission, self.source, self.target)
        event = db.session.query(LineageEvent).one()
        self.assertEqual("confirmed_like", event.evidence["source_role"])

    def test_direct_compression_does_not_claim_unknown_source_is_liked(self):
        from media_lineage.integration import direct_compression

        with direct_compression(self.source, self.target):
            event = db.session.query(LineageEvent).one()
            self.assertIsNone(event.evidence["source_role"])
            self.assertEqual("compressed_like", event.evidence["destination_role"])
