import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
from sqlalchemy import select

from video_filter.tests.support import DatabaseTestCase, bundle_arguments, signature
from app import db
from video_filter.feature_store import FeatureStore
from video_filter.feedback import FeedbackJournal, FeedbackConflict, feedback_values
from video_filter.identity import hash_stable, snapshot
from video_filter.learning import train, score, dataset
from video_filter.models import Asset, FeatureBundle, Location, ModelRun, Prediction, Task, TransferOperation, Variant
from video_filter.models.records import utc_now
from video_filter.tracking import reconcile
from video_filter.runtime import enqueue
from video_filter.tasks import claim_task, enqueue_task
from video_filter.transfer import classify


class WorkflowTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        roles = ("confirmed_like", "predicted_like", "predicted_dislike", "unclassified", "compressed_like")
        self.settings = {"enabled": True, "directories": {role: root / role for role in roles},
            "state_directory": root / "state", "stable_seconds": 1, "missing_seconds": 2,
            "task_timeout_seconds": 30, "scan_interval_seconds": 1,
            "deletion_feedback_enabled": True, "transfer_enabled": True,
            "lineage_enabled": False, "model_manifest": root / "manifest.json"}
        for path in self.settings["directories"].values():
            path.mkdir()
        self.now = utc_now()

    def tearDown(self):
        self.temporary.cleanup()
        super().tearDown()

    def scan(self, seconds):
        return reconcile(db.session, self.settings, self.now + timedelta(seconds=seconds))

    def register_file(self, role="confirmed_like", content=b"original-video"):
        path = self.settings["directories"][role] / "test.mp4"
        path.write_bytes(content)
        self.scan(0)
        self.scan(2)
        variant = db.session.execute(select(Variant)).scalar_one()
        return path, db.session.get(Asset, variant.asset_id), variant

    def save_bundle(self, asset, variant, value=1):
        args = bundle_arguments(asset.id, variant.id)
        args["source_sha256"] = variant.sha256
        args["source_snapshot"]["size_bytes"] = variant.size_bytes
        for name in args["vectors"]:
            args["vectors"][name][:] = value
        return FeatureStore().save(db.session, FeatureStore().prepare(**args))

    def test_rename_move_and_duplicate_cleanup_do_not_reject(self):
        path, asset, variant = self.register_file()
        duplicate = self.settings["directories"]["compressed_like"] / "duplicate.mp4"
        duplicate.write_bytes(path.read_bytes())
        self.scan(3)
        self.scan(5)
        renamed = path.with_name("renamed.mp4")
        path.rename(renamed)
        self.scan(6)
        self.scan(8)
        renamed.unlink()
        self.scan(10)
        self.scan(13)
        self.assertEqual(1, db.session.get(Asset, asset.id).label)
        self.assertEqual(1, db.session.query(Variant).count())

    def test_user_deletion_changes_label_preserves_summary_and_restore_updates(self):
        path, asset, variant = self.register_file()
        bundle = self.save_bundle(asset, variant)
        path.unlink()
        self.scan(3)
        self.assertEqual("missing", db.session.execute(select(Location)).scalar_one().status)
        self.scan(6)
        self.assertEqual(self.now + timedelta(seconds=3), db.session.execute(select(Location)).scalar_one().missing_since)
        self.assertEqual(0, db.session.get(Asset, asset.id).label)
        FeatureStore().require_ready(db.session, bundle.bundle_id)
        path.write_bytes(b"original-video")
        self.scan(7)
        self.scan(9)
        self.assertEqual(1, db.session.get(Asset, asset.id).label)

    def test_incomplete_scan_and_configuration_change_do_not_reject(self):
        path, asset, _ = self.register_file()
        path.unlink()
        inaccessible = self.settings["directories"]["predicted_like"]
        inaccessible.rmdir()
        self.assertFalse(self.scan(4).complete)
        self.assertEqual(1, db.session.get(Asset, asset.id).label)
        inaccessible.mkdir()
        self.settings["missing_seconds"] = 3
        self.scan(20)
        self.scan(30)
        self.assertEqual(1, db.session.get(Asset, asset.id).label)

    def test_feedback_journal_replays_commit_failure_and_rejects_stale_feedback(self):
        _, asset, _ = self.register_file()
        journal = FeedbackJournal(self.settings["state_directory"])
        values = feedback_values(asset, 0, {"reason": "user_confirmed"})
        with patch.object(db.session, "commit", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                journal.submit(db.session, values)
        self.assertEqual(1, len(list(journal.root.glob("*.json"))))
        self.assertEqual(1, journal.replay(db.session)["applied"])
        self.assertEqual(0, db.session.get(Asset, asset.id).label)
        journal.submit(db.session, values)
        stale = {**values, "label": 1, "event_key": "a" * 64}
        with self.assertRaises(FeedbackConflict):
            journal.submit(db.session, stale)

    def test_atomic_claim_and_insufficient_samples(self):
        _, asset, variant = self.register_file()
        self.save_bundle(asset, variant)
        with self.assertRaisesRegex(ValueError, "insufficient"):
            train(db.session, signature().digest)
        config = self.scan(3).config_revision_id
        task = enqueue_task(db.session, "scan", config, {"version": 1})
        db.session.commit()
        self.assertEqual(task.id, claim_task(db.session).id)
        self.assertIsNone(claim_task(db.session))

    def trained_model(self):
        for index in range(20):
            asset = Asset(label=index % 2)
            db.session.add(asset)
            db.session.flush()
            variant = Variant(asset_id=asset.id, sha256=hashlib.sha256(str(index).encode()).hexdigest(), size_bytes=10)
            db.session.add(variant)
            db.session.commit()
            self.save_bundle(asset, variant, float(index % 2) * 2 - 1)
        return train(db.session, signature().digest)

    def test_training_activation_and_feedback_retirement(self):
        model = self.trained_model()
        self.assertEqual("active", model.status)
        self.assertEqual(20, len(model.dataset_snapshot))
        asset = db.session.get(Asset, model.dataset_snapshot[0]["asset_id"])
        FeedbackJournal(self.settings["state_directory"]).submit(db.session,
            feedback_values(asset, 1 - asset.label, {"reason": "user_confirmed"}))
        self.assertIsNone(db.session.get(ModelRun, model.id).active_slot)

    def transfer_task(self):
        path, asset, variant = self.register_file("unclassified")
        bundle = self.save_bundle(asset, variant)
        model = self.trained_model()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "classify", variant.id)
        return path, asset, bundle, task

    def test_classification_commits_summary_before_publish_and_never_labels_asset(self):
        path, asset, bundle, task = self.transfer_task()
        operation = classify(db.session, self.settings, task)
        self.assertEqual("source_cleaned", operation.status)
        self.assertFalse(path.exists())
        self.assertTrue(Path(operation.destination_path).exists())
        self.assertIsNone(db.session.get(Asset, asset.id).label)
        FeatureStore().require_ready(db.session, bundle.bundle_id)
        self.scan(5)
        target = Path(operation.destination_path)
        target.unlink()
        self.scan(6)
        self.scan(9)
        self.assertEqual(0, db.session.get(Asset, asset.id).label)

    def test_same_name_conflict_preserves_source_and_existing_target(self):
        path, _, _, task = self.transfer_task()
        for role in ("predicted_like", "predicted_dislike"):
            (self.settings["directories"][role] / path.name).write_bytes(b"user-owned")
        with self.assertRaisesRegex(ValueError, "conflict"):
            classify(db.session, self.settings, task)
        self.assertTrue(path.exists())
        self.assertEqual(0, db.session.query(TransferOperation).count())

    def test_source_replacement_after_publication_is_preserved(self):
        path, _, _, task = self.transfer_task()
        from video_filter import transfer
        original = transfer.record_published
        def replace_source(session, operation):
            result = original(session, operation)
            path.write_bytes(b"replacement")
            return result
        with patch.object(transfer, "record_published", side_effect=replace_source):
            with self.assertRaises(ValueError):
                classify(db.session, self.settings, task)
        self.assertEqual(b"replacement", path.read_bytes())

    def test_interrupted_publication_recovers_only_owned_target(self):
        path, _, _, task = self.transfer_task()
        with patch("video_filter.transfer.record_published", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                classify(db.session, self.settings, task)
        db.session.rollback()
        operation = db.session.execute(select(TransferOperation)).scalar_one()
        self.assertEqual("destination_verified", operation.status)
        self.assertTrue(path.exists())
        self.assertTrue(Path(operation.destination_path).exists())
        operation = classify(db.session, self.settings, task)
        self.assertEqual("source_cleaned", operation.status)
        self.assertFalse(path.exists())

    def test_target_deleted_before_cleanup_preserves_source(self):
        path, _, _, task = self.transfer_task()
        from video_filter import transfer
        original = transfer.record_published
        def delete_target(session, operation):
            result = original(session, operation)
            from media_lineage.service import operation_events
            Path(operation_events(session, operation)[0].destination_path).unlink()
            return result
        with patch.object(transfer, "record_published", side_effect=delete_target):
            with self.assertRaises(OSError):
                classify(db.session, self.settings, task)
        self.assertTrue(path.exists())

    def test_missing_summary_prevents_any_publication(self):
        path, _, bundle, task = self.transfer_task()
        db.session.get(FeatureBundle, bundle.bundle_id).arrays_blob = b"corrupt"
        db.session.commit()
        with self.assertRaises(ValueError):
            classify(db.session, self.settings, task)
        self.assertTrue(path.exists())
        self.assertEqual(0, db.session.query(TransferOperation).count())

    def test_gpu_lock_excludes_second_holder(self):
        from media_lineage.resources import gpu_lock

        with gpu_lock(self.settings["state_directory"]) as first:
            self.assertTrue(first)
            with gpu_lock(self.settings["state_directory"]) as second:
                self.assertFalse(second)
        with gpu_lock(self.settings["state_directory"]) as after:
            self.assertTrue(after)

    def test_compression_before_initial_scan_inherits_label_across_hash_change(self):
        from media_lineage.service import begin_operation, record_published, record_source_removed
        from uuid import uuid4

        source = self.settings["directories"]["confirmed_like"] / "test.mp4"
        target = self.settings["directories"]["compressed_like"] / "test.mp4"
        source.write_bytes(b"original")
        operation = begin_operation(db.session, producer="video_compression", source_path=source,
            destination_path=target, generation=str(uuid4()), source_role="confirmed_like", destination_role="compressed_like")
        db.session.commit()
        target.write_bytes(b"encoded")
        record_published(db.session, operation)
        db.session.commit()
        source.unlink()
        record_source_removed(db.session, operation)
        db.session.commit()
        self.scan(0)
        self.scan(2)
        variants = db.session.execute(select(Variant)).scalars().all()
        self.assertEqual(2, len(variants))
        self.assertEqual(1, len({variant.asset_id for variant in variants}))
        self.assertEqual(1, db.session.get(Asset, variants[0].asset_id).label)

    def independent_compression_source(self):
        source = self.settings["state_directory"].parent / "compression-source"
        source.mkdir()
        self.settings.update(lineage_enabled=True)
        return source

    def compress_in_isolated_directories(self, source, before_publication=None):
        from types import SimpleNamespace
        from uuid import uuid4
        from env import EnvConfig
        from media_lineage.integration import compression_begin, compression_published, compression_cleaned

        target = self.settings["directories"]["compressed_like"] / source.name
        mission = SimpleNamespace(id=uuid4(), attempts=1)
        with patch("media_lineage.integration.enabled", return_value=True), patch.object(EnvConfig, "video_filter_settings", return_value=self.settings):
            compression_begin(db.session, mission, source, target)
            if before_publication is not None:
                before_publication()
            target.write_bytes(b"reencoded-video")
            compression_published(db.session, mission)
            source.unlink()
            compression_cleaned(db.session, mission)
        return target

    def test_independent_source_and_output_do_not_create_positive_training_labels(self):
        from video_filter.learning import training_snapshot
        from video_filter.models import FeedbackEvent

        source = self.independent_compression_source() / "unknown.mp4"
        source.write_bytes(b"unknown-source-video")
        self.scan(0)
        self.scan(2)
        self.assertEqual(0, db.session.query(Variant).count())
        target = self.compress_in_isolated_directories(source)
        self.scan(3)
        self.scan(5)
        variants = db.session.execute(select(Variant)).scalars().all()
        self.assertEqual(2, len(variants))
        self.assertEqual(1, len({item.asset_id for item in variants}))
        asset = db.session.get(Asset, variants[0].asset_id)
        self.assertIsNone(asset.label)
        self.assertEqual(0, db.session.query(FeedbackEvent).count())
        encoded = next(item for item in variants if item.sha256 == hash_stable(target)[0])
        self.save_bundle(asset, encoded)
        self.assertEqual([], training_snapshot(db.session, signature().digest))

    def test_pending_compression_and_reencode_preserve_existing_label_and_summary(self):
        source_root = self.independent_compression_source()
        path, asset, variant = self.register_file()
        bundle = self.save_bundle(asset, variant)
        source = path.rename(source_root / path.name)
        def check_pending_compression():
            self.scan(3)
            self.scan(6)
            self.scan(9)
            self.assertEqual(1, db.session.get(Asset, asset.id).label)
        target = self.compress_in_isolated_directories(source, before_publication=check_pending_compression)
        self.scan(10)
        self.scan(12)
        self.assertEqual(1, db.session.get(Asset, asset.id).label)
        self.assertEqual({asset.id}, set(db.session.execute(select(Variant.asset_id)).scalars()))
        FeatureStore().require_ready(db.session, bundle.bundle_id)
        target.unlink()
        self.scan(14)
        self.scan(17)
        self.assertEqual(0, db.session.get(Asset, asset.id).label)
        FeatureStore().require_ready(db.session, bundle.bundle_id)

    def test_independent_compression_preserves_latest_negative_label(self):
        source_root = self.independent_compression_source()
        path, asset, _ = self.register_file()
        content = path.read_bytes()
        path.unlink()
        self.scan(3)
        self.scan(6)
        self.assertEqual(0, db.session.get(Asset, asset.id).label)
        revision = asset.label_revision
        source = source_root / path.name
        source.write_bytes(content)
        self.scan(7)
        self.scan(9)
        self.compress_in_isolated_directories(source)
        self.scan(10)
        self.scan(12)
        self.assertEqual(0, db.session.get(Asset, asset.id).label)
        self.assertEqual(revision, asset.label_revision)
        self.assertEqual({asset.id}, set(db.session.execute(select(Variant.asset_id)).scalars()))

    def test_independent_source_changes_do_not_pause_managed_deletion_inference(self):
        source_root = self.independent_compression_source()
        path, asset, _ = self.register_file()
        path.unlink()
        source = source_root / "being-copied.mp4"
        source.write_bytes(b"partial")
        self.scan(3)
        source.write_bytes(b"partial-more")
        self.scan(6)
        self.assertEqual(0, db.session.get(Asset, asset.id).label)

    def test_independent_source_is_never_enumerated_hashed_or_registered(self):
        from video_filter.models import Observation

        source_root = self.independent_compression_source()
        source = source_root / "transient.mp4"
        source.write_bytes(b"transient-compression-input")
        # An obsolete setting must not expand the managed scan scope either.
        self.settings["compression_source_directory"] = source_root
        original_iterdir = Path.iterdir
        def enumerate_managed(path):
            self.assertNotEqual(source_root.resolve(), path.resolve())
            return original_iterdir(path)
        with patch.object(Path, "iterdir", new=enumerate_managed), patch("video_filter.tracking.hash_stable", wraps=hash_stable) as hashes:
            self.scan(0)
            scan = self.scan(2)
            hashes.assert_not_called()
        self.assertEqual(set(self.settings["directories"]), set(scan.role_results))
        self.assertEqual(0, db.session.query(Observation).count())
        self.assertEqual(0, db.session.query(Variant).count())
        source.unlink()
        source_root.rmdir()
        self.assertTrue(self.scan(3).complete)
