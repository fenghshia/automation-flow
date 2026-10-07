from dataclasses import replace
from unittest.mock import patch

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from video_filter.tests.support import DatabaseTestCase, bundle_arguments
from app import db
from video_filter.feature_store import FeatureStore
from video_filter.models import Asset, FeatureBundle


class FeatureStoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.store = FeatureStore()
        self.asset, self.variant, self.config = self.identities()
        self.arguments = bundle_arguments(self.asset.id, self.variant.id)

    def count(self):
        return db.session.scalar(select(db.func.count()).select_from(FeatureBundle))

    def test_commit_read_and_keep_after_negative_feedback(self):
        prepared = self.store.prepare(**self.arguments)
        self.assertEqual(0, self.count())
        stored = self.store.save(db.session, prepared)
        self.asset.label, self.asset.label_revision = 0, 1
        db.session.commit()
        checked = self.store.require_ready(db.session, stored.bundle_id)
        self.assertEqual(prepared.manifest_sha256, checked.manifest_sha256)
        self.assertNotIn("label", checked.manifest)
        record = db.session.get(FeatureBundle, stored.bundle_id)
        self.assertEqual(prepared.arrays_blob, record.arrays_blob)
        self.assertEqual(prepared.manifest, record.manifest)
        self.assertIsNone(record.relative_path)
        np.testing.assert_array_equal(self.arguments["vectors"]["dino"], checked.arrays["dino"])

    def test_idempotent_save_and_conflicting_save(self):
        prepared = self.store.prepare(**self.arguments)
        first = self.store.save(db.session, prepared)
        second = self.store.save(db.session, prepared)
        self.assertEqual(first.bundle_id, second.bundle_id)
        self.assertEqual(1, self.count())
        self.arguments["vectors"]["dino"][:] = 2
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.save(db.session, self.store.prepare(**self.arguments))
        checked = self.store.require_ready(db.session, first.bundle_id)
        self.assertEqual(prepared.manifest_sha256, checked.manifest_sha256)

    def test_serialization_failure_does_not_write_database(self):
        with patch("video_filter.feature_store.np.savez_compressed", side_effect=OSError("memory failure")):
            with self.assertRaises(OSError):
                self.store.prepare(**self.arguments)
        self.assertEqual(0, self.count())

    def test_db_failure_rolls_back_blob_and_ready_state_and_allows_retry(self):
        prepared = self.store.prepare(**self.arguments)
        with patch.object(db.session, "commit", side_effect=RuntimeError("DB unavailable")):
            with self.assertRaises(RuntimeError):
                self.store.save(db.session, prepared)
        self.assertEqual(0, self.count())
        recovered = self.store.save(db.session, prepared)
        self.assertEqual(prepared.manifest_sha256, recovered.manifest_sha256)

    def test_failed_commit_preserves_existing_extracting_record(self):
        prepared = self.store.prepare(**self.arguments)
        record = FeatureBundle(variant_id=self.variant.id, feature_signature=prepared.manifest["feature_signature"])
        db.session.add(record)
        db.session.commit()
        record_id = record.id
        with patch.object(db.session, "commit", side_effect=RuntimeError("DB unavailable")):
            with self.assertRaises(RuntimeError):
                self.store.save(db.session, prepared)
        checked = db.session.get(FeatureBundle, record_id)
        self.assertEqual("extracting", checked.status)
        self.assertIsNone(checked.arrays_blob)
        self.assertIsNone(checked.manifest)

    def test_blob_corruption_blocks_ready_gate(self):
        stored = self.store.save(db.session, self.store.prepare(**self.arguments))
        record = db.session.get(FeatureBundle, stored.bundle_id)
        record.arrays_blob = b"corrupted"
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.require_ready(db.session, stored.bundle_id)

    def test_manifest_corruption_blocks_ready_gate(self):
        stored = self.store.save(db.session, self.store.prepare(**self.arguments))
        record = db.session.get(FeatureBundle, stored.bundle_id)
        record.manifest = {**record.manifest, "duration_seconds": 13.0}
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.require_ready(db.session, stored.bundle_id)

    def test_source_version_mismatch_rejected(self):
        self.arguments["source_sha256"] = "d" * 64
        prepared = self.store.prepare(**self.arguments)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.store.save(db.session, prepared)
        self.assertEqual(0, self.count())

    def test_no_audio_is_complete_but_failed_audio_is_not(self):
        absent = bundle_arguments(self.asset.id, self.variant.id, no_audio=True)
        stored = self.store.save(db.session, self.store.prepare(**absent))
        self.assertEqual("no_audio", stored.manifest["audio_status"])
        absent["audio_status"] = "present"
        with self.assertRaisesRegex(ValueError, "Audio embedding"):
            self.store.prepare(**absent)

    def test_explicit_missing_audio_window_survives_database_roundtrip(self):
        arguments = bundle_arguments(self.asset.id, self.variant.id)
        for name in ("beats", "egemaps"):
            arguments["vectors"][name][1] = 0
            arguments["validity"][name][1] = False
        arguments["audio_missing_windows"] = [1]
        stored = self.store.save(db.session, self.store.prepare(**arguments))
        ready = self.store.require_ready(db.session, stored.bundle_id)
        self.assertEqual([1], ready.manifest["audio_missing_windows"])
        self.assertFalse(ready.arrays["beats_valid"][1].any())
        np.testing.assert_array_equal(ready.arrays["beats_mean"], arguments["vectors"]["beats"][0])

    def test_missing_audio_evidence_cannot_hide_valid_audio_or_failed_embeddings(self):
        for missing in ([1], [True], [2], [1, 1], [1, 0], "1"):
            arguments = bundle_arguments(self.asset.id, self.variant.id)
            arguments["audio_missing_windows"] = missing
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.store.prepare(**arguments)
        arguments = bundle_arguments(self.asset.id, self.variant.id)
        arguments["audio_missing_windows"] = [1]
        arguments["vectors"]["beats"][1] = 0
        arguments["validity"]["beats"][1] = False
        with self.assertRaisesRegex(ValueError, "Missing audio windows"):
            self.store.prepare(**arguments)

    def test_nan_features_missing_modality_and_incomplete_windows_rejected(self):
        for change in ("nan", "missing", "windows", "visual-mask"):
            arguments = bundle_arguments(self.asset.id, self.variant.id)
            if change == "nan":
                arguments["vectors"]["dino"][0, 0] = np.nan
            elif change == "missing":
                del arguments["vectors"]["beats"]
            elif change == "windows":
                arguments["windows"] = [[0, 9], [10, 12]]
            else:
                arguments["validity"]["dino"][0, 0] = False
                arguments["vectors"]["dino"][0, 0] = 0
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.prepare(**arguments)

    def test_egemaps_invalid_values_excluded_from_mean(self):
        self.arguments["vectors"]["egemaps"][0, 0] = 0
        self.arguments["vectors"]["egemaps"][1, 0] = 4
        self.arguments["validity"]["egemaps"][0, 0] = False
        stored = self.store.save(db.session, self.store.prepare(**self.arguments))
        self.assertEqual(4, stored.arrays["egemaps_mean"][0])
        self.assertEqual(0, stored.arrays["egemaps_std"][0])

    def test_missing_summary_rejected(self):
        with self.assertRaises(ValueError):
            self.store.require_ready(db.session, self.variant.id)

    def test_database_rejects_legacy_file_reference_as_ready(self):
        record = FeatureBundle(variant_id=self.variant.id, feature_signature="a" * 64,
                              status="ready", relative_path="bundles/test", manifest_sha256="b" * 64,
                              windows=2, modality_validity={})
        db.session.add(record)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_flushed_but_uncommitted_blob_cannot_satisfy_ready_gate(self):
        prepared = self.store.prepare(**self.arguments)
        record = FeatureBundle(
            variant_id=self.variant.id, feature_signature=prepared.manifest["feature_signature"],
            status="ready", arrays_blob=prepared.arrays_blob, manifest=prepared.manifest,
            payload_format="npz-v1", arrays_sha256=prepared.manifest["arrays_sha256"],
            manifest_sha256=prepared.manifest_sha256, windows=2,
            modality_validity=prepared.manifest["modality_validity"],
        )
        db.session.add(record)
        db.session.flush()
        with self.assertRaisesRegex(ValueError, "committed"):
            self.store.require_ready(db.session, record.id)
        db.session.commit()
        self.assertEqual(prepared.manifest_sha256, self.store.require_ready(db.session, record.id).manifest_sha256)

    def test_asset_deletion_cannot_cascade_into_summaries(self):
        stored = self.store.save(db.session, self.store.prepare(**self.arguments))
        db.session.delete(self.asset)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertIsNotNone(db.session.get(Asset, self.asset.id))
        self.assertEqual(stored.manifest_sha256, self.store.require_ready(db.session, stored.bundle_id).manifest_sha256)

    def test_tampered_worker_output_cannot_be_saved(self):
        prepared = self.store.prepare(**self.arguments)
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.save(db.session, replace(prepared, arrays_blob=b"corrupted"))
        self.assertEqual(0, self.count())

    def test_database_uses_bytea_on_postgresql(self):
        self.assertEqual("BYTEA", FeatureBundle.arrays_blob.type.compile(dialect=postgresql.dialect()))
