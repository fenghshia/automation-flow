"""add image compression mission

Revision ID: c62f9e4a71d3
Revises: a7c2e1f94b36
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c62f9e4a71d3"
down_revision: Union[str, Sequence[str], None] = "a7c2e1f94b36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "image_compression_mission",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("destination_key", sa.Text(), nullable=False),
        sa.Column("destination_key_normalized", sa.Text(), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_file_count", sa.Integer(), nullable=False),
        sa.Column("source_modified_ns", sa.BigInteger(), nullable=True),
        sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("stable_checks", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("output_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_file_count", sa.Integer(), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_path"),
    )
    op.create_index(
        op.f("ix_image_compression_mission_destination_key_normalized"),
        "image_compression_mission",
        ["destination_key_normalized"],
        unique=False,
    )
    op.create_index(
        op.f("ix_image_compression_mission_status"),
        "image_compression_mission",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_image_compression_mission_status"),
        table_name="image_compression_mission",
    )
    op.drop_index(
        op.f("ix_image_compression_mission_destination_key_normalized"),
        table_name="image_compression_mission",
    )
    op.drop_table("image_compression_mission")

