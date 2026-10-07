import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text

from tools import reset_failed_media as reset


class ResetMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        server_patch = patch.object(reset, "SERVER", self.root)
        server_patch.start()
        self.addCleanup(server_patch.stop)
        self.settings = {"name": "example_group", "compression_enabled": True,
                         "directories": {role: self.root / role for role in
                         ("unclassified", "liked", "predicted_like", "predicted_dislike", "liked_source")}}
        for directory in self.settings["directories"].values():
            directory.mkdir()
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        definitions = {
            "video_filter_dataset_group": "id TEXT, name TEXT, reset_epoch TEXT, enabled BOOLEAN",
            "video_filter_config_revision": "id TEXT, dataset_group_id TEXT, reset_epoch TEXT, status TEXT, active_slot TEXT, snapshot TEXT",
            "video_filter_asset": "id TEXT, dataset_group_id TEXT, reset_epoch TEXT, label_revision INTEGER",
            "video_filter_location": "id TEXT, variant_id TEXT, dataset_group_id TEXT, reset_epoch TEXT, status TEXT, role TEXT, current_path_key TEXT, path TEXT, size_bytes BIGINT, modified_ns BIGINT, file_identity TEXT",
            "video_filter_task": "id TEXT PRIMARY KEY, dataset_group_id TEXT, reset_epoch TEXT, config_revision_id TEXT, variant_id TEXT, asset_id TEXT, kind TEXT, status TEXT, attempts INTEGER, error_code TEXT, input_snapshot TEXT, claim_token TEXT, claimed_at TEXT, heartbeat_at TEXT, execution_owner TEXT, finished_at TEXT",
            "video_filter_feature_bundle": "id TEXT, dataset_group_id TEXT, reset_epoch TEXT, variant_id TEXT, status TEXT, feature_signature TEXT",
            "video_filter_transfer_operation": "id TEXT, dataset_group_id TEXT, reset_epoch TEXT, variant_id TEXT, status TEXT",
            "media_workflow_binding": "id TEXT, name TEXT, reset_epoch TEXT, directory_revision_id TEXT, enabled BOOLEAN, source_directory TEXT, destination_directory TEXT",
            "video_compression_mission": "id INTEGER PRIMARY KEY, status TEXT, source_path TEXT, file_name TEXT, size_bytes BIGINT, modified_ns BIGINT, stable_checks INTEGER, attempts INTEGER, error_message TEXT, workflow_id TEXT, reset_epoch TEXT, directory_revision_id TEXT, pinned_output_directory TEXT, output_path TEXT, updated_at TEXT",
        }
        for name, columns in definitions.items():
            self.connection.execute(text(f"CREATE TABLE {name} ({columns})"))
        self.insert("video_filter_dataset_group", id="g", name="example_group", reset_epoch="epoch", enabled=True)
        self.insert("video_filter_config_revision", id="config", dataset_group_id="g", reset_epoch="epoch",
                    status="active", active_slot="active", snapshot=json.dumps({"name": "example_group",
                    "directories": {r: reset.normalized(p) for r, p in self.settings["directories"].items()}}))
        self.insert("media_workflow_binding", id="g", name="example_group", reset_epoch="epoch",
                    directory_revision_id="config", enabled=True,
                    source_directory=str(self.settings["directories"]["liked_source"]),
                    destination_directory=str(self.settings["directories"]["liked"]))
        self.connection.commit()

    def insert(self, table, **values):
        columns = ",".join(values)
        parameters = ",".join(":" + key for key in values)
        self.connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({parameters})"), values)

    def extraction(self):
        path = self.settings["directories"]["unclassified"] / "example.mp4"
        path.write_bytes(b"test-media")
        stat = reset.snapshot(path)
        self.insert("video_filter_asset", id="asset", dataset_group_id="g", reset_epoch="epoch", label_revision=0)
        self.insert("video_filter_location", id="location", variant_id="variant", dataset_group_id="g",
                    reset_epoch="epoch", status="present", role="unclassified", current_path_key="key",
                    path=str(path), size_bytes=stat["size_bytes"], modified_ns=stat["modified_ns"],
                    file_identity=json.dumps(stat["file_identity"]))
        self.insert("video_filter_task", id="task", dataset_group_id="g", reset_epoch="epoch",
                    config_revision_id="config", variant_id="variant", asset_id="asset", kind="extract",
                    status="failed", attempts=3, error_code="extraction_worker_failed", claim_token="old-claim",
                    heartbeat_at="old-heartbeat", execution_owner='{}', finished_at="old-finish",
                    input_snapshot=json.dumps({"path": str(path), "source_snapshot": stat,
                                               "label_revision": 0, "feature_signature": "signature"}))
        self.connection.commit()
        return path

    def compression(self, legacy=False):
        path = self.settings["directories"]["liked_source"] / "example.mp4"
        path.write_bytes(b"test-media")
        stat = reset.snapshot(path)
        self.insert("video_compression_mission", id=7, status="failed", source_path=str(path), file_name=path.name,
                    size_bytes=stat["size_bytes"], modified_ns=stat["modified_ns"], stable_checks=1,
                    attempts=5, error_message="test failure", workflow_id=None if legacy else "g",
                    reset_epoch=None if legacy else "epoch", directory_revision_id=None if legacy else "config",
                    pinned_output_directory=None if legacy else str(self.settings["directories"]["liked"]),
                    output_path=str(self.settings["directories"]["liked"] / path.name))
        self.connection.commit()
        return path

    def plan(self, **options):
        return reset.build_plan(self.connection, self.settings, **options)

    def test_import_has_no_application_side_effect(self):
        self.assertNotIn("app", sys.modules)

    def test_preview_does_not_write(self):
        self.extraction()
        targets, skipped = self.plan(pipeline="extract")
        self.assertEqual(len(targets), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(self.connection.scalar(text("SELECT status FROM video_filter_task")), "failed")

    def test_extraction_gets_new_budget_and_preserves_inputs(self):
        self.extraction()
        original = self.connection.scalar(text("SELECT input_snapshot FROM video_filter_task"))
        targets, _ = self.plan(pipeline="extract")
        reset.apply_targets(self.connection, targets)
        task = reset.rows(self.connection, "SELECT * FROM video_filter_task")[0]
        self.assertEqual((task["status"], task["attempts"]), ("queued", 0))
        self.assertEqual(task["input_snapshot"], original)
        for key in ("error_code", "claim_token", "heartbeat_at", "execution_owner", "finished_at"):
            self.assertIsNone(task[key])

    def test_compression_preserves_generation_and_output_ownership(self):
        path = self.compression()
        output = self.settings["directories"]["liked"] / path.name
        output.write_bytes(b"already-published")
        targets, skipped = self.plan(pipeline="compression")
        reset.apply_targets(self.connection, targets)
        task = reset.rows(self.connection, "SELECT * FROM video_compression_mission")[0]
        self.assertEqual(skipped, [])
        self.assertEqual((task["status"], task["attempts"], task["output_path"]), ("processing", 5, str(output)))
        self.assertEqual(output.read_bytes(), b"already-published")

    def test_legacy_requires_opt_in_and_binds_only_without_artifacts(self):
        self.compression(legacy=True)
        targets, skipped = self.plan(pipeline="compression")
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]["reason"], "legacy_requires_include_legacy")
        targets, skipped = self.plan(pipeline="compression", include_legacy=True)
        reset.apply_targets(self.connection, targets)
        task = reset.rows(self.connection, "SELECT * FROM video_compression_mission")[0]
        self.assertEqual((task["status"], task["attempts"], task["workflow_id"]), ("waiting_stable", 5, "g"))
        self.assertEqual(task["directory_revision_id"], "config")
        self.assertIsNone(task["output_path"])

    def test_legacy_output_blocks_adoption(self):
        path = self.compression(legacy=True)
        (self.settings["directories"]["liked"] / path.name).write_bytes(b"preserve")
        targets, skipped = self.plan(pipeline="compression", include_legacy=True)
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]["reason"], "legacy_artifact_requires_manual_review")

    def test_changed_source_is_skipped(self):
        path = self.extraction()
        path.write_bytes(b"changed-video")
        targets, skipped = self.plan(pipeline="extract")
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]["reason"], "source_snapshot_changed")

    def test_existing_summary_and_conflict_are_not_reset(self):
        self.extraction()
        self.insert("video_filter_feature_bundle", id="summary", dataset_group_id="g", reset_epoch="epoch",
                    variant_id="variant", status="ready", feature_signature="signature")
        targets, skipped = self.plan(pipeline="extract")
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]["reason"], "ready_summary_exists")
        self.connection.execute(text("DELETE FROM video_filter_feature_bundle"))
        self.insert("video_filter_transfer_operation", id="operation", dataset_group_id="g", reset_epoch="epoch",
                    variant_id="variant", status="conflict")
        targets, skipped = self.plan(pipeline="extract")
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]["reason"], "transfer_conflict")

    def test_transaction_rolls_back_all_updates_if_later_source_changes(self):
        self.extraction()
        compression_path = self.compression()
        targets, _ = self.plan()
        self.connection.rollback()
        compression_path.write_bytes(b"changed-media")
        with self.assertRaises(reset.ResetError), self.connection.begin():
            reset.apply_targets(self.connection, targets)
        self.assertEqual(self.connection.scalar(text("SELECT status FROM video_filter_task")), "failed")
        self.assertEqual(self.connection.scalar(text("SELECT status FROM video_compression_mission")), "failed")

    def test_old_epoch_and_changed_configuration_are_rejected(self):
        self.compression()
        self.connection.execute(text("UPDATE video_compression_mission SET reset_epoch='old'"))
        targets, skipped = self.plan(pipeline="compression")
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]["reason"], "mission_binding_changed")
        self.settings["directories"]["unclassified"] = self.root / "different"
        with self.assertRaisesRegex(reset.ResetError, "current_configuration_not_reconciled"):
            self.plan()

    def test_apply_requires_explicit_stopped_server_flag_before_config_load(self):
        with patch.object(reset.EnvConfig, "video_filter_settings", side_effect=AssertionError("must not load")):
            with self.assertRaises(SystemExit):
                reset.main(["--group", "example_group", "--apply"])

    def test_backup_uses_private_directory_and_retains_error(self):
        self.extraction()
        targets, _ = self.plan(pipeline="extract")
        with patch.object(reset, "SERVER", self.root):
            name = reset.write_backup(targets)
        report = json.loads((self.root / "private" / "task-resets" / name).read_text(encoding="utf-8"))
        self.assertEqual(report["tasks"][0]["old"]["error_code"], "extraction_worker_failed")


if __name__ == "__main__":
    unittest.main()
