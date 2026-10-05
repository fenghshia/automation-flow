import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from .support import DatabaseTestCase, bundle_arguments
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
