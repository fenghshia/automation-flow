"""Store video_filter numerical summaries as database binary payloads.

Revision ID: b6c20d8e7419
Revises: f3a81c9d6024

Legacy references are retained. Refuse automatic upgrade if ready file summaries
exist, and refuse payload-dropping downgrade when binary summaries exist.
"""

from alembic import op
import sqlalchemy as sa


revision = "b6c20d8e7419"
down_revision = "f3a81c9d6024"
branch_labels = None
depends_on = None

OLD_READY = "status != 'ready' OR (relative_path IS NOT NULL AND manifest_sha256 IS NOT NULL AND windows IS NOT NULL AND windows > 0 AND modality_validity IS NOT NULL)"
NEW_READY = "status != 'ready' OR (arrays_blob IS NOT NULL AND arrays_sha256 IS NOT NULL AND manifest IS NOT NULL AND payload_format IS NOT NULL AND payload_format = 'npz-v1' AND manifest_sha256 IS NOT NULL AND windows IS NOT NULL AND windows > 0 AND modality_validity IS NOT NULL)"


def upgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM video_filter_feature_bundle WHERE status = 'ready'"
    )).scalar_one()
    if count:
        raise RuntimeError("Legacy ready file summaries require an explicit data conversion before this upgrade.")
    with op.batch_alter_table("video_filter_feature_bundle") as batch:
        batch.add_column(sa.Column("arrays_blob", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("arrays_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("manifest", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("payload_format", sa.String(24), nullable=True))
        batch.drop_constraint("vf_bundle_ready", type_="check")
        batch.create_check_constraint("vf_bundle_ready", NEW_READY)


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM video_filter_feature_bundle WHERE arrays_blob IS NOT NULL"
    )).scalar_one()
    if count:
        raise RuntimeError("Export database summaries before downgrading; binary features would be lost.")
    with op.batch_alter_table("video_filter_feature_bundle") as batch:
        batch.drop_constraint("vf_bundle_ready", type_="check")
        batch.create_check_constraint("vf_bundle_ready", OLD_READY)
        for name in ("payload_format", "manifest", "arrays_sha256", "arrays_blob"):
            batch.drop_column(name)
