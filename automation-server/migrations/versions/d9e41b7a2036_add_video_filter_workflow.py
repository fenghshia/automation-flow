"""Add persistent scanning, active configuration and DB classifier artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "d9e41b7a2036"
down_revision = "b6c20d8e7419"
branch_labels = None
depends_on = None
OLD_MODEL_ACTIVE = "status != 'active' OR (active_slot IS NOT NULL AND active_slot = 'active' AND relative_path IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)"
NEW_MODEL_ACTIVE = "status != 'active' OR (active_slot IS NOT NULL AND active_slot = 'active' AND model_blob IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)"


def upgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM video_filter_model_run WHERE status = 'active'")).scalar_one():
        raise RuntimeError("Existing active model artifacts require explicit conversion before upgrading.")
    with op.batch_alter_table("video_filter_config_revision") as batch:
        batch.add_column(sa.Column("active_slot", sa.String(16), nullable=True))
        batch.create_unique_constraint("vf_config_active_slot", ["active_slot"])
        batch.create_check_constraint("vf_config_slot", "active_slot IS NULL OR active_slot = 'active'")
    op.create_table("video_filter_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("config_revision_id", sa.String(36), sa.ForeignKey("video_filter_config_revision.id"), nullable=False),
        sa.Column("path_key", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("modified_ns", sa.BigInteger(), nullable=False),
        sa.Column("file_identity", sa.JSON(), nullable=False),
        sa.Column("stable_since", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("config_revision_id", "path_key", name="vf_observation_path"))
    with op.batch_alter_table("video_filter_model_run") as batch:
        batch.add_column(sa.Column("model_blob", sa.LargeBinary(), nullable=True))
        batch.drop_constraint("vf_model_active", type_="check")
        batch.create_check_constraint("vf_model_active", NEW_MODEL_ACTIVE)
    with op.batch_alter_table("media_lineage_event") as batch:
        batch.add_column(sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM video_filter_model_run WHERE model_blob IS NOT NULL")).scalar_one():
        raise RuntimeError("Export classifier artifacts before downgrading.")
    with op.batch_alter_table("media_lineage_event") as batch:
        batch.drop_column("evidence")
    with op.batch_alter_table("video_filter_model_run") as batch:
        batch.drop_constraint("vf_model_active", type_="check")
        batch.create_check_constraint("vf_model_active", OLD_MODEL_ACTIVE)
        batch.drop_column("model_blob")
    op.drop_table("video_filter_observation")
    with op.batch_alter_table("video_filter_config_revision") as batch:
        batch.drop_constraint("vf_config_slot", type_="check")
        batch.drop_constraint("vf_config_active_slot", type_="unique")
        batch.drop_column("active_slot")
