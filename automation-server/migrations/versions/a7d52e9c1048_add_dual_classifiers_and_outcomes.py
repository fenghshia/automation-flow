"""Independent LR/MIL model slots and immutable prospective feedback records."""

from alembic import op
import sqlalchemy as sa

revision = "a7d52e9c1048"
down_revision = "d9e41b7a2036"
branch_labels = None
depends_on = None

OLD_ACTIVE = "status != 'active' OR (active_slot IS NOT NULL AND active_slot = 'active' AND model_blob IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)"
NEW_ACTIVE = "status != 'active' OR (active_slot IS NOT NULL AND model_blob IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)"
NEW_SLOT = "active_slot IS NULL OR (model_type = 'logistic_regression' AND active_slot = 'active') OR (model_type = 'mil' AND active_slot = 'mil')"


def upgrade():
    with op.batch_alter_table("video_filter_model_run") as batch:
        batch.add_column(sa.Column("model_type", sa.String(24), nullable=False, server_default="logistic_regression"))
        batch.drop_constraint("vf_model_slot", type_="check")
        batch.drop_constraint("vf_model_active", type_="check")
        batch.create_check_constraint("vf_model_type", "model_type IN ('logistic_regression', 'mil')")
        batch.create_check_constraint("vf_model_slot", NEW_SLOT)
        batch.create_check_constraint("vf_model_active", NEW_ACTIVE)
    with op.batch_alter_table("video_filter_task") as batch:
        batch.drop_constraint("vf_task_kind", type_="check")
        batch.create_check_constraint("vf_task_kind", "kind IN ('scan', 'extract', 'train', 'predict', 'classify')")
    with op.batch_alter_table("video_filter_prediction") as batch:
        batch.add_column(sa.Column("group_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("threshold", sa.Float(), nullable=True))
        batch.add_column(sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.true()))
        # Historical predictions lack evidence of prospective eligibility.
        batch.add_column(sa.Column("evaluation_eligible", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_unique_constraint("vf_prediction_group_model", ["group_id", "model_id"])
        batch.create_check_constraint("vf_prediction_threshold", "threshold IS NULL OR (threshold >= 0 AND threshold <= 1)")
    op.create_table("video_filter_prediction_outcome",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("prediction_id", sa.String(36), sa.ForeignKey("video_filter_prediction.id"), nullable=False),
        sa.Column("feedback_event_id", sa.String(36), sa.ForeignKey("video_filter_feedback_event.id"), nullable=False),
        sa.Column("actual_label", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("prediction_id", "feedback_event_id", name="vf_outcome_prediction_feedback"),
        sa.CheckConstraint("actual_label IN (0, 1)", name="vf_outcome_label"))
    op.create_index("ix_video_filter_prediction_outcome_prediction_id", "video_filter_prediction_outcome", ["prediction_id"])


def downgrade():
    connection = op.get_bind()
    tasks = sa.table("video_filter_task", sa.column("input_snapshot", sa.JSON()))
    new_tasks = any("model_type" in value or "model_ids" in value
                    for value in connection.execute(sa.select(tasks.c.input_snapshot)).scalars())
    if (connection.execute(sa.text("SELECT COUNT(*) FROM video_filter_prediction_outcome")).scalar_one()
            or connection.execute(sa.text("SELECT COUNT(*) FROM video_filter_prediction WHERE group_id IS NOT NULL")).scalar_one()
            or connection.execute(sa.text("SELECT COUNT(*) FROM video_filter_model_run WHERE model_type = 'mil'")).scalar_one()
            or connection.execute(sa.text("SELECT COUNT(*) FROM video_filter_task WHERE kind = 'predict'")).scalar_one()
            or new_tasks):
        raise RuntimeError("Export dual classifier models, tasks, predictions and outcomes before downgrading.")
    op.drop_table("video_filter_prediction_outcome")
    with op.batch_alter_table("video_filter_prediction") as batch:
        batch.drop_constraint("vf_prediction_group_model", type_="unique")
        batch.drop_constraint("vf_prediction_threshold", type_="check")
        for column in ("evaluation_eligible", "selected", "threshold", "group_id"):
            batch.drop_column(column)
    with op.batch_alter_table("video_filter_task") as batch:
        batch.drop_constraint("vf_task_kind", type_="check")
        batch.create_check_constraint("vf_task_kind", "kind IN ('scan', 'extract', 'train', 'classify')")
    with op.batch_alter_table("video_filter_model_run") as batch:
        batch.drop_constraint("vf_model_slot", type_="check")
        batch.drop_constraint("vf_model_type", type_="check")
        batch.drop_constraint("vf_model_active", type_="check")
        batch.drop_column("model_type")
        batch.create_check_constraint("vf_model_slot", "active_slot IS NULL OR active_slot = 'active'")
        batch.create_check_constraint("vf_model_active", OLD_ACTIVE)
