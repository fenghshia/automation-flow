from uuid import uuid4
from unittest.mock import MagicMock

from video_filter.tests.support import DatabaseTestCase
from app import db
from media_lineage.service import PHASES, append_event, read_events


class LineageTests(DatabaseTestCase):
    def values(self):
        return {
            "operation_id": str(uuid4()), "sequence": 0, "producer": "video_compression",
            "generation": str(uuid4()), "phase": "planned", "source_sha256": "a" * 64,
            "destination_sha256": None, "source_path": "test-source.mp4",
            "destination_path": "test-output.mp4",
        }

    def test_idempotent_replay_and_conflict(self):
        values = self.values()
        first = append_event(db.session, **values)
        db.session.commit()
        second = append_event(db.session, **values)
        self.assertEqual(first.id, second.id)
        values["source_sha256"] = "c" * 64
        with self.assertRaises(ValueError):
            append_event(db.session, **values)
        self.assertEqual(1, len(read_events(db.session)))

    def test_phase_order_and_byte_identity(self):
        values = self.values()
        for sequence, phase in enumerate(PHASES):
            values.update(sequence=sequence, phase=phase)
            if sequence:
                values["destination_sha256"] = "b" * 64
            append_event(db.session, **values)
            db.session.commit()
        events = read_events(db.session)
        self.assertEqual(list(PHASES), [record.phase for record in events])
        self.assertEqual(2, len(read_events(db.session, after_id=events[1].id)))

    def test_missing_prior_phase_or_reused_identity_rejected(self):
        values = self.values()
        with self.assertRaises(ValueError):
            append_event(db.session, **{**values, "sequence": 1, "phase": PHASES[1], "destination_sha256": "b" * 64})
        append_event(db.session, **values)
        db.session.commit()
        with self.assertRaises(ValueError):
            append_event(db.session, **{**values, "sequence": 1, "phase": PHASES[1], "generation": str(uuid4()), "destination_sha256": "b" * 64})

    def test_record_cannot_be_mutated_or_deleted(self):
        values = self.values()
        record = append_event(db.session, **values)
        db.session.commit()
        record.source_path = "changed-test-path"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        db.session.delete(record)
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()

    def test_same_task_generation_can_have_new_operation_without_merging(self):
        values = self.values()
        append_event(db.session, **values)
        append_event(db.session, **{**values, "operation_id": str(uuid4())})
        db.session.commit()
        self.assertEqual(2, len(read_events(db.session)))

    def test_postgresql_serializes_id_allocation_until_commit(self):
        session = MagicMock()
        session.get_bind.return_value.dialect.name = "postgresql"
        session.execute.return_value.scalar_one_or_none.return_value = None
        append_event(session, **self.values())
        first = session.execute.call_args_list[0]
        self.assertIn("pg_advisory_xact_lock", str(first.args[0]))
        session.commit.assert_not_called()
