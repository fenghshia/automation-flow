"""Persistent identities and artifacts. No video deletion cascades are defined."""

from datetime import datetime, timezone
from uuid import uuid4

from app import db
from ..scope import group_default, epoch_default, install_scope_guards


def new_id():
    return str(uuid4())


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DatasetGroup(db.Model):
    __tablename__ = "video_filter_dataset_group"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    name = db.Column(db.String(64), nullable=False, unique=True)
    reset_epoch = db.Column(db.String(36), nullable=False)
    lineage_floor = db.Column(db.Integer, nullable=False, default=0)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class RuntimeState(db.Model):
    __tablename__ = "video_filter_runtime_state"
    id = db.Column(db.String(16), primary_key=True)
    epoch = db.Column(db.String(36), nullable=False)


class Record:
    dataset_group_id = db.Column(db.String(36), db.ForeignKey("video_filter_dataset_group.id"), nullable=False, default=group_default)
    reset_epoch = db.Column(db.String(36), nullable=False, default=epoch_default)
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class Asset(Record, db.Model):
    __tablename__ = "video_filter_asset"
    label = db.Column(db.Integer, nullable=True)
    label_revision = db.Column(db.Integer, nullable=False, default=0)
    label_event_id = db.Column(db.String(36), nullable=True)
    label_updated_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.CheckConstraint("label IS NULL OR label IN (0, 1)", name="vf_asset_label"),
        db.CheckConstraint("label_revision >= 0", name="vf_asset_revision"),
    )


class ConfigRevision(Record, db.Model):
    __tablename__ = "video_filter_config_revision"
    signature = db.Column(db.String(64), nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending")
    active_slot = db.Column(db.String(16), nullable=True)
    __table_args__ = (
        db.CheckConstraint("status IN ('pending', 'active', 'retired')", name="vf_config_status"),
        db.CheckConstraint("active_slot IS NULL OR active_slot = 'active'", name="vf_config_slot"),
    )


class Observation(Record, db.Model):
    __tablename__ = "video_filter_observation"
    config_revision_id = db.Column(db.String(36), db.ForeignKey("video_filter_config_revision.id"), nullable=False)
    path_key = db.Column(db.String(64), nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    modified_ns = db.Column(db.BigInteger, nullable=False)
    file_identity = db.Column(db.JSON, nullable=False)
    stable_since = db.Column(db.DateTime, nullable=False)
    last_seen_at = db.Column(db.DateTime, nullable=False)
    baseline_entry = db.Column(db.Boolean, nullable=False, default=False)
    __table_args__ = (db.UniqueConstraint("config_revision_id", "path_key", name="vf_observation_path"),)


class Variant(Record, db.Model):
    __tablename__ = "video_filter_variant"
    asset_id = db.Column(db.String(36), db.ForeignKey("video_filter_asset.id"), nullable=False, index=True)
    sha256 = db.Column(db.String(64), nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    media_metadata = db.Column(db.JSON, nullable=False, default=dict)
    source_variant_id = db.Column(db.String(36), db.ForeignKey("video_filter_variant.id"), nullable=True)
    __table_args__ = (db.CheckConstraint("size_bytes > 0", name="vf_variant_size"),)


class ScanRun(Record, db.Model):
    __tablename__ = "video_filter_scan_run"
    config_revision_id = db.Column(db.String(36), db.ForeignKey("video_filter_config_revision.id"), nullable=False)
    finished_at = db.Column(db.DateTime, nullable=True)
    role_results = db.Column(db.JSON, nullable=False, default=dict)
    complete = db.Column(db.Boolean, nullable=False, default=False)
    lineage_watermark = db.Column(db.BigInteger, nullable=False, default=0)


class Location(Record, db.Model):
    __tablename__ = "video_filter_location"
    variant_id = db.Column(db.String(36), db.ForeignKey("video_filter_variant.id"), nullable=False, index=True)
    role = db.Column(db.String(24), nullable=False)
    path = db.Column(db.Text, nullable=False)
    # A normalized path digest is unique only while a location is current.
    # Releasing it (NULL) preserves old location history, including replacements.
    current_path_key = db.Column(db.String(64), nullable=True)
    file_identity = db.Column(db.JSON, nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    modified_ns = db.Column(db.BigInteger, nullable=False)
    last_scan_id = db.Column(db.String(36), db.ForeignKey("video_filter_scan_run.id"), nullable=True)
    missing_since = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(24), nullable=False, default="present")
    __table_args__ = (
        db.CheckConstraint("role IN ('confirmed_like', 'predicted_like', 'predicted_dislike', 'unclassified', 'compressed_like', 'liked', 'liked_source')", name="vf_location_role"),
        db.CheckConstraint("status IN ('present', 'missing', 'retired', 'conflict')", name="vf_location_status"),
    )


class FeatureBundle(Record, db.Model):
    __tablename__ = "video_filter_feature_bundle"
    variant_id = db.Column(db.String(36), db.ForeignKey("video_filter_variant.id"), nullable=False)
    feature_signature = db.Column(db.String(64), nullable=False)
    relative_path = db.Column(db.Text, nullable=True)
    # Legacy file reference is retained for migration safety; new summaries use DB payloads.
    arrays_blob = db.Column(db.LargeBinary, nullable=True)
    arrays_sha256 = db.Column(db.String(64), nullable=True)
    manifest = db.Column(db.JSON, nullable=True)
    payload_format = db.Column(db.String(24), nullable=True)
    manifest_sha256 = db.Column(db.String(64), nullable=True)
    windows = db.Column(db.Integer, nullable=True)
    modality_validity = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(24), nullable=False, default="extracting")
    __table_args__ = (
        db.UniqueConstraint("variant_id", "feature_signature", name="vf_bundle_variant_signature"),
        db.CheckConstraint("status IN ('extracting', 'ready', 'failed')", name="vf_bundle_status"),
        db.CheckConstraint("status != 'ready' OR (arrays_blob IS NOT NULL AND arrays_sha256 IS NOT NULL AND manifest IS NOT NULL AND payload_format IS NOT NULL AND payload_format = 'npz-v1' AND manifest_sha256 IS NOT NULL AND windows IS NOT NULL AND windows > 0 AND modality_validity IS NOT NULL)", name="vf_bundle_ready"),
    )


class FeedbackEvent(Record, db.Model):
    __tablename__ = "video_filter_feedback_event"
    event_key = db.Column(db.String(64), nullable=False, unique=True)
    asset_id = db.Column(db.String(36), db.ForeignKey("video_filter_asset.id"), nullable=False)
    label = db.Column(db.Integer, nullable=False)
    expected_revision = db.Column(db.Integer, nullable=False)
    resulting_revision = db.Column(db.Integer, nullable=False)
    evidence = db.Column(db.JSON, nullable=False)
    __table_args__ = (
        db.CheckConstraint("label IN (0, 1)", name="vf_feedback_label"),
        db.UniqueConstraint("asset_id", "resulting_revision", name="vf_feedback_asset_revision"),
    )


class Task(Record, db.Model):
    __tablename__ = "video_filter_task"
    dedup_key = db.Column(db.String(64), nullable=False, unique=True)
    kind = db.Column(db.String(24), nullable=False)
    asset_id = db.Column(db.String(36), db.ForeignKey("video_filter_asset.id"), nullable=True)
    variant_id = db.Column(db.String(36), db.ForeignKey("video_filter_variant.id"), nullable=True)
    config_revision_id = db.Column(db.String(36), db.ForeignKey("video_filter_config_revision.id"), nullable=False)
    input_snapshot = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="queued", index=True)
    claim_token = db.Column(db.String(36), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    execution_owner = db.Column(db.JSON, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    error_code = db.Column(db.String(64), nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.CheckConstraint("kind IN ('scan', 'extract', 'train', 'predict', 'classify')", name="vf_task_kind"),
        db.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="vf_task_status"),
        db.CheckConstraint("attempts >= 0", name="vf_task_attempts"),
    )


class ModelRun(Record, db.Model):
    __tablename__ = "video_filter_model_run"
    model_type = db.Column(db.String(24), nullable=False, default="logistic_regression", server_default="logistic_regression")
    feature_signature = db.Column(db.String(64), nullable=False)
    dataset_snapshot = db.Column(db.JSON, nullable=False)
    hyperparameters = db.Column(db.JSON, nullable=False, default=dict)
    training_config_digest = db.Column(db.String(64), nullable=True)
    validation = db.Column(db.JSON, nullable=True)
    threshold = db.Column(db.Float, nullable=True)
    relative_path = db.Column(db.Text, nullable=True)
    model_blob = db.Column(db.LargeBinary, nullable=True)
    sha256 = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(24), nullable=False, default="training")
    # Preserve the legacy LR slot; MIL owns a separate unique slot.
    active_slot = db.Column(db.String(16), nullable=True)
    __table_args__ = (
        db.CheckConstraint("status IN ('training', 'validated', 'active', 'retired', 'failed')", name="vf_model_status"),
        db.CheckConstraint("model_type IN ('logistic_regression', 'mil')", name="vf_model_type"),
        db.CheckConstraint("active_slot IS NULL OR (model_type = 'logistic_regression' AND active_slot = 'active') OR (model_type = 'mil' AND active_slot = 'mil')", name="vf_model_slot"),
        db.CheckConstraint("threshold IS NULL OR (threshold >= 0 AND threshold <= 1)", name="vf_model_threshold"),
        db.CheckConstraint("status != 'active' OR (active_slot IS NOT NULL AND model_blob IS NOT NULL AND sha256 IS NOT NULL AND validation IS NOT NULL AND threshold IS NOT NULL)", name="vf_model_active"),
    )


class Prediction(Record, db.Model):
    __tablename__ = "video_filter_prediction"
    variant_id = db.Column(db.String(36), db.ForeignKey("video_filter_variant.id"), nullable=False)
    bundle_id = db.Column(db.String(36), db.ForeignKey("video_filter_feature_bundle.id"), nullable=False)
    model_id = db.Column(db.String(36), db.ForeignKey("video_filter_model_run.id"), nullable=False)
    label_revision = db.Column(db.Integer, nullable=False)
    score = db.Column(db.Float, nullable=False)
    predicted_label = db.Column(db.Integer, nullable=False)
    prediction_batch_id = db.Column(db.String(36), nullable=True)
    group_id = db.synonym("prediction_batch_id")
    threshold = db.Column(db.Float, nullable=True)
    selected = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    evaluation_eligible = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    __table_args__ = (
        db.UniqueConstraint("prediction_batch_id", "model_id", name="vf_prediction_group_model"),
        db.CheckConstraint("predicted_label IN (0, 1)", name="vf_prediction_label"),
        db.CheckConstraint("score >= 0 AND score <= 1", name="vf_prediction_score"),
        db.CheckConstraint("threshold IS NULL OR (threshold >= 0 AND threshold <= 1)", name="vf_prediction_threshold"),
    )


class PredictionOutcome(Record, db.Model):
    __tablename__ = "video_filter_prediction_outcome"
    prediction_id = db.Column(db.String(36), db.ForeignKey("video_filter_prediction.id"), nullable=False, index=True)
    feedback_event_id = db.Column(db.String(36), db.ForeignKey("video_filter_feedback_event.id"), nullable=False)
    actual_label = db.Column(db.Integer, nullable=False)
    correct = db.Column(db.Boolean, nullable=False)
    __table_args__ = (
        db.UniqueConstraint("prediction_id", "feedback_event_id", name="vf_outcome_prediction_feedback"),
        db.CheckConstraint("actual_label IN (0, 1)", name="vf_outcome_label"),
    )


class TransferOperation(Record, db.Model):
    __tablename__ = "video_filter_transfer_operation"
    task_id = db.Column(db.String(36), db.ForeignKey("video_filter_task.id"), nullable=False, unique=True)
    variant_id = db.Column(db.String(36), db.ForeignKey("video_filter_variant.id"), nullable=False)
    prediction_id = db.Column(db.String(36), db.ForeignKey("video_filter_prediction.id"), nullable=False)
    source_path = db.Column(db.Text, nullable=False)
    destination_path = db.Column(db.Text, nullable=False)
    source_sha256 = db.Column(db.String(64), nullable=False)
    evidence = db.Column(db.JSON, nullable=False, default=dict)
    status = db.Column(db.String(32), nullable=False, default="planned")
    __table_args__ = (db.CheckConstraint("status IN ('planned', 'destination_verified', 'published', 'source_cleaned', 'conflict')", name="vf_transfer_status"),)

# Composite constraints enforce scope even for direct SQL and identity-map access.
SCOPED_MODELS = (Asset, ConfigRevision, Observation, Variant, ScanRun, Location, FeatureBundle,
                 FeedbackEvent, Task, ModelRun, Prediction, PredictionOutcome, TransferOperation)
for model in SCOPED_MODELS:
    table = model.__table__
    table.append_constraint(db.UniqueConstraint("dataset_group_id", "id", name="vf_scope_id_" + table.name.removeprefix("video_filter_")))
    links = [(column, list(column.foreign_keys)) for column in table.columns]
    for column, foreign_keys in links:
        for foreign in foreign_keys:
            target = foreign.target_fullname.split(".")[0]
            if target.startswith("video_filter_") and target != "video_filter_dataset_group":
                table.append_constraint(db.ForeignKeyConstraint(["dataset_group_id", column.name],
                    [target + ".dataset_group_id", foreign.target_fullname],
                    name="vf_scope_" + table.name.removeprefix("video_filter_") + "_" + column.name))
for model, columns in ((ConfigRevision, ("signature",)), (ConfigRevision, ("active_slot",)),
                       (Variant, ("sha256",)), (Location, ("current_path_key",)), (ModelRun, ("active_slot",))):
    model.__table__.append_constraint(db.UniqueConstraint("dataset_group_id", *columns,
        name="vf_group_" + model.__tablename__.removeprefix("video_filter_") + "_" + columns[0]))
install_scope_guards(Record)
