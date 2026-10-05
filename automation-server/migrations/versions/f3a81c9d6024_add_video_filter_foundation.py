"""Add video_filter identities, summaries, tasks and shared media lineage.

Revision ID: f3a81c9d6024
Revises: e01b6d9f4a72

Only new tables are created. No production migration is executed by this file
being imported. Downgrade removes this project's state and must be explicit.
"""

from alembic import op
import sqlalchemy as sa


revision = "f3a81c9d6024"
down_revision = "e01b6d9f4a72"
branch_labels = None
depends_on = None


def _base():
    return [sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False)]


def _ref(name, target, nullable=False):
    return sa.Column(name, sa.String(36), sa.ForeignKey(target), nullable=nullable)


def upgrade():
    op.create_table("video_filter_asset", *_base(),
        sa.Column("label", sa.Integer(), nullable=True),
        sa.Column("label_revision", sa.Integer(), nullable=False),
        sa.Column("label_event_id", sa.String(36), nullable=True),
        sa.Column("label_updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("label IS NULL OR label IN (0, 1)", name="vf_asset_label"),
        sa.CheckConstraint("label_revision >= 0", name="vf_asset_revision"))
    op.create_table("video_filter_config_revision", *_base(),
        sa.Column("signature", sa.String(64), nullable=False, unique=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'active', 'retired')", name="vf_config_status"))
    op.create_table("video_filter_variant", *_base(),
        _ref("asset_id", "video_filter_asset.id"),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_metadata", sa.JSON(), nullable=False),
        _ref("source_variant_id", "video_filter_variant.id", True),
        sa.CheckConstraint("size_bytes > 0", name="vf_variant_size"))
    op.create_index("ix_video_filter_variant_asset_id", "video_filter_variant", ["asset_id"])
    op.create_table("video_filter_scan_run", *_base(),
        _ref("config_revision_id", "video_filter_config_revision.id"),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("role_results", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("lineage_watermark", sa.BigInteger(), nullable=False))
    op.create_table("video_filter_location", *_base(),
        _ref("variant_id", "video_filter_variant.id"),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("current_path_key", sa.String(64), nullable=True, unique=True),
        sa.Column("file_identity", sa.JSON(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("modified_ns", sa.BigInteger(), nullable=False),
        _ref("last_scan_id", "video_filter_scan_run.id", True),
        sa.Column("missing_since", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.CheckConstraint("role IN ('confirmed_like', 'predicted_like', 'predicted_dislike', 'unclassified', 'compressed_like')", name="vf_location_role"),
        sa.CheckConstraint("status IN ('present', 'missing', 'retired', 'conflict')", name="vf_location_status"))
    op.create_index("ix_video_filter_location_variant_id", "video_filter_location", ["variant_id"])
    op.create_table("video_filter_feature_bundle", *_base(),
        _ref("variant_id", "video_filter_variant.id"),
        sa.Column("feature_signature", sa.String(64), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("windows", sa.Integer(), nullable=True),
        sa.Column("modality_validity", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.UniqueConstraint("variant_id", "feature_signature", name="vf_bundle_variant_signature"),
        sa.CheckConstraint("status IN ('extracting', 'ready', 'failed')", name="vf_bundle_status"),
        sa.CheckConstraint("status != 'ready' OR (relative_path IS NOT NULL AND manifest_sha256 IS NOT NULL AND windows IS NOT NULL AND windows > 0 AND modality_validity IS NOT NULL)", name="vf_bundle_ready"))
    op.create_table("video_filter_feedback_event", *_base(),
        sa.Column("event_key", sa.String(64), nullable=False, unique=True),
        _ref("asset_id", "video_filter_asset.id"),
        sa.Column("label", sa.Integer(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.CheckConstraint("label IN (0, 1)", name="vf_feedback_label"),
        sa.UniqueConstraint("asset_id", "resulting_revision", name="vf_feedback_asset_revision"))
    op.create_table("video_filter_task", *_base(),
        sa.Column("dedup_key", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(24), nullable=False),
        _ref("asset_id", "video_filter_asset.id", True),
        _ref("variant_id", "video_filter_variant.id", True),
        _ref("config_revision_id", "video_filter_config_revision.id"),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('scan', 'extract', 'train', 'classify')", name="vf_task_kind"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="vf_task_status"),
        sa.CheckConstraint("attempts >= 0", name="vf_task_attempts"))
    op.create_index("ix_video_filter_task_status", "video_filter_task", ["status"])
    op.create_table("video_filter_model_run", *_base(),
        sa.Column("feature_signature", sa.String(64), nullable=False),
        sa.Column("dataset_snapshot", sa.JSON(), nullable=False),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("active_slot", sa.String(16), nullable=True, unique=True),
        sa.CheckConstraint("status IN ('training', 'validated', 'active', 'retired', 'failed')", name="vf_model_status"),
        sa.CheckConstraint("active_slot IS NULL OR active_slot = 'active'", name="vf_model_slot"),
        sa.CheckConstraint("threshold IS NULL OR (threshold >= 0 AND threshold <= 1)", name="vf_model_threshold"),
        sa.CheckConstraint("status != 'active' OR (active_slot IS NOT NULL AND active_slot = 'active' AND relative_path IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)", name="vf_model_active"))
    op.create_table("video_filter_prediction", *_base(),
        _ref("variant_id", "video_filter_variant.id"),
        _ref("bundle_id", "video_filter_feature_bundle.id"),
        _ref("model_id", "video_filter_model_run.id"),
        sa.Column("label_revision", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("predicted_label", sa.Integer(), nullable=False),
        sa.CheckConstraint("predicted_label IN (0, 1)", name="vf_prediction_label"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="vf_prediction_score"))
    op.create_table("video_filter_transfer_operation", *_base(),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("video_filter_task.id"), nullable=False, unique=True),
        _ref("variant_id", "video_filter_variant.id"),
        _ref("prediction_id", "video_filter_prediction.id"),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("destination_path", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint("status IN ('planned', 'destination_verified', 'published', 'source_cleaned', 'conflict')", name="vf_transfer_status"))
    op.create_table("media_lineage_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("producer", sa.String(32), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("destination_sha256", sa.String(64), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("destination_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("operation_id", "sequence", name="ml_operation_sequence"),
        sa.CheckConstraint("sequence >= 0", name="ml_sequence"),
        sa.CheckConstraint("producer IN ('video_filter', 'video_compression')", name="ml_producer"),
        sa.CheckConstraint("phase IN ('planned', 'destination_verified', 'published', 'source_cleaned')", name="ml_phase"),
        sa.CheckConstraint("phase = 'planned' OR destination_sha256 IS NOT NULL", name="ml_destination_hash"))
    op.create_index("ix_media_lineage_event_operation_id", "media_lineage_event", ["operation_id"])


def downgrade():
    for name in (
        "media_lineage_event", "video_filter_transfer_operation", "video_filter_prediction",
        "video_filter_model_run", "video_filter_task", "video_filter_feedback_event",
        "video_filter_feature_bundle", "video_filter_location", "video_filter_scan_run",
        "video_filter_variant", "video_filter_config_revision", "video_filter_asset",
    ):
        op.drop_table(name)
