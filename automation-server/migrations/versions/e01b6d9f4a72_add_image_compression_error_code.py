"""add image compression error code

Revision ID: e01b6d9f4a72
Revises: c62f9e4a71d3
Create Date: 2026-09-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e01b6d9f4a72"
down_revision: Union[str, Sequence[str], None] = "c62f9e4a71d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "image_compression_mission",
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_image_compression_mission_error_code"),
        "image_compression_mission",
        ["error_code"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_image_compression_mission_error_code"),
        table_name="image_compression_mission",
    )
    op.drop_column("image_compression_mission", "error_code")
