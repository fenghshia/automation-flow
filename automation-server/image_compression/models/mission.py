from datetime import datetime

from app import db


class ImageCompressionStatus:
    WAITING_STABLE = "waiting_stable"
    READY = "ready"
    MOVING = "moving"
    PROCESSING = "processing"
    PUBLISHING = "publishing"
    CLEANUP_PENDING = "cleanup_pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ImageCompressionMission(db.Model):
    __tablename__ = "image_compression_mission"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source_path = db.Column(db.Text, nullable=False, unique=True)
    source_kind = db.Column(db.String(16), nullable=False)
    source_name = db.Column(db.Text, nullable=False)
    destination_key = db.Column(db.Text, nullable=False)
    destination_key_normalized = db.Column(db.Text, nullable=False, index=True)
    source_size_bytes = db.Column(db.BigInteger, nullable=False)
    source_file_count = db.Column(db.Integer, nullable=False)
    source_modified_ns = db.Column(db.BigInteger, nullable=True)
    source_manifest_sha256 = db.Column(db.String(64), nullable=False)
    stable_checks = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(
        db.String(32),
        nullable=False,
        index=True,
        default=ImageCompressionStatus.WAITING_STABLE,
    )
    attempts = db.Column(db.Integer, nullable=False, default=0)
    output_path = db.Column(db.Text, nullable=True)
    output_manifest_sha256 = db.Column(db.String(64), nullable=True)
    output_file_count = db.Column(db.Integer, nullable=True)
    output_size_bytes = db.Column(db.BigInteger, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    last_checked_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    processing_started_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

