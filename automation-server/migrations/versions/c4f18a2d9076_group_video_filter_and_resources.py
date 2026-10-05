"""Require explicit legacy cleanup; rebuild empty filter schema with grouped constraints.

Shared lineage and compression rows are preserved. No production cleanup is run
by this migration. SQL cleanup is delivered in plan/video_filter.
"""
from alembic import op
import sqlalchemy as sa

revision = "c4f18a2d9076"
down_revision = "a7d52e9c1048"
branch_labels = None
depends_on = None
BUSINESS_TABLES = ["video_filter_prediction_outcome","video_filter_transfer_operation","video_filter_prediction","video_filter_task","video_filter_feedback_event","video_filter_feature_bundle","video_filter_location","video_filter_observation","video_filter_scan_run","video_filter_model_run","video_filter_variant","video_filter_asset","video_filter_config_revision"]

def _empty(names):
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE " + ", ".join(names) + " IN ACCESS EXCLUSIVE MODE"))
    for name in names:
        if bind.execute(sa.text("SELECT COUNT(*) FROM " + name)).scalar_one():
            raise RuntimeError("Export/clear video_filter data explicitly before grouped schema changes: " + name)

def upgrade():
    _empty(BUSINESS_TABLES)
    for name in BUSINESS_TABLES:
        op.drop_table(name)
    _create_grouped_1()
    _create_grouped_2()
    _create_grouped_3()
    if sa.inspect(op.get_bind()).has_table("video_compression_mission"):
        with op.batch_alter_table("video_compression_mission") as batch:
            batch.add_column(sa.Column("workflow_id", sa.String(36), nullable=True))
            batch.add_column(sa.Column("reset_epoch", sa.String(36), nullable=True))
            batch.add_column(sa.Column("directory_revision_id", sa.String(36), nullable=True))
            batch.add_column(sa.Column("pinned_output_directory", sa.Text(), nullable=True))
            batch.create_index("ix_video_compression_mission_workflow_id", ["workflow_id"])

def downgrade():
    _empty(BUSINESS_TABLES + ["media_resource_lease", "media_workflow_binding"])
    if sa.inspect(op.get_bind()).has_table("video_compression_mission"):
        if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM video_compression_mission WHERE workflow_id IS NOT NULL")).scalar_one():
            raise RuntimeError("Export grouped compression missions before downgrading")
        with op.batch_alter_table("video_compression_mission") as batch:
            batch.drop_index("ix_video_compression_mission_workflow_id")
            for name in ("pinned_output_directory", "directory_revision_id", "reset_epoch", "workflow_id"):
                batch.drop_column(name)
    for name in BUSINESS_TABLES + ["video_filter_dataset_group", "video_filter_runtime_state", "media_workflow_binding", "media_resource_lease"]:
        op.drop_table(name)
    _create_legacy_1()
    _create_legacy_2()


def _create_grouped_1():
    op.create_table('media_resource_lease',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('device', sa.String(length=128), nullable=False),
    sa.Column('mode', sa.String(length=32), nullable=False),
    sa.Column('owner', sa.String(length=128), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=False),
    sa.Column('process_identity', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("mode IN ('extract_shared', 'exclusive_train', 'exclusive_compression')", name='ml_resource_mode'),
    sa.CheckConstraint("status IN ('waiting', 'active', 'released', 'conflict')", name='ml_resource_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner')
    )
    op.create_index(op.f('ix_media_resource_lease_device'), 'media_resource_lease', ['device'], unique=False)
    op.create_table('media_workflow_binding',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('directory_revision_id', sa.String(length=36), nullable=False),
    sa.Column('source_directory', sa.Text(), nullable=False),
    sa.Column('destination_directory', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('video_filter_dataset_group',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('lineage_floor', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('video_filter_runtime_state',
    sa.Column('id', sa.String(length=16), nullable=False),
    sa.Column('epoch', sa.String(length=36), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('video_filter_asset',
    sa.Column('label', sa.Integer(), nullable=True),
    sa.Column('label_revision', sa.Integer(), nullable=False),
    sa.Column('label_event_id', sa.String(length=36), nullable=True),
    sa.Column('label_updated_at', sa.DateTime(), nullable=True),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('label IS NULL OR label IN (0, 1)', name='vf_asset_label'),
    sa.CheckConstraint('label_revision >= 0', name='vf_asset_revision'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_asset')
    )
    op.create_table('video_filter_config_revision',
    sa.Column('signature', sa.String(length=64), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('active_slot', sa.String(length=16), nullable=True),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("active_slot IS NULL OR active_slot = 'active'", name='vf_config_slot'),
    sa.CheckConstraint("status IN ('pending', 'active', 'retired')", name='vf_config_status'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'active_slot', name='vf_group_config_revision_active_slot'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_config_revision'),
    sa.UniqueConstraint('dataset_group_id', 'signature', name='vf_group_config_revision_signature')
    )


def _create_grouped_2():
    op.create_table('video_filter_model_run',
    sa.Column('model_type', sa.String(length=24), server_default='logistic_regression', nullable=False),
    sa.Column('feature_signature', sa.String(length=64), nullable=False),
    sa.Column('dataset_snapshot', sa.JSON(), nullable=False),
    sa.Column('hyperparameters', sa.JSON(), nullable=False),
    sa.Column('training_config_digest', sa.String(length=64), nullable=True),
    sa.Column('validation', sa.JSON(), nullable=True),
    sa.Column('threshold', sa.Float(), nullable=True),
    sa.Column('relative_path', sa.Text(), nullable=True),
    sa.Column('model_blob', sa.LargeBinary(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('active_slot', sa.String(length=16), nullable=True),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("active_slot IS NULL OR (model_type = 'logistic_regression' AND active_slot = 'active') OR (model_type = 'mil' AND active_slot = 'mil')", name='vf_model_slot'),
    sa.CheckConstraint("model_type IN ('logistic_regression', 'mil')", name='vf_model_type'),
    sa.CheckConstraint("status != 'active' OR (active_slot IS NOT NULL AND model_blob IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)", name='vf_model_active'),
    sa.CheckConstraint("status IN ('training', 'validated', 'active', 'retired', 'failed')", name='vf_model_status'),
    sa.CheckConstraint('threshold IS NULL OR (threshold >= 0 AND threshold <= 1)', name='vf_model_threshold'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'active_slot', name='vf_group_model_run_active_slot'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_model_run')
    )
    op.create_table('video_filter_feedback_event',
    sa.Column('event_key', sa.String(length=64), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('label', sa.Integer(), nullable=False),
    sa.Column('expected_revision', sa.Integer(), nullable=False),
    sa.Column('resulting_revision', sa.Integer(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('label IN (0, 1)', name='vf_feedback_label'),
    sa.ForeignKeyConstraint(['asset_id'], ['video_filter_asset.id'], ),
    sa.ForeignKeyConstraint(['dataset_group_id', 'asset_id'], ['video_filter_asset.dataset_group_id', 'video_filter_asset.id'], name='vf_scope_feedback_event_asset_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asset_id', 'resulting_revision', name='vf_feedback_asset_revision'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_feedback_event'),
    sa.UniqueConstraint('event_key')
    )
    op.create_table('video_filter_observation',
    sa.Column('config_revision_id', sa.String(length=36), nullable=False),
    sa.Column('path_key', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('modified_ns', sa.BigInteger(), nullable=False),
    sa.Column('file_identity', sa.JSON(), nullable=False),
    sa.Column('stable_since', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('baseline_entry', sa.Boolean(), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_revision_id'], ['video_filter_config_revision.id'], ),
    sa.ForeignKeyConstraint(['dataset_group_id', 'config_revision_id'], ['video_filter_config_revision.dataset_group_id', 'video_filter_config_revision.id'], name='vf_scope_observation_config_revision_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('config_revision_id', 'path_key', name='vf_observation_path'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_observation')
    )
    op.create_table('video_filter_scan_run',
    sa.Column('config_revision_id', sa.String(length=36), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('role_results', sa.JSON(), nullable=False),
    sa.Column('complete', sa.Boolean(), nullable=False),
    sa.Column('lineage_watermark', sa.BigInteger(), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_revision_id'], ['video_filter_config_revision.id'], ),
    sa.ForeignKeyConstraint(['dataset_group_id', 'config_revision_id'], ['video_filter_config_revision.dataset_group_id', 'video_filter_config_revision.id'], name='vf_scope_scan_run_config_revision_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_scan_run')
    )
    op.create_table('video_filter_variant',
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('media_metadata', sa.JSON(), nullable=False),
    sa.Column('source_variant_id', sa.String(length=36), nullable=True),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('size_bytes > 0', name='vf_variant_size'),
    sa.ForeignKeyConstraint(['asset_id'], ['video_filter_asset.id'], ),
    sa.ForeignKeyConstraint(['dataset_group_id', 'asset_id'], ['video_filter_asset.dataset_group_id', 'video_filter_asset.id'], name='vf_scope_variant_asset_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'source_variant_id'], ['video_filter_variant.dataset_group_id', 'video_filter_variant.id'], name='vf_scope_variant_source_variant_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['source_variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_variant'),
    sa.UniqueConstraint('dataset_group_id', 'sha256', name='vf_group_variant_sha256')
    )
    op.create_index(op.f('ix_video_filter_variant_asset_id'), 'video_filter_variant', ['asset_id'], unique=False)
    op.create_table('video_filter_feature_bundle',
    sa.Column('variant_id', sa.String(length=36), nullable=False),
    sa.Column('feature_signature', sa.String(length=64), nullable=False),
    sa.Column('relative_path', sa.Text(), nullable=True),
    sa.Column('arrays_blob', sa.LargeBinary(), nullable=True),
    sa.Column('arrays_sha256', sa.String(length=64), nullable=True),
    sa.Column('manifest', sa.JSON(), nullable=True),
    sa.Column('payload_format', sa.String(length=24), nullable=True),
    sa.Column('manifest_sha256', sa.String(length=64), nullable=True),
    sa.Column('windows', sa.Integer(), nullable=True),
    sa.Column('modality_validity', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status != 'ready' OR (arrays_blob IS NOT NULL AND arrays_sha256 IS NOT NULL AND manifest IS NOT NULL AND payload_format IS NOT NULL AND payload_format = 'npz-v1' AND manifest_sha256 IS NOT NULL AND windows IS NOT NULL AND windows > 0 AND modality_validity IS NOT NULL)", name='vf_bundle_ready'),
    sa.CheckConstraint("status IN ('extracting', 'ready', 'failed')", name='vf_bundle_status'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'variant_id'], ['video_filter_variant.dataset_group_id', 'video_filter_variant.id'], name='vf_scope_feature_bundle_variant_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_feature_bundle'),
    sa.UniqueConstraint('variant_id', 'feature_signature', name='vf_bundle_variant_signature')
    )


def _create_grouped_3():
    op.create_table('video_filter_location',
    sa.Column('variant_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('current_path_key', sa.String(length=64), nullable=True),
    sa.Column('file_identity', sa.JSON(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('modified_ns', sa.BigInteger(), nullable=False),
    sa.Column('last_scan_id', sa.String(length=36), nullable=True),
    sa.Column('missing_since', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("role IN ('confirmed_like', 'predicted_like', 'predicted_dislike', 'unclassified', 'compressed_like', 'liked', 'liked_source')", name='vf_location_role'),
    sa.CheckConstraint("status IN ('present', 'missing', 'retired', 'conflict')", name='vf_location_status'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'last_scan_id'], ['video_filter_scan_run.dataset_group_id', 'video_filter_scan_run.id'], name='vf_scope_location_last_scan_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'variant_id'], ['video_filter_variant.dataset_group_id', 'video_filter_variant.id'], name='vf_scope_location_variant_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['last_scan_id'], ['video_filter_scan_run.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'current_path_key', name='vf_group_location_current_path_key'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_location')
    )
    op.create_index(op.f('ix_video_filter_location_variant_id'), 'video_filter_location', ['variant_id'], unique=False)
    op.create_table('video_filter_task',
    sa.Column('dedup_key', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=True),
    sa.Column('variant_id', sa.String(length=36), nullable=True),
    sa.Column('config_revision_id', sa.String(length=36), nullable=False),
    sa.Column('input_snapshot', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('claim_token', sa.String(length=36), nullable=True),
    sa.Column('claimed_at', sa.DateTime(), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
    sa.Column('execution_owner', sa.JSON(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("kind IN ('scan', 'extract', 'train', 'predict', 'classify')", name='vf_task_kind'),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name='vf_task_status'),
    sa.CheckConstraint('attempts >= 0', name='vf_task_attempts'),
    sa.ForeignKeyConstraint(['asset_id'], ['video_filter_asset.id'], ),
    sa.ForeignKeyConstraint(['config_revision_id'], ['video_filter_config_revision.id'], ),
    sa.ForeignKeyConstraint(['dataset_group_id', 'asset_id'], ['video_filter_asset.dataset_group_id', 'video_filter_asset.id'], name='vf_scope_task_asset_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'config_revision_id'], ['video_filter_config_revision.dataset_group_id', 'video_filter_config_revision.id'], name='vf_scope_task_config_revision_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'variant_id'], ['video_filter_variant.dataset_group_id', 'video_filter_variant.id'], name='vf_scope_task_variant_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_task'),
    sa.UniqueConstraint('dedup_key')
    )
    op.create_index(op.f('ix_video_filter_task_status'), 'video_filter_task', ['status'], unique=False)
    op.create_table('video_filter_prediction',
    sa.Column('variant_id', sa.String(length=36), nullable=False),
    sa.Column('bundle_id', sa.String(length=36), nullable=False),
    sa.Column('model_id', sa.String(length=36), nullable=False),
    sa.Column('label_revision', sa.Integer(), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('predicted_label', sa.Integer(), nullable=False),
    sa.Column('prediction_batch_id', sa.String(length=36), nullable=True),
    sa.Column('threshold', sa.Float(), nullable=True),
    sa.Column('selected', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('evaluation_eligible', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('predicted_label IN (0, 1)', name='vf_prediction_label'),
    sa.CheckConstraint('score >= 0 AND score <= 1', name='vf_prediction_score'),
    sa.CheckConstraint('threshold IS NULL OR (threshold >= 0 AND threshold <= 1)', name='vf_prediction_threshold'),
    sa.ForeignKeyConstraint(['bundle_id'], ['video_filter_feature_bundle.id'], ),
    sa.ForeignKeyConstraint(['dataset_group_id', 'bundle_id'], ['video_filter_feature_bundle.dataset_group_id', 'video_filter_feature_bundle.id'], name='vf_scope_prediction_bundle_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'model_id'], ['video_filter_model_run.dataset_group_id', 'video_filter_model_run.id'], name='vf_scope_prediction_model_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'variant_id'], ['video_filter_variant.dataset_group_id', 'video_filter_variant.id'], name='vf_scope_prediction_variant_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['model_id'], ['video_filter_model_run.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_prediction'),
    sa.UniqueConstraint('prediction_batch_id', 'model_id', name='vf_prediction_group_model')
    )
    op.create_table('video_filter_prediction_outcome',
    sa.Column('prediction_id', sa.String(length=36), nullable=False),
    sa.Column('feedback_event_id', sa.String(length=36), nullable=False),
    sa.Column('actual_label', sa.Integer(), nullable=False),
    sa.Column('correct', sa.Boolean(), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('actual_label IN (0, 1)', name='vf_outcome_label'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'feedback_event_id'], ['video_filter_feedback_event.dataset_group_id', 'video_filter_feedback_event.id'], name='vf_scope_prediction_outcome_feedback_event_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'prediction_id'], ['video_filter_prediction.dataset_group_id', 'video_filter_prediction.id'], name='vf_scope_prediction_outcome_prediction_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['feedback_event_id'], ['video_filter_feedback_event.id'], ),
    sa.ForeignKeyConstraint(['prediction_id'], ['video_filter_prediction.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_prediction_outcome'),
    sa.UniqueConstraint('prediction_id', 'feedback_event_id', name='vf_outcome_prediction_feedback')
    )
    op.create_index(op.f('ix_video_filter_prediction_outcome_prediction_id'), 'video_filter_prediction_outcome', ['prediction_id'], unique=False)
    op.create_table('video_filter_transfer_operation',
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('variant_id', sa.String(length=36), nullable=False),
    sa.Column('prediction_id', sa.String(length=36), nullable=False),
    sa.Column('source_path', sa.Text(), nullable=False),
    sa.Column('destination_path', sa.Text(), nullable=False),
    sa.Column('source_sha256', sa.String(length=64), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('dataset_group_id', sa.String(length=36), nullable=False),
    sa.Column('reset_epoch', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('planned', 'destination_verified', 'published', 'source_cleaned', 'conflict')", name='vf_transfer_status'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'prediction_id'], ['video_filter_prediction.dataset_group_id', 'video_filter_prediction.id'], name='vf_scope_transfer_operation_prediction_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'task_id'], ['video_filter_task.dataset_group_id', 'video_filter_task.id'], name='vf_scope_transfer_operation_task_id'),
    sa.ForeignKeyConstraint(['dataset_group_id', 'variant_id'], ['video_filter_variant.dataset_group_id', 'video_filter_variant.id'], name='vf_scope_transfer_operation_variant_id'),
    sa.ForeignKeyConstraint(['dataset_group_id'], ['video_filter_dataset_group.id'], ),
    sa.ForeignKeyConstraint(['prediction_id'], ['video_filter_prediction.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['video_filter_task.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_group_id', 'id', name='vf_scope_id_transfer_operation'),
    sa.UniqueConstraint('task_id')
    )


def _create_legacy_1():
    op.create_table('video_filter_asset',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('label', sa.INTEGER(), nullable=True),
    sa.Column('label_revision', sa.INTEGER(), nullable=False),
    sa.Column('label_event_id', sa.VARCHAR(length=36), nullable=True),
    sa.Column('label_updated_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint('label IS NULL OR label IN (0, 1)', name='vf_asset_label'),
    sa.CheckConstraint('label_revision >= 0', name='vf_asset_revision'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('video_filter_config_revision',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('signature', sa.VARCHAR(length=64), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('status', sa.VARCHAR(length=24), nullable=False),
    sa.Column('active_slot', sa.VARCHAR(length=16), nullable=True),
    sa.CheckConstraint("active_slot IS NULL OR active_slot = 'active'", name='vf_config_slot'),
    sa.CheckConstraint("status IN ('pending', 'active', 'retired')", name='vf_config_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('active_slot', name='vf_config_active_slot'),
    sa.UniqueConstraint('signature')
    )
    op.create_table('video_filter_model_run',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('feature_signature', sa.VARCHAR(length=64), nullable=False),
    sa.Column('dataset_snapshot', sa.JSON(), nullable=False),
    sa.Column('validation', sa.JSON(), nullable=True),
    sa.Column('threshold', sa.FLOAT(), nullable=True),
    sa.Column('relative_path', sa.TEXT(), nullable=True),
    sa.Column('sha256', sa.VARCHAR(length=64), nullable=True),
    sa.Column('status', sa.VARCHAR(length=24), nullable=False),
    sa.Column('active_slot', sa.VARCHAR(length=16), nullable=True),
    sa.Column('model_blob', sa.LargeBinary(), nullable=True),
    sa.Column('model_type', sa.VARCHAR(length=24), server_default=sa.text("'logistic_regression'"), nullable=False),
    sa.CheckConstraint("active_slot IS NULL OR (model_type = 'logistic_regression' AND active_slot = 'active') OR (model_type = 'mil' AND active_slot = 'mil')", name='vf_model_slot'),
    sa.CheckConstraint("model_type IN ('logistic_regression', 'mil')", name='vf_model_type'),
    sa.CheckConstraint("status != 'active' OR (active_slot IS NOT NULL AND model_blob IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)", name='vf_model_active'),
    sa.CheckConstraint("status IN ('training', 'validated', 'active', 'retired', 'failed')", name='vf_model_status'),
    sa.CheckConstraint('threshold IS NULL OR (threshold >= 0 AND threshold <= 1)', name='vf_model_threshold'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('active_slot')
    )
    op.create_table('video_filter_feedback_event',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('event_key', sa.VARCHAR(length=64), nullable=False),
    sa.Column('asset_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('label', sa.INTEGER(), nullable=False),
    sa.Column('expected_revision', sa.INTEGER(), nullable=False),
    sa.Column('resulting_revision', sa.INTEGER(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.CheckConstraint('label IN (0, 1)', name='vf_feedback_label'),
    sa.ForeignKeyConstraint(['asset_id'], ['video_filter_asset.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asset_id', 'resulting_revision', name='vf_feedback_asset_revision'),
    sa.UniqueConstraint('event_key')
    )
    op.create_table('video_filter_observation',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('config_revision_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('path_key', sa.VARCHAR(length=64), nullable=False),
    sa.Column('size_bytes', sa.BIGINT(), nullable=False),
    sa.Column('modified_ns', sa.BIGINT(), nullable=False),
    sa.Column('file_identity', sa.JSON(), nullable=False),
    sa.Column('stable_since', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_revision_id'], ['video_filter_config_revision.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('config_revision_id', 'path_key', name='vf_observation_path')
    )
    op.create_table('video_filter_scan_run',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('config_revision_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('role_results', sa.JSON(), nullable=False),
    sa.Column('complete', sa.BOOLEAN(), nullable=False),
    sa.Column('lineage_watermark', sa.BIGINT(), nullable=False),
    sa.ForeignKeyConstraint(['config_revision_id'], ['video_filter_config_revision.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('video_filter_variant',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('asset_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('sha256', sa.VARCHAR(length=64), nullable=False),
    sa.Column('size_bytes', sa.BIGINT(), nullable=False),
    sa.Column('media_metadata', sa.JSON(), nullable=False),
    sa.Column('source_variant_id', sa.VARCHAR(length=36), nullable=True),
    sa.CheckConstraint('size_bytes > 0', name='vf_variant_size'),
    sa.ForeignKeyConstraint(['asset_id'], ['video_filter_asset.id'], ),
    sa.ForeignKeyConstraint(['source_variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sha256')
    )
    op.create_index('ix_video_filter_variant_asset_id', 'video_filter_variant', ['asset_id'], unique=False)


def _create_legacy_2():
    op.create_table('video_filter_feature_bundle',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('variant_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('feature_signature', sa.VARCHAR(length=64), nullable=False),
    sa.Column('relative_path', sa.TEXT(), nullable=True),
    sa.Column('manifest_sha256', sa.VARCHAR(length=64), nullable=True),
    sa.Column('windows', sa.INTEGER(), nullable=True),
    sa.Column('modality_validity', sa.JSON(), nullable=True),
    sa.Column('status', sa.VARCHAR(length=24), nullable=False),
    sa.Column('arrays_blob', sa.LargeBinary(), nullable=True),
    sa.Column('arrays_sha256', sa.VARCHAR(length=64), nullable=True),
    sa.Column('manifest', sa.JSON(), nullable=True),
    sa.Column('payload_format', sa.VARCHAR(length=24), nullable=True),
    sa.CheckConstraint("status != 'ready' OR (arrays_blob IS NOT NULL AND arrays_sha256 IS NOT NULL AND manifest IS NOT NULL AND payload_format IS NOT NULL AND payload_format = 'npz-v1' AND manifest_sha256 IS NOT NULL AND windows IS NOT NULL AND windows > 0 AND modality_validity IS NOT NULL)", name='vf_bundle_ready'),
    sa.CheckConstraint("status IN ('extracting', 'ready', 'failed')", name='vf_bundle_status'),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('variant_id', 'feature_signature', name='vf_bundle_variant_signature')
    )
    op.create_table('video_filter_location',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('variant_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('role', sa.VARCHAR(length=24), nullable=False),
    sa.Column('path', sa.TEXT(), nullable=False),
    sa.Column('current_path_key', sa.VARCHAR(length=64), nullable=True),
    sa.Column('file_identity', sa.JSON(), nullable=True),
    sa.Column('size_bytes', sa.BIGINT(), nullable=False),
    sa.Column('modified_ns', sa.BIGINT(), nullable=False),
    sa.Column('last_scan_id', sa.VARCHAR(length=36), nullable=True),
    sa.Column('missing_since', sa.DateTime(), nullable=True),
    sa.Column('status', sa.VARCHAR(length=24), nullable=False),
    sa.CheckConstraint("role IN ('confirmed_like', 'predicted_like', 'predicted_dislike', 'unclassified', 'compressed_like')", name='vf_location_role'),
    sa.CheckConstraint("status IN ('present', 'missing', 'retired', 'conflict')", name='vf_location_status'),
    sa.ForeignKeyConstraint(['last_scan_id'], ['video_filter_scan_run.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('current_path_key')
    )
    op.create_index('ix_video_filter_location_variant_id', 'video_filter_location', ['variant_id'], unique=False)
    op.create_table('video_filter_task',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('dedup_key', sa.VARCHAR(length=64), nullable=False),
    sa.Column('kind', sa.VARCHAR(length=24), nullable=False),
    sa.Column('asset_id', sa.VARCHAR(length=36), nullable=True),
    sa.Column('variant_id', sa.VARCHAR(length=36), nullable=True),
    sa.Column('config_revision_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('input_snapshot', sa.JSON(), nullable=False),
    sa.Column('status', sa.VARCHAR(length=24), nullable=False),
    sa.Column('claim_token', sa.VARCHAR(length=36), nullable=True),
    sa.Column('claimed_at', sa.DateTime(), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
    sa.Column('attempts', sa.INTEGER(), nullable=False),
    sa.Column('error_code', sa.VARCHAR(length=64), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("kind IN ('scan', 'extract', 'train', 'predict', 'classify')", name='vf_task_kind'),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name='vf_task_status'),
    sa.CheckConstraint('attempts >= 0', name='vf_task_attempts'),
    sa.ForeignKeyConstraint(['asset_id'], ['video_filter_asset.id'], ),
    sa.ForeignKeyConstraint(['config_revision_id'], ['video_filter_config_revision.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key')
    )
    op.create_index('ix_video_filter_task_status', 'video_filter_task', ['status'], unique=False)
    op.create_table('video_filter_prediction',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('variant_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('bundle_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('model_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('label_revision', sa.INTEGER(), nullable=False),
    sa.Column('score', sa.FLOAT(), nullable=False),
    sa.Column('predicted_label', sa.INTEGER(), nullable=False),
    sa.Column('group_id', sa.VARCHAR(length=36), nullable=True),
    sa.Column('threshold', sa.FLOAT(), nullable=True),
    sa.Column('selected', sa.BOOLEAN(), server_default=sa.true(), nullable=False),
    sa.Column('evaluation_eligible', sa.BOOLEAN(), server_default=sa.false(), nullable=False),
    sa.CheckConstraint('predicted_label IN (0, 1)', name='vf_prediction_label'),
    sa.CheckConstraint('score >= 0 AND score <= 1', name='vf_prediction_score'),
    sa.CheckConstraint('threshold IS NULL OR (threshold >= 0 AND threshold <= 1)', name='vf_prediction_threshold'),
    sa.ForeignKeyConstraint(['bundle_id'], ['video_filter_feature_bundle.id'], ),
    sa.ForeignKeyConstraint(['model_id'], ['video_filter_model_run.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_id', 'model_id', name='vf_prediction_group_model')
    )
    op.create_table('video_filter_prediction_outcome',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('prediction_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('feedback_event_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('actual_label', sa.INTEGER(), nullable=False),
    sa.Column('correct', sa.BOOLEAN(), nullable=False),
    sa.CheckConstraint('actual_label IN (0, 1)', name='vf_outcome_label'),
    sa.ForeignKeyConstraint(['feedback_event_id'], ['video_filter_feedback_event.id'], ),
    sa.ForeignKeyConstraint(['prediction_id'], ['video_filter_prediction.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('prediction_id', 'feedback_event_id', name='vf_outcome_prediction_feedback')
    )
    op.create_index('ix_video_filter_prediction_outcome_prediction_id', 'video_filter_prediction_outcome', ['prediction_id'], unique=False)
    op.create_table('video_filter_transfer_operation',
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('task_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('variant_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('prediction_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('source_path', sa.TEXT(), nullable=False),
    sa.Column('destination_path', sa.TEXT(), nullable=False),
    sa.Column('source_sha256', sa.VARCHAR(length=64), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('status', sa.VARCHAR(length=32), nullable=False),
    sa.CheckConstraint("status IN ('planned', 'destination_verified', 'published', 'source_cleaned', 'conflict')", name='vf_transfer_status'),
    sa.ForeignKeyConstraint(['prediction_id'], ['video_filter_prediction.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['video_filter_task.id'], ),
    sa.ForeignKeyConstraint(['variant_id'], ['video_filter_variant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id')
    )

