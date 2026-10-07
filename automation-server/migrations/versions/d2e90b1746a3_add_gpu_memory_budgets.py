"""Add recoverable GPU reservations without changing existing leases or video data."""
from alembic import op
import sqlalchemy as sa

revision = "d2e90b1746a3"
down_revision = "c4f18a2d9076"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_resource_lease") as batch:
        batch.add_column(sa.Column("memory_budget_mib", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("memory_observation", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM media_resource_lease WHERE status IN ('active', 'conflict')")).scalar():
        raise RuntimeError("Finish active GPU workers before downgrading memory reservations.")
    with op.batch_alter_table("media_resource_lease") as batch:
        batch.drop_column("memory_observation")
        batch.drop_column("memory_budget_mib")
