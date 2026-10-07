"""Pure configuration validation. No database, scheduler or media enumeration."""

import json
import math
import os
import stat
from pathlib import Path

ROLES = ("unclassified", "liked", "predicted_like", "predicted_dislike", "liked_source")
SAMPLE_ROLES = frozenset(ROLES[:-1])
TRAIN_DEFAULTS = {"minimum_per_class": 10, "seed": 1729, "validation_fraction": .3,
    "activation_like_precision": .8, "activation_dislike_precision": .8,
    # Accepted for compatibility with existing group JSON; no longer gates.
    "activation_balanced_accuracy": .6, "activation_roc_auc": .6,
    "logistic_regression": {"C": .1, "max_iter": 2000, "class_weight": "balanced"},
    "mil": {"epochs": 60, "patience": 8, "max_train_windows": 512,
            "learning_rate": .0003, "weight_decay": .001, "dropout": .2,
            "encoder_width": 128, "attention_width": 64}}


def reject_links(path):
    from media_lineage.files import reject_media_links
    return reject_media_links(path)


def anchored(value, base):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("directory_value_required")
    path = Path(value)
    return reject_links(path if path.is_absolute() else base / path).resolve()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_configuration_key")
        result[key] = value
    return result


def training(values):
    if not isinstance(values, dict) or set(values) - set(TRAIN_DEFAULTS):
        raise ValueError("invalid_training_configuration")
    result = {}
    for key, default in TRAIN_DEFAULTS.items():
        value = values.get(key, default)
        if isinstance(default, dict):
            if not isinstance(value, dict) or set(value) - set(default):
                raise ValueError("invalid_model_hyperparameters")
            result[key] = {**default, **value}
        else:
            result[key] = value
    numeric = [result[key] for key in ("minimum_per_class", "seed", "validation_fraction", "activation_balanced_accuracy", "activation_roc_auc",
                                     "activation_like_precision", "activation_dislike_precision")]
    numeric += [v for block in ("mil", "logistic_regression") for k, v in result[block].items() if k != "class_weight"]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in numeric):
        raise ValueError("invalid_numeric_hyperparameter")
    if type(result["seed"]) is not int or result["seed"] < 0 or result["seed"] >= 2**32:
        raise ValueError("invalid_training_seed")
    for key, value in [("minimum_per_class", result["minimum_per_class"]), ("max_iter", result["logistic_regression"]["max_iter"]),
                       *[(k, result["mil"][k]) for k in ("epochs", "patience", "max_train_windows", "encoder_width", "attention_width")]]:
        if type(value) is not int or value <= 0:
            raise ValueError("positive_integer_hyperparameter_required")
    if result["minimum_per_class"] < 10 or not 0 < result["validation_fraction"] < 1:
        raise ValueError("invalid_training_sample_gate")
    if any(not 0 <= result[k] <= 1 for k in ("activation_balanced_accuracy", "activation_roc_auc",
                                          "activation_like_precision", "activation_dislike_precision")):
        raise ValueError("invalid_activation_gate")
    lr, mil = result["logistic_regression"], result["mil"]
    if lr["C"] <= 0 or lr["class_weight"] not in (None, "balanced") or mil["learning_rate"] <= 0 or mil["weight_decay"] < 0 or not 0 <= mil["dropout"] < 1:
        raise ValueError("invalid_model_hyperparameters")
    # Numerical MIL artifact v1 has a fixed, checked architecture.
    if (mil["encoder_width"], mil["attention_width"]) != (128, 64):
        raise ValueError("unsupported_mil_architecture")
    return result


def load_groups(path, base, shared):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("groups_configuration_too_large")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    if not isinstance(value, dict) or set(value) != {"schema_version", "groups"} or type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["groups"], list) or len(value["groups"]) > 100:
        raise ValueError("invalid_groups_configuration")
    result, names, roots = [], set(), []
    for item in value["groups"]:
        if not isinstance(item, dict) or set(item) - {"name", "enabled", "directories", "classifier", "transfer_enabled", "compression_enabled", "training"}:
            raise ValueError("invalid_group_configuration")
        name = item.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not all(c.isalnum() or c in "_-" for c in name) or name.casefold() in names:
            raise ValueError("invalid_or_duplicate_group_name")
        names.add(name.casefold())
        directories = item.get("directories")
        if not isinstance(directories, dict) or set(directories) != set(ROLES):
            raise ValueError("five_group_directories_required")
        directories = {role: anchored(p, base) for role, p in directories.items()}
        if any(path.exists() and not path.is_dir() for path in directories.values()):
            raise ValueError("group_root_must_be_directory")
        for field in ("enabled", "transfer_enabled", "compression_enabled"):
            if type(item.get(field, field == "enabled")) is not bool:
                raise ValueError("group_boolean_required")
        classifier = item.get("classifier", "logistic_regression")
        if classifier not in ("logistic_regression", "mil"):
            raise ValueError("invalid_classifier_type")
        group = {**shared, "grouped": True, "name": name, "enabled": item.get("enabled", True), "directories": directories,
                 "classifier": classifier, "transfer_enabled": item.get("transfer_enabled", False),
                 "compression_enabled": item.get("compression_enabled", False), "lineage_enabled": True,
                 "deletion_feedback_enabled": True, "training": training(item.get("training", {}))}
        group.update({"mil_" + key: v for key, v in group["training"]["mil"].items()})
        result.append(group)
        if group["enabled"]:
            roots.extend(directories.values())
    roots.extend(p for p in (shared.get("state_directory"), shared.get("model_manifest", Path(".")).parent if shared.get("model_manifest") else None) if p)
    for i, first in enumerate(roots):
        for second in roots[i + 1:]:
            a, b = first.resolve(), second.resolve()
            if a == b or a in b.parents or b in a.parents or (a.exists() and b.exists() and a.samefile(b)):
                raise ValueError("group_directories_overlap")
    return result


def require_scope(settings, path, *, sample=False):
    candidate = reject_links(path)
    parent = candidate.parent.resolve()
    roles = SAMPLE_ROLES if sample else ROLES
    role = next((r for r in roles if settings["directories"].get(r) is not None and
                parent == reject_links(settings["directories"][r]).resolve()), None)
    if role is None:
        raise ValueError("media_outside_group_scope")
    return role


def require_current_group(settings):
    from .scope import isolated_test
    # Legacy isolated fixtures deliberately have no live JSON configuration.
    if isolated_test() and not os.getenv("VIDEO_FILTER_GROUPS_CONFIG"):
        return
    from env import EnvConfig
    current = next((g for g in EnvConfig.video_filter_settings(ignore_scope=True).get("groups", [])
                    if g["name"] == settings["name"] and g["enabled"]), None)
    if current is None or current["directories"] != settings["directories"]:
        raise ValueError("configuration_changed")
