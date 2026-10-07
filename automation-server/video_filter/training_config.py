"""One effective, model-specific training contract and deduplication digest."""

import hashlib
import math
from .group_config import training
from .features.contract import canonical_json


def validate_acceptance(values):
    if not isinstance(values, dict) or set(values) != {"like_precision", "dislike_precision"} or any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in values.values()):
        raise ValueError("invalid_training_acceptance")
    return dict(values)


def effective(settings, kind, *, acceptance=None):
    settings = settings or {}
    values = training(settings.get("training", {}))
    values.pop("activation_balanced_accuracy")
    values.pop("activation_roc_auc")
    if acceptance is not None:
        values.update({"activation_" + key: value for key, value in validate_acceptance(acceptance).items()})
    if not settings.get("grouped") and kind == "mil":
        values["mil"].update({k: settings["mil_" + k] for k in values["mil"] if "mil_" + k in settings})
    return {**{k: v for k, v in values.items() if k not in ("logistic_regression", "mil")},
            **values[kind], "model_type": kind}


def digest(settings, kind, *, acceptance=None):
    return hashlib.sha256(canonical_json(effective(settings, kind, acceptance=acceptance)).encode()).hexdigest()
