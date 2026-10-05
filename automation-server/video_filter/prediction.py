"""Persist both classifier outputs before any program-controlled file move."""

import logging

from sqlalchemy import select

from .feature_store import FeatureStore
from .learning import score
from .identity import snapshot
from .model_registry import MODEL_TYPES
from .models import Asset, ModelRun, Prediction, Variant

logger = logging.getLogger(__name__)


def predict_both(session, task, selected="logistic_regression"):
    variant = session.get(Variant, task.variant_id)
    asset = session.get(Asset, variant.asset_id, populate_existing=True, with_for_update=True)
    if asset.label_revision != task.input_snapshot["label_revision"]:
        raise ValueError("asset_feedback_changed")
    bundle = FeatureStore().require_ready(session, task.input_snapshot["bundle_id"])
    if bundle.manifest["variant_id"] != variant.id:
        raise ValueError("summary_variant_mismatch")
    ids = task.input_snapshot.get("model_ids")
    if ids is None:  # Compatible with previously queued single-classifier tasks.
        ids = {selected: task.input_snapshot["model_id"]}
    models = {kind: session.get(ModelRun, run_id, populate_existing=True) for kind, run_id in ids.items()}
    predictions = {}
    source = task.input_snapshot["path"]
    if snapshot(source) != task.input_snapshot["source_snapshot"]:
        raise ValueError("source_version_changed")
    for kind, model in models.items():
        if kind not in MODEL_TYPES or model is None or model.model_type != kind:
            raise ValueError("invalid_prediction_model")
        existing = session.execute(select(Prediction).filter_by(prediction_batch_id=task.id, model_id=model.id)).scalar_one_or_none()
        if existing is None:
            probability = score(session, model, bundle)
            eligible = asset.label is None and not any(item["asset_id"] == asset.id for item in model.dataset_snapshot)
            existing = Prediction(variant_id=variant.id, bundle_id=bundle.bundle_id, model_id=model.id,
                prediction_batch_id=task.id, label_revision=asset.label_revision, score=probability, threshold=model.threshold,
                predicted_label=int(probability >= model.threshold), selected=kind == selected, evaluation_eligible=eligible)
            session.add(existing)
            session.flush()
            logger.info("模型预测已生成 | prediction_batch_id=%s | model_type=%s | model_id=%s | variant_id=%s | score=%.4f | threshold=%.3f | predicted_label=%s | selected=%s | evaluation_eligible=%s",
                task.id, kind, model.id, variant.id, probability, model.threshold, existing.predicted_label, kind == selected, eligible)
        predictions[kind] = existing
    if not predictions:
        raise ValueError("active_compatible_model_required")
    # If the user moves/deletes/replaces the file during inference, these new
    # outputs cannot be treated as predictions made before their decision.
    if snapshot(source) != task.input_snapshot["source_snapshot"]:
        raise ValueError("source_version_changed")
    session.commit()
    logger.info("各模型预测已提交数据库 | prediction_batch_id=%s | classifier_count=%s", task.id, len(predictions))
    return predictions


def prediction_complete(session, variant_id, bundle_id, revision, models):
    return all(session.execute(select(Prediction.id).where(Prediction.variant_id == variant_id,
        Prediction.bundle_id == bundle_id, Prediction.label_revision == revision,
        Prediction.model_id == model.id, Prediction.prediction_batch_id.is_not(None)).limit(1)).first() for model in models.values())
