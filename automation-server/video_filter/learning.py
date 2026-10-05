"""Train only from confirmed asset labels; persist numerical parameters, never pickle."""

import hashlib
import json
import logging
import time

import numpy as np
from sqlalchemy import select

from .feature_store import FeatureStore
from .features.contract import MODALITIES, canonical_json
from .models import Asset, FeatureBundle, ModelRun, Variant
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


def require_current(session, snapshot):
    for item in snapshot:
        asset = session.get(Asset, item["asset_id"], populate_existing=True, with_for_update=True)
        if asset is None or (asset.label, asset.label_revision) != (item["label"], item["label_revision"]):
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


def train(session, signature, task_id=None, model_type="logistic_regression", settings=None):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import balanced_accuracy_score, roc_auc_score
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler

    started = time.monotonic()
    validate_model_type(model_type)
    from .training_config import effective, digest
    hyperparameters = effective(settings, model_type)
    configuration_digest = digest(settings, model_type)
    logger.info("个人分类器训练开始 | task_id=%s | model_type=%s | feature_signature=%s", task_id or "-", model_type, signature)
    with progress_phase(logger, "读取并校验训练摘要", task_id=task_id):
        x, y, snapshot = dataset(session, signature, model_type)
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
    # Select a threshold on held-out assets; keep these metrics explicitly labelled.
    thresholds = np.linspace(0.1, 0.9, 17)
    accuracies = [balanced_accuracy_score(y[holdout], probabilities >= value) for value in thresholds]
    threshold = float(thresholds[int(np.argmax(accuracies))])
    validation = {"method": "stratified_asset_holdout", "seed": hyperparameters["seed"],
        "training_assets": len(fit), "validation_assets": len(holdout),
        "balanced_accuracy": float(max(accuracies)), "roc_auc": float(roc_auc_score(y[holdout], probabilities)),
        "threshold_selected_on_validation": True, "activation_gate": hyperparameters["activation_balanced_accuracy"], "model_type": model_type,
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
    require_current(session, snapshot)
    if training_snapshot(session, signature) != snapshot:
        raise ValueError("training_dataset_changed")
    if settings and settings.get("grouped"):
        from env import EnvConfig
        current = next((g for g in EnvConfig.video_filter_settings(ignore_scope=True)["groups"] if g["name"] == settings["name"] and g["enabled"]), None)
        if current is None or digest(current, model_type) != configuration_digest:
            raise ValueError("training_configuration_changed")
    if validation["balanced_accuracy"] >= hyperparameters["activation_balanced_accuracy"] and validation["roc_auc"] >= hyperparameters["activation_roc_auc"]:
        for old in session.execute(select(ModelRun).filter_by(active_slot=SLOTS[model_type]).with_for_update()).scalars():
            old.active_slot, old.status = None, "retired"
        session.flush()
        run.status, run.active_slot = "active", SLOTS[model_type]
    session.commit()
    logger.info("模型已保存到数据库 | task_id=%s | model_type=%s | model_id=%s | status=%s | parameter_bytes=%s | elapsed=%.1fs",
        task_id or "-", model_type, run.id, run.status, len(payload), time.monotonic() - started)
    if run.status != "active":
        logger.warning("模型未激活：验证指标未达到门槛 | model_id=%s | required_balanced_accuracy=0.6 | required_roc_auc=0.6", run.id)
    return run


def score(session, model, bundle):
    from .scope import current_scope
    scope = current_scope()
    if scope and ((model.dataset_group_id, model.reset_epoch) != (scope["id"], scope["epoch"]) or
                  (bundle.manifest.get("dataset_group_id"), bundle.manifest.get("reset_epoch")) != (scope["id"], scope["epoch"])):
        raise ValueError("cross_group_or_stale_epoch_model")
    if model.status != "active" or model.active_slot != SLOTS[model.model_type] or model.feature_signature != bundle.manifest["feature_signature"]:
        raise ValueError("active_compatible_model_required")
    require_current(session, model.dataset_snapshot)
    blob = bytes(model.model_blob)
    if hashlib.sha256(blob).hexdigest() != model.sha256:
        raise ValueError("model_checksum_mismatch")
    if model.model_type == "mil":
        from .mil import deserialize, probability, window_bag
        return probability(deserialize(blob), window_bag(bundle))
    parameters = json.loads(blob)
    x = input_vector(bundle)
    mean, scale, coef = [np.asarray(parameters[name], dtype=np.float64) for name in ("mean", "scale", "coef")]
    if parameters["schema"] != 1 or any(value.shape != x.shape or not np.isfinite(value).all() for value in (mean, scale, coef)) or (scale <= 0).any():
        raise ValueError("invalid_model_parameters")
    logit = float(np.dot((x - mean) / scale, coef) + parameters["intercept"])
    if not np.isfinite(logit):
        raise ValueError("invalid_model_score")
    return float(1 / (1 + np.exp(-np.clip(logit, -700, 700))))
