from datetime import datetime

from app import db


class CompressionStatus:
    WAITING_STABLE = "waiting_stable"
    READY = "ready"
    PROCESSING = "processing"
    VALIDATING = "validating"
    CLEANUP_PENDING = "cleanup_pending"
    COMPLETED = "completed"
    FAILED = "failed"


class CompressionMission(db.Model):
    __tablename__ = "video_compression_mission"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source_path = db.Column(db.Text, nullable=False, unique=True)
    file_name = db.Column(db.Text, nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    modified_ns = db.Column(db.BigInteger, nullable=False)
    stable_checks = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(
        db.String(32), nullable=False, index=True, default=CompressionStatus.WAITING_STABLE
    )
    attempts = db.Column(db.Integer, nullable=False, default=0)
    output_path = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

