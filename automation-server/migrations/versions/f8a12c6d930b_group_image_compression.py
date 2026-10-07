"""Persist image group snapshots; leave historical directory ownership unbound."""
from alembic import op
import sqlalchemy as sa

revision = "f8a12c6d930b"
down_revision = "d2e90b1746a3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("image_compression_mission") as batch:
        batch.add_column(sa.Column("group_name", sa.String(64), nullable=True))
        batch.add_column(sa.Column("source_directory", sa.Text(), nullable=True))
        batch.add_column(sa.Column("output_directory", sa.Text(), nullable=True))
        batch.add_column(sa.Column("output_scope_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("flatten", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.create_index("ix_image_compression_mission_group_name", ["group_name"])
        batch.create_index("ix_image_compression_mission_output_scope_key", ["output_scope_key"])


def downgrade():
    outstanding = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM image_compression_mission "
        "WHERE group_name IS NOT NULL AND status NOT IN ('completed', 'failed')"
    )).scalar()
    if outstanding:
        raise RuntimeError("Finish bound image tasks before downgrading group snapshots.")
    with op.batch_alter_table("image_compression_mission") as batch:
        batch.drop_index("ix_image_compression_mission_output_scope_key")
        batch.drop_index("ix_image_compression_mission_group_name")
        for name in ("flatten", "output_scope_key", "output_directory", "source_directory", "group_name"):
            batch.drop_column(name)
