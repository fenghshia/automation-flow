"""Persist both classifier outputs before any program-controlled file move."""

import logging
import math
import time
from types import SimpleNamespace

from sqlalchemy import select

from .feature_store import FeatureStore
from .learning import lock_assets, require_model, score
from .identity import source_snapshot as snapshot
from .model_registry import MODEL_TYPES
from .models import Asset, FeatureBundle, ModelRun, Prediction, Variant

logger = logging.getLogger(__name__)


def compatible_predictions(session, variant_id, bundle_id, revision, models, source_snapshot=None):
    from .models import Task
    batches = session.scalars(select(Task).where(Task.variant_id == variant_id,
        Task.kind.in_(("predict", "classify")), Task.status.in_(("running", "succeeded")))
        .order_by(Task.created_at.desc(), Task.id.desc())).all()
    expected = {kind: model.id for kind, model in models.items()}
    for task in batches:
        inputs = task.input_snapshot
        if inputs.get("bundle_id") != bundle_id or inputs.get("label_revision") != revision or inputs.get("model_ids") != expected:
            continue
        if source_snapshot is not None and inputs.get("source_snapshot") != source_snapshot:
            continue
        from .scope import current_scope
        scope = current_scope()
        if scope and (task.dataset_group_id, task.reset_epoch) != (scope["id"], scope["epoch"]):
            continue
        rows = session.scalars(select(Prediction).where(Prediction.prediction_batch_id == task.id)).all()
        found = {row.model_id: row for row in rows if row.variant_id == variant_id and row.bundle_id == bundle_id
            and row.label_revision == revision and math.isfinite(row.score) and 0 <= row.score <= 1}
        if all(model.id in found and found[model.id].threshold == model.threshold for model in models.values()):
            return {kind: found[model.id] for kind, model in models.items()}
    return {}


def predict_both(session, task, selected="logistic_regression", settings=None):
    started = time.monotonic()
    variant = session.get(Variant, task.variant_id)
    bundle = FeatureStore().require_ready(session, task.input_snapshot["bundle_id"])
    if bundle.manifest["variant_id"] != variant.id:
        raise ValueError("summary_variant_mismatch")
    ids = task.input_snapshot.get("model_ids")
    if ids is None:  # Compatible with previously queued single-classifier tasks.
        ids = {selected: task.input_snapshot["model_id"]}
    # Numerical work runs after closing the read transaction; commit rechecks
    # the same sources and models under the established global lock order.
    frozen = {}
    for kind, model_id in ids.items():
        model = session.get(ModelRun, model_id, populate_existing=True)
        if model is None or model.model_type != kind:
            raise ValueError("invalid_prediction_model")
        frozen[kind] = (SimpleNamespace(id=model.id, model_type=kind), require_model(session, model, bundle, lock=False))
    if snapshot(task.input_snapshot["path"]) != task.input_snapshot["source_snapshot"]:
        raise ValueError("source_version_changed")
    existing_ids = set(session.scalars(select(Prediction.model_id).where(Prediction.prediction_batch_id == task.id)))
    identifier = task.id
    session.commit()
    values = {kind: score(session, model, bundle, settings=settings, task_id=identifier, validated_blob=blob)
        for kind, (model, blob) in frozen.items() if model.id not in existing_ids}
    session.rollback()  # Any lazy metadata refresh during numerical work is read-only.
    models = {kind: session.get(ModelRun, run_id, populate_existing=True) for kind, run_id in ids.items()}
    # Lock the target and both training datasets together. Locking the target
    # first, or inserting an LR prediction before locking the MIL dataset,
    # reverses the order used by training/feedback and can deadlock on model FKs.
    asset_ids = {variant.asset_id}
    for kind, model in models.items():
        if kind not in MODEL_TYPES or model is None or model.model_type != kind:
            raise ValueError("invalid_prediction_model")
        asset_ids.update(item["asset_id"] for item in model.dataset_snapshot)
    lock_started = time.monotonic()
    assets = lock_assets(session, asset_ids)
    logger.info("预测资产锁已获取 | task_id=%s | lock_wait_seconds=%.3f", task.id, time.monotonic() - lock_started)
    asset = assets.get(variant.asset_id)
    if asset is None or asset.label_revision != task.input_snapshot["label_revision"]:
        raise ValueError("asset_feedback_changed")
    # Refresh after waiting for asset locks; training may have replaced a model.
    locked_models = {model.id: model for model in session.scalars(select(ModelRun)
        .where(ModelRun.id.in_(ids.values())).order_by(ModelRun.id)
        .with_for_update(read=True, of=ModelRun).execution_options(populate_existing=True)).all()}
    models = {kind: locked_models.get(run_id) for kind, run_id in ids.items()}
    current_bundle = session.execute(select(FeatureBundle.status, FeatureBundle.manifest_sha256, FeatureBundle.arrays_sha256)
        .where(FeatureBundle.id == bundle.bundle_id).with_for_update(read=True, of=FeatureBundle)).first()
    if current_bundle is None or tuple(current_bundle) != ("ready", bundle.manifest_sha256, bundle.manifest["arrays_sha256"]):
        raise ValueError("summary_version_changed")
    predictions = {}
    source = task.input_snapshot["path"]
    if snapshot(source) != task.input_snapshot["source_snapshot"]:
        raise ValueError("source_version_changed")
    for kind, model in models.items():
        if kind not in MODEL_TYPES or model is None or model.model_type != kind:
            raise ValueError("invalid_prediction_model")
        if require_model(session, model, bundle) != frozen[kind][1]:
            raise ValueError("model_parameters_changed")
        existing = session.execute(select(Prediction).filter_by(prediction_batch_id=task.id, model_id=model.id)).scalar_one_or_none()
        if existing is None:
            probability = values[kind]
            eligible = asset.label is None and not any(item["asset_id"] == asset.id for item in model.dataset_snapshot)
            existing = Prediction(variant_id=variant.id, bundle_id=bundle.bundle_id, model_id=model.id,
                prediction_batch_id=task.id, label_revision=asset.label_revision, score=probability, threshold=model.threshold,
                predicted_label=int(probability >= model.threshold), selected=kind == selected, evaluation_eligible=eligible)
            session.add(existing)
            session.flush()
            logger.info("模型预测已生成 | prediction_batch_id=%s | model_type=%s | model_id=%s | variant_id=%s | score=%.4f | threshold=%.3f | predicted_label=%s | selected=%s | evaluation_eligible=%s",
                task.id, kind, model.id, variant.id, probability, model.threshold, existing.predicted_label, kind == selected, eligible)
        elif (existing.variant_id != variant.id or existing.bundle_id != bundle.bundle_id or
              existing.label_revision != asset.label_revision or existing.threshold != model.threshold or
              not math.isfinite(existing.score) or not 0 <= existing.score <= 1):
            raise ValueError("prediction_snapshot_mismatch")
        predictions[kind] = existing
    if not predictions:
        raise ValueError("active_compatible_model_required")
    # If the user moves/deletes/replaces the file during inference, these new
    # outputs cannot be treated as predictions made before their decision.
    if snapshot(source) != task.input_snapshot["source_snapshot"]:
        raise ValueError("source_version_changed")
    session.commit()
    logger.info("各模型预测已提交数据库 | prediction_batch_id=%s | classifier_count=%s | elapsed_seconds=%.3f", task.id, len(predictions), time.monotonic() - started)
    return predictions


def prediction_complete(session, variant_id, bundle_id, revision, models):
    return all(session.execute(select(Prediction.id).where(Prediction.variant_id == variant_id,
        Prediction.bundle_id == bundle_id, Prediction.label_revision == revision,
        Prediction.model_id == model.id, Prediction.prediction_batch_id.is_not(None)).limit(1)).first() for model in models.values())
