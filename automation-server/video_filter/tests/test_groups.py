import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from .support import DatabaseTestCase, bundle_arguments, signature
from app import db
from video_filter.group_config import ROLES, load_groups, require_scope
from video_filter.scope import group_scope
from video_filter.models import Asset, Variant, Location
from video_filter.models.records import utc_now
from video_filter.tracking import reconcile
from video_filter.feature_store import FeatureStore
from video_filter.training_config import digest
from media_lineage.resources import request_lease, release


class GroupTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.groups = [self.group("alpha"), self.group("beta")]
        self.now = utc_now()

    def tearDown(self):
        self.temp.cleanup()
        super().tearDown()

    def group(self, name):
        roots = {role: self.root / name / role for role in ROLES}
        for path in roots.values():
            path.mkdir(parents=True)
        return {"enabled": True, "grouped": True, "name": name, "directories": roots,
            "state_directory": self.root / "state", "model_manifest": None,
            "stable_seconds": 1, "missing_seconds": 2, "scan_interval_seconds": 1,
            "task_timeout_seconds": 30, "lineage_enabled": True, "deletion_feedback_enabled": True,
            "classifier": "logistic_regression", "compression_enabled": False}

    def scan(self, settings, seconds):
        return reconcile(db.session, settings, self.now + timedelta(seconds=seconds))

    def test_all_groups_require_manual_training_regardless_of_new_sample_count(self):
        from uuid import UUID
        from video_filter.configuration import record_configuration
        from video_filter.models import ModelRun, ScanRun
        from video_filter.runtime import enqueue_automatic

        data = [{"asset_id": str(UUID(int=index + 1)), "label": index % 2, "label_revision": 1,
                 "bundle_id": str(UUID(int=index + 1001)), "manifest_sha256": "a" * 64}
                for index in range(120)]
        for group in self.groups:
            with group_scope(db.session, group) as scoped:
                config = record_configuration(db.session, scoped)
                config.status, config.active_slot = "active", "active"
                db.session.add(ScanRun(config_revision_id=config.id, complete=True))
                for kind in ("logistic_regression", "mil"):
                    db.session.add(ModelRun(model_type=kind, feature_signature=signature().digest,
                        dataset_snapshot=data[:20], status="validated", training_config_digest=digest(scoped, kind)))
                db.session.commit()
        for group, count in zip(self.groups, (119, 120)):
            settings = {**group, "model_manifest": self.root / "manifest.json", "ffmpeg_directory": self.root / "tools"}
            with group_scope(db.session, settings) as scoped, \
                    patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                    patch("video_filter.learning.training_snapshot", return_value=data[:count]), \
                    patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
                enqueue_automatic(db.session, scoped)
                submit.assert_not_called()

    def test_same_hash_is_independent_and_all_query_forms_are_scoped(self):
        ids = []
        for group, label in zip(self.groups, (1, 0)):
            with group_scope(db.session, group):
                asset = Asset(label=label)
                db.session.add(asset)
                db.session.flush()
                variant = Variant(asset_id=asset.id, sha256="b" * 64, size_bytes=10)
                db.session.add(variant)
                db.session.commit()
                ids.append(asset.id)
                self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Asset)))
                self.assertEqual([label], list(db.session.scalars(select(Asset.label))))
                self.assertEqual(1, len(db.session.execute(select(Asset, Variant).join(Variant, Variant.asset_id == Asset.id)).all()))
        with group_scope(db.session, self.groups[0]):
            self.assertIsNone(db.session.get(Asset, ids[1]))
            self.assertEqual(1, db.session.scalar(select(Asset.label)))
            db.session.add(Variant(asset_id=ids[1], sha256="c" * 64, size_bytes=10))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_core_feature_read_cannot_cross_group(self):
        with group_scope(db.session, self.groups[0]) as scoped:
            asset = Asset(label=1)
            db.session.add(asset)
            db.session.flush()
            variant = Variant(asset_id=asset.id, sha256="b" * 64, size_bytes=10)
            db.session.add(variant)
            db.session.commit()
            args = bundle_arguments(asset.id, variant.id)
            args.update(dataset_group_id=scoped["dataset_group_id"], reset_epoch=scoped["reset_epoch"])
            prepared = FeatureStore().prepare(**args)
            stored = FeatureStore().save(db.session, prepared)
            bundle_id = stored.bundle_id
        with group_scope(db.session, self.groups[1]):
            with self.assertRaises(ValueError):
                FeatureStore().require_ready(db.session, bundle_id)
            with self.assertRaisesRegex(ValueError, "cross_group"):
                FeatureStore().save(db.session, prepared)

    def test_deleted_copy_rejects_even_with_surviving_liked_copy(self):
        group = self.groups[0]
        liked = group["directories"]["liked"] / "fixture.mp4"
        duplicate = group["directories"]["predicted_like"] / "fixture.mp4"
        liked.write_bytes(b"synthetic-video")
        duplicate.write_bytes(liked.read_bytes())
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            self.assertEqual(1, db.session.scalar(select(Asset.label)))
            duplicate.unlink()
            self.scan(scoped, 3)
            self.scan(scoped, 6)
            self.assertEqual(0, db.session.scalar(select(Asset.label)))
            self.scan(scoped, 9)
            self.assertEqual(0, db.session.scalar(select(Asset.label)))

    def test_new_destination_is_move_and_path_change_does_not_relabel(self):
        group = self.groups[0]
        source = group["directories"]["unclassified"] / "fixture.mp4"
        source.write_bytes(b"synthetic-video")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            target = group["directories"]["liked_source"] / source.name
            source.rename(target)
            self.scan(scoped, 3)
            self.scan(scoped, 5)
            self.scan(scoped, 8)
            self.assertEqual(1, db.session.scalar(select(Asset.label)))
            self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Variant)))
            target.unlink()
            self.scan(scoped, 9)
            self.scan(scoped, 12)
            self.assertEqual(0, db.session.scalar(select(Asset.label)))
            identifier = scoped["dataset_group_id"]
        changed = {**group, "directories": dict(group["directories"])}
        changed["directories"]["liked"] = self.root / "new-liked"
        changed["directories"]["liked"].mkdir()
        (changed["directories"]["liked"] / "new.mp4").write_bytes(b"synthetic-video")
        with group_scope(db.session, changed) as scoped:
            self.assertEqual(identifier, scoped["dataset_group_id"])
            self.scan(scoped, 13)
            self.scan(scoped, 15)
            self.assertEqual(0, db.session.scalar(select(Asset.label)))

    def test_liked_source_is_never_sample_allowed_and_only_mil_digest_changes(self):
        with self.assertRaisesRegex(ValueError, "outside_group_scope"):
            require_scope(self.groups[0], self.groups[0]["directories"]["liked_source"] / "fixture.mp4", sample=True)
        changed = {**self.groups[0], "training": {"mil": {"dropout": .3}}}
        self.assertEqual(digest(self.groups[0], "logistic_regression"), digest(changed, "logistic_regression"))
        self.assertNotEqual(digest(self.groups[0], "mil"), digest(changed, "mil"))

    def test_resource_capacity_and_exclusive_fairness(self):
        leases = [request_lease(db.session, "cpu", "extract_shared", "extract-" + str(i), check_memory=False) for i in range(6)]
        self.assertTrue(all(leases))
        self.assertIsNone(request_lease(db.session, "cpu", "exclusive_train", "train", check_memory=False))
        self.assertIsNone(request_lease(db.session, "cpu", "extract_shared", "seventh", check_memory=False))
        for lease in leases:
            release(db.session, lease)
        self.assertIsNone(request_lease(db.session, "cpu", "extract_shared", "seventh", check_memory=False))
        train = request_lease(db.session, "cpu", "exclusive_train", "train", check_memory=False)
        self.assertIsNotNone(train)
        release(db.session, train)
        self.assertIsNotNone(request_lease(db.session, "cpu", "extract_shared", "seventh", check_memory=False))

    def test_partial_scan_commits_positive_arrival_and_recovers_move_without_rejection(self):
        from video_filter.identity import hash_stable
        from video_filter.models import FeedbackEvent, ScanRun
        from video_filter.runtime import _extraction_baseline_ready
        group = self.groups[0]
        source = group["directories"]["predicted_dislike"] / "fixture.mp4"
        source.write_bytes(b"synthetic-moved-video")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            asset = db.session.scalar(select(Asset))
            asset.label = 0
            db.session.commit()
            target = group["directories"]["liked"] / source.name
            source.rename(target)
            later = group["directories"]["predicted_like"] / "later.mp4"
            later.write_bytes(b"synthetic-later-video")
            self.scan(scoped, 3)
            def interrupted(path, stat):
                self.assertFalse(db.session().in_transaction(), "Hashing must own no transaction")
                if Path(path).resolve() == later.resolve():
                    raise FileNotFoundError("synthetic interruption")
                return hash_stable(path, stat)
            with patch("video_filter.tracking.hash_stable", side_effect=interrupted):
                partial = self.scan(scoped, 5)
            self.assertFalse(partial.complete)
            self.assertEqual(1, db.session.get(Asset, asset.id).label)
            self.assertGreater(db.session.scalar(select(func.count()).select_from(FeedbackEvent)), 0)
            self.assertIsNotNone(db.session.scalar(select(Location.id).where(Location.path == str(target.resolve()))))
            self.scan(scoped, 8)
            self.scan(scoped, 12)
            self.assertEqual(1, db.session.get(Asset, asset.id).label)
            old = db.session.scalar(select(Location).where(Location.path == str(source.resolve())))
            self.assertEqual("retired", old.status)

    def test_location_published_while_hashing_is_reused(self):
        from video_filter.identity import hash_stable, path_key
        group = {**self.groups[0], "stable_seconds": 0}
        path = group["directories"]["unclassified"] / "fixture.mp4"
        path.write_bytes(b"synthetic-video")
        published = []
        with group_scope(db.session, group) as scoped:
            def publish_during_hash(source, stat):
                self.assertFalse(db.session().in_transaction())
                digest, identity = hash_stable(source, stat)
                asset = Asset()
                db.session.add(asset)
                db.session.flush()
                variant = Variant(asset_id=asset.id, sha256=digest, size_bytes=stat["size_bytes"])
                db.session.add(variant)
                db.session.flush()
                location = Location(variant_id=variant.id, role="unclassified", path=str(source),
                    current_path_key=path_key(source), **stat)
                db.session.add(location)
                db.session.commit()
                published.append(location.id)
                return digest, identity
            with patch("video_filter.tracking.hash_stable", side_effect=publish_during_hash):
                scan = self.scan(scoped, 0)
            self.assertTrue(scan.complete)
            self.assertEqual(published, list(db.session.scalars(select(Location.id))))
            self.assertEqual(scan.id, db.session.get(Location, published[0]).last_scan_id)

    def test_location_insert_collision_keeps_session_and_history_usable(self):
        from video_filter.tracking import _insert_scan_location
        group = self.groups[0]
        with group_scope(db.session, group):
            asset = Asset()
            db.session.add(asset)
            db.session.flush()
            variant = Variant(asset_id=asset.id, sha256="e" * 64, size_bytes=10)
            db.session.add(variant)
            db.session.flush()
            values = dict(variant_id=variant.id, role="unclassified", path="fixture.mp4",
                          current_path_key="f" * 64, size_bytes=10, modified_ns=20)
            first, inserted = _insert_scan_location(db.session, **values)
            self.assertTrue(inserted)
            second, inserted = _insert_scan_location(db.session, **values)
            self.assertFalse(inserted)
            self.assertEqual(first.id, second.id)
            db.session.commit()
            self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Location)))
            with self.assertRaises(IntegrityError):
                _insert_scan_location(db.session, **{**values, "current_path_key": "a" * 64, "role": "invalid"})

    def test_initial_partial_identity_batch_allows_extraction_but_never_deletion(self):
        from video_filter.identity import hash_stable
        from video_filter.models import FeedbackEvent
        from video_filter.runtime import _extraction_baseline_ready
        group = {**self.groups[0], "stable_seconds": 0}
        paths = [group["directories"]["unclassified"] / name for name in ("a.mp4", "b.mp4")]
        for i, path in enumerate(paths):
            path.write_bytes(str(i).encode())
        with group_scope(db.session, group) as scoped:
            def stop_on_second(path, stat):
                self.assertFalse(db.session().in_transaction())
                if Path(path).resolve() == paths[1].resolve():
                    raise FileNotFoundError("synthetic interruption")
                return hash_stable(path, stat)
            with patch("video_filter.tracking.hash_stable", side_effect=stop_on_second):
                partial = self.scan(scoped, 0)
            self.assertFalse(partial.complete)
            self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Variant)))
            self.assertTrue(_extraction_baseline_ready(db.session, scoped, partial.config_revision_id))
            self.assertEqual(0, db.session.scalar(select(func.count()).select_from(FeedbackEvent)))

    def test_compression_fast_passage_preserves_identity_and_cleanup_is_not_deletion(self):
        from uuid import uuid4
        from media_lineage.service import begin_operation, record_published, record_source_removed
        group = self.groups[0]
        source = group["directories"]["liked_source"] / "fixture.mp4"
        target = group["directories"]["liked"] / "fixture.mp4"
        source.write_bytes(b"synthetic-original")
        target.write_bytes(b"synthetic-compressed")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            operation = begin_operation(db.session, producer="video_compression", source_path=source,
                destination_path=target, generation=str(uuid4()), source_role="liked_source", destination_role="liked",
                scope_evidence={"dataset_group_id": scoped["dataset_group_id"], "reset_epoch": scoped["reset_epoch"]})
            db.session.commit()
            record_published(db.session, operation)
            db.session.commit()
            source.unlink()
            record_source_removed(db.session, operation)
            db.session.commit()
            self.scan(scoped, 2)
            self.scan(scoped, 5)
            self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Asset)))
            self.assertEqual(2, db.session.scalar(select(func.count()).select_from(Variant)))
            self.assertEqual(1, db.session.scalar(select(Asset.label)))
            target.unlink()
            self.scan(scoped, 6)
            self.scan(scoped, 9)
            self.assertEqual(0, db.session.scalar(select(Asset.label)))

    def test_configuration_rejects_duplicate_names_unknown_fields_and_overlaps(self):
        path = self.root / "groups.json"
        shared = {"state_directory": self.root / "state", "model_manifest": None}
        item = {"name": "alpha", "directories": {r: str(p) for r, p in self.groups[0]["directories"].items()}}
        for content in ({"schema_version": 1, "groups": [item, item]},
                        {"schema_version": True, "groups": [item]},
                        {"schema_version": 1, "groups": [{**item, "unknown": 1}]},
                        {"schema_version": 1, "groups": [{**item, "directories": {**item["directories"], "liked": item["directories"]["unclassified"]}}]}):
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_groups(path, self.root, shared)
        path.write_text(json.dumps({"schema_version": 1, "groups": [item]}), encoding="utf-8")
        self.assertEqual("alpha", load_groups(path, self.root, shared)[0]["name"])

    def test_deleted_old_location_cannot_repeat_after_new_positive_feedback(self):
        group = self.groups[0]
        source = group["directories"]["liked"] / "fixture.mp4"
        source.write_bytes(b"synthetic-video")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            source.unlink()
            self.scan(scoped, 3)
            self.scan(scoped, 6)
            self.assertEqual(0, db.session.scalar(select(Asset.label)))
            new = group["directories"]["liked_source"] / "new-confirmation.mp4"
            new.write_bytes(b"synthetic-video")
            self.scan(scoped, 7)
            self.scan(scoped, 9)
            self.scan(scoped, 12)
            self.assertEqual(1, db.session.scalar(select(Asset.label)))

    def test_deleted_predicted_dislike_summary_remains_trainable_for_both_models(self):
        from video_filter.learning import dataset, training_snapshot
        from video_filter.reporting import status_snapshot
        from video_filter.identity import snapshot
        group = self.groups[0]
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"specification": signature().to_dict()}), encoding="utf-8")
        group["model_manifest"] = manifest
        path = group["directories"]["predicted_dislike"] / "fixture.mp4"
        path.write_bytes(b"synthetic-video")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            variant = db.session.scalar(select(Variant))
            args = bundle_arguments(variant.asset_id, variant.id)
            stat = snapshot(path)
            args.update(source_sha256=variant.sha256,
                source_snapshot={key: stat[key] for key in ("size_bytes", "modified_ns")},
                dataset_group_id=scoped["dataset_group_id"], reset_epoch=scoped["reset_epoch"])
            stored = FeatureStore().save(db.session, FeatureStore().prepare(**args))
            path.unlink()
            self.scan(scoped, 3)
            self.assertIsNone(db.session.get(Asset, variant.asset_id).label)
            self.scan(scoped, 6)
            self.assertEqual(0, db.session.get(Asset, variant.asset_id).label)
            self.assertEqual("retired", db.session.scalar(select(Location.status)))
            counts = status_snapshot(db.session, scoped)["counts"]
            self.assertEqual(0, counts["present_variants"])
            self.assertEqual(1, counts["ready_summaries"])
            self.assertEqual(1, counts["trainable_negative_assets"])
            self.assertEqual(1, len(training_snapshot(db.session, signature().digest)))
            for kind in ("logistic_regression", "mil"):
                vectors, labels, rows = dataset(db.session, signature().digest, kind)
                self.assertEqual([0], labels.tolist())
                self.assertEqual(1, len(vectors))
                self.assertEqual(stored.bundle_id, rows[0]["bundle_id"])

    def add_lineage(self, evidence):
        from media_lineage.models import LineageEvent
        event = LineageEvent(operation_id=str(uuid4()), sequence=0, producer="video_filter",
            generation=str(uuid4()), phase="planned", source_sha256="a" * 64,
            source_path=str(self.groups[0]["directories"]["unclassified"] / "fixture.mp4"),
            destination_path=str(self.groups[0]["directories"]["predicted_like"] / "fixture.mp4"),
            evidence=evidence)
        db.session.add(event)
        db.session.commit()
        return event

    def test_lineage_wait_is_scoped_and_automatic_tasks_resume_after_reconciliation(self):
        from video_filter.runtime import enqueue, enqueue_automatic, reconciliation_pending
        from video_filter.configuration import record_configuration
        from video_filter.tracking import lineage_pending
        group = {**self.groups[0], "model_manifest": self.root / "manifest.json",
                 "ffmpeg_directory": self.root / "tools"}
        path = group["directories"]["unclassified"] / "fixture.mp4"
        path.write_bytes(b"synthetic-video")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            variant = db.session.scalar(select(Variant))
            config = record_configuration(db.session, scoped)
            for evidence in ({}, {"dataset_group_id": str(uuid4()), "reset_epoch": scoped["reset_epoch"]},
                    {"dataset_group_id": scoped["dataset_group_id"], "reset_epoch": str(uuid4())}):
                self.add_lineage(evidence)
            self.assertFalse(reconciliation_pending(db.session, scoped, config.id))
            with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
                self.assertEqual("queued", enqueue(db.session, scoped, "extract", variant.id).status)
            event = self.add_lineage({"dataset_group_id": scoped["dataset_group_id"], "reset_epoch": scoped["reset_epoch"]})
            self.assertTrue(reconciliation_pending(db.session, scoped, config.id))
            self.assertFalse(lineage_pending(db.session, {**scoped, "lineage_floor": event.id}, 0))
            with self.assertRaisesRegex(ValueError, "lineage_reconciliation_pending"):
                enqueue(db.session, scoped, "extract", variant.id)
            with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                    patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit, \
                    patch("video_filter.runtime.log_exception") as failures:
                enqueue_automatic(db.session, scoped)
                submit.assert_not_called()
                failures.assert_not_called()
                self.scan(scoped, 4)
                self.assertFalse(reconciliation_pending(db.session, scoped, config.id))
                enqueue_automatic(db.session, scoped)
                # The existing queued extraction is already sufficient; resume
                # must not rediscover it as a new task.
                submit.assert_not_called()

    def test_all_five_roles_support_user_deletion(self):
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            for index, role in enumerate(ROLES):
                with self.subTest(role=role):
                    path = group["directories"][role] / "fixture.mp4"
                    path.write_bytes(("synthetic-" + role).encode())
                    base = index * 20
                    self.scan(scoped, base)
                    self.scan(scoped, base + 2)
                    from video_filter.identity import hash_stable
                    digest = hash_stable(path)[0]
                    asset_id = db.session.scalar(select(Variant.asset_id).where(Variant.sha256 == digest))
                    path.unlink()
                    self.scan(scoped, base + 3)
                    self.scan(scoped, base + 6)
                    self.assertEqual(0, db.session.get(Asset, asset_id).label)

    def test_notification_overflow_rebaseline_does_not_infer_deletion(self):
        group = self.groups[0]
        path = group["directories"]["liked"] / "fixture.mp4"
        path.write_bytes(b"synthetic-video")
        with group_scope(db.session, group) as scoped:
            self.scan(scoped, 0)
            self.scan(scoped, 2)
            path.unlink()
            self.scan({**scoped, "notifications_uncertain": True}, 3)
            self.scan(scoped, 6)
            self.assertEqual(1, db.session.scalar(select(Asset.label)))
