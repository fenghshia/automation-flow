"""Observed user outcomes, never training/validation accuracy or pseudo-labels."""

import logging
import math

from sqlalchemy import select

from .models import FeedbackEvent, ModelRun, Prediction, PredictionOutcome, Variant

logger = logging.getLogger(__name__)


def record_outcomes(session, event):
    # Locking the asset in apply_feedback serializes predictions and feedback.
    # Keep *all* historical judgments, including preference changes; reporting
    # chooses the latest judgment and deduplicates by asset rather than file.
    predictions = session.execute(select(Prediction).join(Variant, Variant.id == Prediction.variant_id)
        .where(Variant.asset_id == event.asset_id, Prediction.created_at < event.created_at)).scalars().all()
    for prediction in predictions:
        session.add(PredictionOutcome(prediction_id=prediction.id, feedback_event_id=event.id,
            actual_label=event.label, correct=prediction.predicted_label == event.label))
    session.flush()
    if predictions:
        logger.info("用户实际结果已关联预测 | asset_id=%s | feedback_event_id=%s | actual_label=%s | prediction_records=%s | reason=%s",
                    event.asset_id, event.id, event.label, len(predictions), event.evidence["reason"])


def _statistics(rows):
    # Rows are one prospective prediction per asset for the requested cohort.
    tp = sum(row["predicted"] == 1 and row["actual"] == 1 for row in rows)
    tn = sum(row["predicted"] == 0 and row["actual"] == 0 for row in rows)
    fp = sum(row["predicted"] == 1 and row["actual"] == 0 for row in rows)
    fn = sum(row["predicted"] == 0 and row["actual"] == 1 for row in rows)
    n, correct = len(rows), tp + tn
    labels = {}
    for name, hits, false, missed in (("like", tp, fp, fn), ("dislike", tn, fn, fp)):
        labels[name] = {"actual_assets": hits + missed, "predicted_assets": hits + false,
            "correct": hits, "false_predictions": false, "missed_assets": missed,
            "precision": hits / (hits + false) if hits + false else None,
            "recall": hits / (hits + missed) if hits + missed else None}
    accuracy = correct / n if n else None
    interval = None
    if n:
        z = 1.96
        center = (accuracy + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(accuracy * (1 - accuracy) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        interval = [max(0, center - half), min(1, center + half)]
    return {"reviewed_assets": n, "correct": correct, "accuracy": accuracy, "labels": labels,
        "accuracy_wilson_95": interval,
        "balanced_accuracy": ((tp / (tp + fn) + tn / (tn + fp)) / 2) if tp + fn and tn + fp else None,
        "like_precision": tp / (tp + fp) if tp + fp else None,
        "dislike_precision": tn / (tn + fn) if tn + fn else None,
        "confusion": {"true_like": tp, "true_dislike": tn, "false_like": fp, "false_dislike": fn}}


def classification_metrics(actual, predicted):
    return _statistics([{"actual": int(label), "predicted": int(guess)} for label, guess in zip(actual, predicted)])


def acceptance_result(metrics, requirements):
    checks = {name: metrics["labels"][name]["precision"] is not None and
              metrics["labels"][name]["precision"] >= requirements[name + "_precision"]
              for name in ("like", "dislike")}
    return {"method": "per_label_precision", "requirements": dict(requirements),
            "checks": checks, "passed": all(checks.values())}


def actual_metrics(session, include_versions=True):
    from .model_registry import MODEL_TYPES

    # Select metadata only: never fetch numerical features or classifier blobs.
    rows = session.execute(select(Variant.asset_id, ModelRun.model_type, Prediction.model_id,
        Prediction.id, Prediction.prediction_batch_id, Prediction.created_at, Prediction.predicted_label,
        FeedbackEvent.id, FeedbackEvent.resulting_revision, PredictionOutcome.actual_label)
        .select_from(Prediction)
        .join(PredictionOutcome, PredictionOutcome.prediction_id == Prediction.id)
        .join(FeedbackEvent, FeedbackEvent.id == PredictionOutcome.feedback_event_id)
        .join(Variant, Variant.id == Prediction.variant_id)
        .join(ModelRun, ModelRun.id == Prediction.model_id)
        .where(Prediction.evaluation_eligible == True)
        .order_by(FeedbackEvent.resulting_revision, Prediction.created_at, Prediction.id)).all()
    latest_feedback = {}
    for row in rows:
        latest_feedback[row[0]] = max(latest_feedback.get(row[0], 0), row[8])
    family, versions, groups = {}, {}, {}
    for row in rows:
        asset, kind, model_id, prediction_id, group, created, predicted, event_id, revision, actual = row
        if revision != latest_feedback[asset]:
            continue
        item = {"asset_id": asset, "model_type": kind, "model_id": model_id,
                "prediction_id": prediction_id, "group_id": group, "created_at": created,
                "feedback_event_id": event_id, "predicted": predicted, "actual": actual}
        family[asset, kind] = item
        versions[asset, model_id] = item
        if group is not None:
            groups.setdefault((asset, group, event_id), {})[kind] = item
    paired = {}
    for (asset, _, _), pair in groups.items():
        if set(pair) != set(MODEL_TYPES):
            continue
        timestamp = max(item["created_at"] for item in pair.values())
        if asset not in paired or timestamp > paired[asset][0]:
            paired[asset] = timestamp, pair
    runs = session.execute(select(ModelRun.id, ModelRun.model_type, ModelRun.status, ModelRun.created_at)
                           .order_by(ModelRun.created_at.desc())).all() if include_versions else []
    return {"method": "prospective_user_feedback_latest_per_asset",
        "eligibility": "unlabelled_at_prediction_and_absent_from_model_training_and_validation_snapshot",
        "feedback_policy": "latest_user_judgment_with_full_history_preserved",
        "models": {kind: _statistics([item for (_, name), item in family.items() if name == kind]) for kind in MODEL_TYPES},
        "paired": {"reviewed_assets": len(paired), **{kind: _statistics([pair[kind] for _, pair in paired.values()]) for kind in MODEL_TYPES}},
        "versions": [{"model_id": run_id, "model_type": kind, "status": status,
            "created_at": created.isoformat(), **_statistics([item for (_, model), item in versions.items() if model == run_id])}
            for run_id, kind, status, created in runs]}
