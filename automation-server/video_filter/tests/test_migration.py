import importlib.util
from pathlib import Path
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from video_filter.tests.support import DatabaseTestCase
from app import db


class MigrationTests(DatabaseTestCase):
    def migration(self, filename):
        path = Path(__file__).parents[2] / "migrations" / "versions" / filename
        spec = importlib.util.spec_from_file_location("video_filter_test_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def chain(self):
        return [self.migration(name) for name in (
            "f3a81c9d6024_add_video_filter_foundation.py",
            "b6c20d8e7419_store_video_filter_features_in_database.py",
            "d9e41b7a2036_add_video_filter_workflow.py",
            "a7d52e9c1048_add_dual_classifiers_and_outcomes.py",
        )]

    def test_isolated_upgrade_matches_models_and_downgrade_preserves_other_tables(self):
        migrations = self.chain() + [self.migration("c4f18a2d9076_group_video_filter_and_resources.py")]
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE unrelated_test_data (value TEXT)")
                connection.exec_driver_sql("INSERT INTO unrelated_test_data VALUES ('preserved')")
                context = MigrationContext.configure(connection)
                for migration in migrations:
                    with patch.object(migration, "op", Operations(context)):
                        migration.upgrade()
                context = MigrationContext.configure(connection, opts={
                    "include_object": lambda obj, name, kind, reflected, compared:
                    name != "unrelated_test_data" if kind == "table" else True,
                })
                self.assertEqual([], compare_metadata(context, db.metadata))
                inspector = inspect(connection)
                for table in db.metadata.sorted_tables:
                    expected = {constraint.name: str(constraint.sqltext)
                                for constraint in table.constraints if hasattr(constraint, "sqltext")}
                    actual = {constraint["name"]: constraint["sqltext"]
                              for constraint in inspector.get_check_constraints(table.name)}
                    self.assertEqual(expected, actual, table.name)
                for migration in reversed(migrations):
                    with patch.object(migration, "op", Operations(context)):
                        migration.downgrade()
                self.assertEqual(["unrelated_test_data"], inspect(connection).get_table_names())
                self.assertEqual("preserved", connection.exec_driver_sql("SELECT value FROM unrelated_test_data").scalar_one())
        finally:
            engine.dispose()

    def test_upgrade_refuses_legacy_ready_summary_without_changing_schema(self):
        foundation, binary = self.chain()[:2]
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                with patch.object(foundation, "op", operations):
                    foundation.upgrade()
                connection.exec_driver_sql(
                    "INSERT INTO video_filter_feature_bundle "
                    "(id, variant_id, feature_signature, created_at, status, relative_path, manifest_sha256, windows, modality_validity) "
                    "VALUES ('fixture', 'variant-fixture', 'test-signature', '2026-01-01', 'ready', 'legacy/example', 'test-hash', 1, '{}')"
                )
                with patch.object(binary, "op", operations), self.assertRaisesRegex(RuntimeError, "Legacy"):
                    binary.upgrade()
                columns = {column["name"] for column in inspect(connection).get_columns("video_filter_feature_bundle")}
                self.assertNotIn("arrays_blob", columns)
                self.assertEqual("legacy/example", connection.exec_driver_sql("SELECT relative_path FROM video_filter_feature_bundle").scalar_one())
        finally:
            engine.dispose()

    def test_downgrade_refuses_to_drop_database_payload(self):
        foundation, binary = self.chain()[:2]
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                for migration in (foundation, binary):
                    with patch.object(migration, "op", operations):
                        migration.upgrade()
                connection.exec_driver_sql(
                    "INSERT INTO video_filter_feature_bundle "
                    "(id, variant_id, feature_signature, created_at, status, arrays_blob) "
                    "VALUES ('fixture', 'variant-fixture', 'test-signature', '2026-01-01', 'extracting', ?)",
                    (b"test-preserved",),
                )
                with patch.object(binary, "op", operations), self.assertRaisesRegex(RuntimeError, "Export"):
                    binary.downgrade()
                self.assertEqual(b"test-preserved", connection.exec_driver_sql("SELECT arrays_blob FROM video_filter_feature_bundle").scalar_one())
        finally:
            engine.dispose()

    def test_dual_upgrade_preserves_existing_lr_and_downgrade_refuses_mil(self):
        migrations = self.chain()
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                for migration in migrations[:-1]:
                    with patch.object(migration, "op", operations):
                        migration.upgrade()
                connection.exec_driver_sql(
                    "INSERT INTO video_filter_model_run "
                    "(id, created_at, feature_signature, dataset_snapshot, validation, threshold, model_blob, sha256, status, active_slot) "
                    "VALUES ('existing-lr', '2026-01-01', 'fixture', '[]', '{}', 0.5, ?, 'fixture', 'active', 'active')",
                    (b"existing-numerical-parameters",))
                dual = migrations[-1]
                with patch.object(dual, "op", operations):
                    dual.upgrade()
                self.assertEqual(("logistic_regression", "active", b"existing-numerical-parameters"),
                    tuple(connection.exec_driver_sql("SELECT model_type, active_slot, model_blob FROM video_filter_model_run").one()))
                connection.exec_driver_sql(
                    "INSERT INTO video_filter_model_run "
                    "(id, created_at, feature_signature, dataset_snapshot, model_type, status) "
                    "VALUES ('mil-fixture', '2026-01-02', 'fixture', '[]', 'mil', 'validated')")
                with patch.object(dual, "op", operations), self.assertRaisesRegex(RuntimeError, "Export"):
                    dual.downgrade()
                self.assertEqual(2, connection.exec_driver_sql("SELECT COUNT(*) FROM video_filter_model_run").scalar_one())
        finally:
            engine.dispose()

    def test_grouped_upgrade_refuses_residual_data_and_preserves_public_lineage(self):
        grouped = self.migration("c4f18a2d9076_group_video_filter_and_resources.py")
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                connection.exec_driver_sql("CREATE TABLE video_compression_mission (id INTEGER PRIMARY KEY, payload TEXT)")
                connection.exec_driver_sql("INSERT INTO video_compression_mission VALUES (7, 'preserved')")
                for migration in self.chain():
                    with patch.object(migration, "op", operations):
                        migration.upgrade()
                connection.exec_driver_sql("INSERT INTO video_filter_asset (id,created_at,label_revision) VALUES ('old','2026-01-01',0)")
                with patch.object(grouped, "op", operations), self.assertRaisesRegex(RuntimeError, "clear video_filter"):
                    grouped.upgrade()
                self.assertNotIn("dataset_group_id", {c["name"] for c in inspect(connection).get_columns("video_filter_asset")})
                connection.exec_driver_sql("DELETE FROM video_filter_asset")
                connection.exec_driver_sql("INSERT INTO media_lineage_event (operation_id,sequence,producer,generation,phase,source_sha256,source_path,destination_path,created_at,evidence) VALUES ('old-operation',0,'video_compression','old-generation','planned','old-hash','source-fixture','target-fixture','2026-01-01','{}')")
                with patch.object(grouped, "op", operations):
                    grouped.upgrade()
                self.assertIn("pinned_output_directory", {c["name"] for c in inspect(connection).get_columns("video_compression_mission")})
                self.assertEqual("preserved", connection.exec_driver_sql("SELECT payload FROM video_compression_mission").scalar_one())
                self.assertEqual(1, connection.exec_driver_sql("SELECT COUNT(*) FROM media_lineage_event").scalar_one())
                connection.exec_driver_sql("INSERT INTO video_filter_dataset_group (id,name,reset_epoch,lineage_floor,enabled) VALUES ('group','fixture','epoch',1,1)")
                connection.exec_driver_sql("INSERT INTO video_filter_asset (id,created_at,label_revision,dataset_group_id,reset_epoch) VALUES ('new','2026-01-01',0,'group','epoch')")
                with patch.object(grouped, "op", operations), self.assertRaisesRegex(RuntimeError, "clear video_filter"):
                    grouped.downgrade()
                self.assertIn("dataset_group_id", {c["name"] for c in inspect(connection).get_columns("video_filter_asset")})
                connection.exec_driver_sql("DELETE FROM video_filter_asset")
                with patch.object(grouped, "op", operations):
                    grouped.downgrade()
                self.assertEqual("preserved", connection.exec_driver_sql("SELECT payload FROM video_compression_mission").scalar_one())
                self.assertNotIn("workflow_id", {c["name"] for c in inspect(connection).get_columns("video_compression_mission")})
        finally:
            engine.dispose()
