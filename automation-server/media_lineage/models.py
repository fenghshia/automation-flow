from datetime import datetime, timezone

from sqlalchemy import event

from app import db
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class WorkflowBinding(db.Model):
    """Neutral public compression mapping; no dependency on filter tables."""
    __tablename__ = "media_workflow_binding"
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    reset_epoch = db.Column(db.String(36), nullable=False)
    directory_revision_id = db.Column(db.String(36), nullable=False)
    source_directory = db.Column(db.Text, nullable=False)
    destination_directory = db.Column(db.Text, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)


class ResourceLease(db.Model):
    __tablename__ = "media_resource_lease"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    device = db.Column(db.String(128), nullable=False, index=True)
    mode = db.Column(db.String(32), nullable=False)
    owner = db.Column(db.String(128), nullable=False, unique=True)
    pid = db.Column(db.Integer, nullable=False)
    process_identity = db.Column(db.String(128), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="waiting")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    heartbeat_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.CheckConstraint("mode IN ('extract_shared', 'exclusive_train', 'exclusive_compression')", name="ml_resource_mode"),
        db.CheckConstraint("status IN ('waiting', 'active', 'released', 'conflict')", name="ml_resource_status"),
    )


class LineageEvent(db.Model):
    __tablename__ = "media_lineage_event"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    operation_id = db.Column(db.String(36), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    producer = db.Column(db.String(32), nullable=False)
    generation = db.Column(db.String(36), nullable=False)
    phase = db.Column(db.String(32), nullable=False)
    source_sha256 = db.Column(db.String(64), nullable=False)
    destination_sha256 = db.Column(db.String(64), nullable=True)
    source_path = db.Column(db.Text, nullable=False)
    destination_path = db.Column(db.Text, nullable=False)
    evidence = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("operation_id", "sequence", name="ml_operation_sequence"),
        db.CheckConstraint("sequence >= 0", name="ml_sequence"),
        db.CheckConstraint("producer IN ('video_filter', 'video_compression')", name="ml_producer"),
        db.CheckConstraint("phase IN ('planned', 'destination_verified', 'published', 'source_cleaned')", name="ml_phase"),
        db.CheckConstraint("phase = 'planned' OR destination_sha256 IS NOT NULL", name="ml_destination_hash"),
    )


@event.listens_for(LineageEvent, "before_update")
@event.listens_for(LineageEvent, "before_delete")
def reject_mutation(mapper, connection, target):
    raise ValueError("Lineage events are append-only.")
