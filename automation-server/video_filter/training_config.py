"""One effective, model-specific training contract and deduplication digest."""

import hashlib
from .group_config import training
from .features.contract import canonical_json


def effective(settings, kind):
    settings = settings or {}
    values = training(settings.get("training", {}))
    if not settings.get("grouped") and kind == "mil":
        values["mil"].update({k: settings["mil_" + k] for k in values["mil"] if "mil_" + k in settings})
    return {**{k: v for k, v in values.items() if k not in ("logistic_regression", "mil")},
            **values[kind], "model_type": kind}


def digest(settings, kind):
    return hashlib.sha256(canonical_json(effective(settings, kind)).encode()).hexdigest()
