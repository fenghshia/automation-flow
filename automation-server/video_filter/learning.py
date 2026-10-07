"""Train only from confirmed asset labels; persist numerical parameters, never pickle."""

import hashlib
import json
import logging
import time

import numpy as np
from sqlalchemy import select

from .feature_store import FeatureStore
from .features.contract import MODALITIES, canonical_json
from .models import Asset, FeatureBundle, FeedbackEvent, ModelRun, Variant
from .observability import progress_phase
from .model_registry import SLOTS, model_type as validate_model_type


logger = logging.getLogger(__name__)


def input_vector(bundle):
    arrays = bundle.arrays
    return np.concatenate([np.concatenate((arrays[name + "_mean"], arrays[name + "_std"],
        arrays[name + "_valid"].mean(axis=0))) for name in MODALITIES]).astype(np.float64)


def dataset(session, signature, model_type="logistic_regression"):
    validate_model_type(model_type)
    rows = session.execute(select(Asset, FeatureBundle).join(Variant, Variant.asset_id == Asset.id)
        .join(FeatureBundle, FeatureBundle.variant_id == Variant.id).where(
            Asset.label.is_not(None), FeatureBundle.status == "ready",
            FeatureBundle.feature_signature == signature).order_by(Asset.id, FeatureBundle.created_at.desc(), FeatureBundle.id)).all()
    seen, vectors, labels, snapshot = set(), [], [], []
    for asset, record in rows:
        if asset.id in seen:
            continue
        bundle = FeatureStore().require_ready(session, record.id)
        seen.add(asset.id)
        if model_type == "mil":
            from .mil import window_bag
            vectors.append(window_bag(bundle))
        else:
            vectors.append(input_vector(bundle))
        labels.append(asset.label)
        snapshot.append({"asset_id": asset.id, "label": asset.label,
                         "label_revision": asset.label_revision, "bundle_id": record.id,
                         "manifest_sha256": bundle.manifest_sha256})
    return (vectors if model_type == "mil" else np.asarray(vectors)), np.asarray(labels), snapshot


def lock_assets(session, asset_ids):
    """Acquire every asset lock in one global order, before any model lock."""
    ids = set(asset_ids)
    if not ids:
        return {}
    rows = session.scalars(select(Asset).where(Asset.id.in_(ids)).order_by(Asset.id)
        .with_for_update(of=Asset).execution_options(populate_existing=True)).all()
    return {asset.id: asset for asset in rows}


def _feedback_since(session, snapshot, current):
    changed = {item["asset_id"]: item for item in snapshot if item["asset_id"] in current
               and current[item["asset_id"]]["label_revision"] > item["label_revision"]}
    events = {identifier: [] for identifier in changed}
    if changed:
        rows = session.scalars(select(FeedbackEvent).where(FeedbackEvent.asset_id.in_(changed),
            FeedbackEvent.resulting_revision > min(item["label_revision"] for item in changed.values()))
            .order_by(FeedbackEvent.resulting_revision))
        for event in rows:
            if changed[event.asset_id]["label_revision"] < event.resulting_revision <= current[event.asset_id]["label_revision"]:
                events[event.asset_id].append(event)
    return events


def training_labels_changed(session, snapshot, data):
    """Ignore repeated confirmation, but detect corrections including flip-backs."""
    current = {item["asset_id"]: item for item in data}
    if any(item["asset_id"] not in current or current[item["asset_id"]]["label"] != item["label"] for item in snapshot):
        return True
    events = _feedback_since(session, snapshot, current)
    return any(event.label != item["label"] for item in snapshot for event in events.get(item["asset_id"], ()))


def require_current(session, snapshot, *, lock=True):
    ids = [item["asset_id"] for item in snapshot]
    assets = lock_assets(session, ids) if lock else {a.id: a for a in session.scalars(select(Asset)
        .where(Asset.id.in_(ids)).execution_options(populate_existing=True))}
    current = {identifier: {"label": asset.label, "label_revision": asset.label_revision} for identifier, asset in assets.items()}
    events = _feedback_since(session, snapshot, current)
    for item in snapshot:
        asset = assets.get(item["asset_id"])
        if asset is None or asset.label != item["label"] or asset.label_revision < item["label_revision"]:
            raise ValueError("training_labels_changed")
        if asset.label_revision != item["label_revision"]:
            history = events.get(asset.id, [])
            # Only a complete chain of same-label events can safely advance the
            # audit revision. Actual corrections, gaps and direct edits fail.
            if len(history) != asset.label_revision - item["label_revision"] or any(
                    (event.expected_revision, event.resulting_revision, event.label) !=
                    (item["label_revision"] + offset, item["label_revision"] + offset + 1, item["label"])
                    for offset, event in enumerate(history)):
                raise ValueError("training_labels_changed")


def training_snapshot(session, signature):
    rows = session.execute(select(Asset.id, Asset.label, Asset.label_revision, FeatureBundle.id,
        FeatureBundle.manifest_sha256).join(Variant, Variant.asset_id == Asset.id)
        .join(FeatureBundle, FeatureBundle.variant_id == Variant.id).where(Asset.label.is_not(None),
            FeatureBundle.status == "ready", FeatureBundle.feature_signature == signature)
        .order_by(Asset.id, FeatureBundle.created_at.desc(), FeatureBundle.id)).all()
    result, seen = [], set()
    for asset_id, label, revision, bundle_id, checksum in rows:
        if asset_id not in seen:
            seen.add(asset_id)
            result.append({"asset_id": asset_id, "label": label, "label_revision": revision,
                           "bundle_id": bundle_id, "manifest_sha256": checksum})
    return result


def require_training_snapshot(session, signature, snapshot):
    """Validate the fitted inputs; additional samples belong to the next run."""
    require_current(session, snapshot)
    rows = session.execute(select(Variant.asset_id, FeatureBundle.id, FeatureBundle.manifest_sha256)
        .join(FeatureBundle, FeatureBundle.variant_id == Variant.id).where(
            FeatureBundle.id.in_([item["bundle_id"] for item in snapshot]),
            FeatureBundle.status == "ready", FeatureBundle.feature_signature == signature)).all()
    expected = {(item["asset_id"], item["bundle_id"], item["manifest_sha256"]) for item in snapshot}
    if set(rows) != expected:
        raise ValueError("training_dataset_changed")


def select_threshold(actual, probabilities, requirements):
    from .evaluation import classification_metrics, acceptance_result
    thresholds = np.linspace(0.1, 0.9, 17)
    results = [classification_metrics(actual, probabilities >= value) for value in thresholds]
    qualified = [index for index, metrics in enumerate(results) if acceptance_result(metrics, requirements)["passed"]]
    best = max(qualified or range(len(thresholds)), key=lambda index: results[index]["balanced_accuracy"])
    return float(thresholds[best]), results[best], acceptance_result(results[best], requirements)


def train(session, signature, task_id=None, model_type="logistic_regression", settings=None, *, acceptance=None):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler

    started = time.monotonic()
    validate_model_type(model_type)
    from .training_config import effective, digest
    hyperparameters = effective(settings, model_type, acceptance=acceptance)
    configuration_digest = digest(settings, model_type, acceptance=acceptance)
    logger.info("个人分类器训练开始 | task_id=%s | model_type=%s | feature_signature=%s", task_id or "-", model_type, signature)
    with progress_phase(logger, "读取并校验训练摘要", task_id=task_id):
        x, y, snapshot = dataset(session, signature, model_type)
    # Numerical inputs and their validation snapshot are plain values. Return
    # the DB connection before fitting or waiting for an exclusive GPU lease;
    # require_training_snapshot rechecks them before publishing the model.
    session.commit()
    logger.info("训练样本已加载 | task_id=%s | positives=%s | negatives=%s | assets=%s | input_dimensions=%s",
        task_id or "-", int((y == 1).sum()), int((y == 0).sum()), len(y),
        x.shape[1] if isinstance(x, np.ndarray) and x.ndim == 2 else "window_bags")
    if any(int((y == label).sum()) < hyperparameters["minimum_per_class"] for label in (0, 1)):
        logger.info("训练暂缓：有效摘要样本不足 | task_id=%s | required_per_class=10", task_id or "-")
        raise ValueError("insufficient_confirmed_samples_minimum_10_per_class")
    indices = np.arange(len(y))
    fit, holdout = train_test_split(indices, test_size=hyperparameters["validation_fraction"], random_state=hyperparameters["seed"], stratify=y)
    measurements = {}
    if model_type == "mil":
        if settings is None:
            raise ValueError("mil_runtime_settings_required")
        from .worker_client import run_mil_training
        result = run_mil_training(x, y, fit, holdout, settings, task_id)
        parameters = result["parameters"]
        from .mil import probability, restore
        restore(parameters)  # Reject malformed worker parameters before publication.
        probabilities = np.asarray(result["probabilities"], dtype=np.float64)
        measurements = result["measurements"]
        if probabilities.shape == (len(holdout),) and not np.isclose(
                probability(parameters, x[holdout[0]]), probabilities[0], atol=1e-4, rtol=1e-4):
            raise ValueError("mil_inference_validation_mismatch")
    else:
        scaler = StandardScaler().fit(x[fit])
        classifier = LogisticRegression(**{k: hyperparameters[k] for k in ("C", "max_iter", "class_weight")}, random_state=hyperparameters["seed"])
        hyperparameters["estimator_parameters"] = classifier.get_params()
        with progress_phase(logger, "LogisticRegression 拟合", task_id=task_id):
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=(settings or {}).get("worker_cpu_threads", 1)):
                classifier.fit(scaler.transform(x[fit]), y[fit])
        logger.info("拟合完成，开始验证与选择阈值 | task_id=%s | iterations=%s", task_id or "-", int(classifier.n_iter_[0]))
        probabilities = classifier.predict_proba(scaler.transform(x[holdout]))[:, 1]
        parameters = {"schema": 1, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                      "coef": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0])}
    if probabilities.shape != (len(holdout),) or not np.isfinite(probabilities).all() or ((probabilities < 0) | (probabilities > 1)).any():
        raise ValueError("invalid_validation_probabilities")
    # Prefer thresholds that meet both label precision gates. Use balanced
    # accuracy to retain coverage rather than choosing tiny perfect cohorts.
    requirements = {name + "_precision": hyperparameters["activation_" + name + "_precision"] for name in ("like", "dislike")}
    threshold, metrics, accepted = select_threshold(y[holdout], probabilities, requirements)
    validation = {"method": "stratified_asset_holdout", "seed": hyperparameters["seed"],
        "training_assets": len(fit), "validation_assets": len(holdout),
        "balanced_accuracy": metrics["balanced_accuracy"], "roc_auc": float(roc_auc_score(y[holdout], probabilities)),
        "labels": metrics["labels"], "confusion": metrics["confusion"], "accuracy": metrics["accuracy"],
        "acceptance": accepted, "threshold_selected_on_validation": True, "model_type": model_type,
        "training_asset_ids": [snapshot[i]["asset_id"] for i in fit],
        "validation_asset_ids": [snapshot[i]["asset_id"] for i in holdout], **measurements}
    from importlib.metadata import version
    validation["library_versions"] = {name: version(name) for name in ("numpy", "scikit-learn")}
    hyperparameters["architecture"] = "gated-attention-128-64-v1" if model_type == "mil" else "standardized-logistic-regression"
    if model_type == "mil":
        from .mil import serialize
        payload = serialize(parameters)
    else:
        payload = canonical_json(parameters).encode("utf-8")
    run = ModelRun(model_type=model_type, feature_signature=signature, dataset_snapshot=snapshot, validation=validation,
        hyperparameters=hyperparameters, training_config_digest=configuration_digest,
        threshold=threshold, model_blob=payload, sha256=hashlib.sha256(payload).hexdigest(), status="validated")
    session.add(run)
    logger.info("验证完成，核对标签版本并保存模型 | task_id=%s | balanced_accuracy=%.4f | roc_auc=%.4f | threshold=%.3f",
        task_id or "-", validation["balanced_accuracy"], validation["roc_auc"], threshold)
    require_training_snapshot(session, signature, snapshot)
    if settings and settings.get("grouped"):
        from env import EnvConfig
        current = next((g for g in EnvConfig.video_filter_settings(ignore_scope=True)["groups"] if g["name"] == settings["name"] and g["enabled"]), None)
        if current is None or digest(current, model_type, acceptance=acceptance) != configuration_digest:
            raise ValueError("training_configuration_changed")
    if accepted["passed"]:
        for old in session.execute(select(ModelRun).filter_by(active_slot=SLOTS[model_type]).with_for_update()).scalars():
            old.active_slot, old.status = None, "retired"
        session.flush()
        run.status, run.active_slot = "active", SLOTS[model_type]
    session.commit()
    logger.info("模型已保存到数据库 | task_id=%s | model_type=%s | model_id=%s | status=%s | parameter_bytes=%s | elapsed=%.1fs",
        task_id or "-", model_type, run.id, run.status, len(payload), time.monotonic() - started)
    if run.status != "active":
        logger.warning("模型未激活：标签精确率未达到门槛 | model_id=%s | like_precision=%s/%s | dislike_precision=%s/%s",
            run.id, validation["labels"]["like"]["precision"], requirements["like_precision"],
            validation["labels"]["dislike"]["precision"], requirements["dislike_precision"])
    return run


def require_model(session, model, bundle, *, lock=True):
    """Validate serving identity and parameters; training labels may evolve."""
    from .scope import current_scope
    scope = current_scope()
    if scope and ((model.dataset_group_id, model.reset_epoch) != (scope["id"], scope["epoch"]) or
                  (bundle.manifest.get("dataset_group_id"), bundle.manifest.get("reset_epoch")) != (scope["id"], scope["epoch"])):
        raise ValueError("cross_group_or_stale_epoch_model")
    if model.status != "active" or model.active_slot != SLOTS[model.model_type] or model.feature_signature != bundle.manifest["feature_signature"]:
        raise ValueError("active_compatible_model_required")
    blob = bytes(model.model_blob)
    if hashlib.sha256(blob).hexdigest() != model.sha256:
        raise ValueError("model_checksum_mismatch")
    return blob


def score(session, model, bundle, settings=None, task_id=None, *, validated_blob=None):
    blob = validated_blob if validated_blob is not None else require_model(session, model, bundle)
    if model.model_type == "mil":
        from .mil import window_bag
        from .worker_client import run_mil_prediction
        if settings is None:
            from .scope import current_scope
            scope = current_scope()
            settings = scope["settings"] if scope else None
        if settings is None:
            raise ValueError("mil_runtime_settings_required")
        return run_mil_prediction(blob, window_bag(bundle), settings, task_id)
    parameters = json.loads(blob)
    x = input_vector(bundle)
    mean, scale, coef = [np.asarray(parameters[name], dtype=np.float64) for name in ("mean", "scale", "coef")]
    if parameters["schema"] != 1 or any(value.shape != x.shape or not np.isfinite(value).all() for value in (mean, scale, coef)) or (scale <= 0).any():
        raise ValueError("invalid_model_parameters")
    logit = float(np.dot((x - mean) / scale, coef) + parameters["intercept"])
    if not np.isfinite(logit):
        raise ValueError("invalid_model_score")
    return float(1 / (1 + np.exp(-np.clip(logit, -700, 700))))
