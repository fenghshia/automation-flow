"""Read-only metadata snapshots shared by JSON status and the Jinja dashboard."""

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from .evaluation import actual_metrics
from .features.contract import FeatureSignature
from .models import Asset, FeatureBundle, Location, ModelRun, ScanRun, Task, TransferOperation, Variant
from .progress import read_progress


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


def status_snapshot(session, settings, include_versions=False):
    # A schema probe, no numerical payloads are loaded by dashboard polling.
    session.execute(select(FeatureBundle.arrays_blob, FeatureBundle.manifest).limit(0))
    def count(table, *conditions):
        return session.scalar(select(func.count()).select_from(table).where(*conditions))
    counts = {
        "positive_assets": count(Asset, Asset.label == 1), "negative_assets": count(Asset, Asset.label == 0),
        "ready_summaries": count(FeatureBundle, FeatureBundle.status == "ready"),
        "queued_tasks": count(Task, Task.status == "queued"), "running_tasks": count(Task, Task.status == "running"),
        "failed_tasks": count(Task, Task.status == "failed"),
        "tracking_conflicts": count(Location, Location.status == "conflict"),
        "transfer_conflicts": count(TransferOperation, TransferOperation.status == "conflict"),
        "present_locations": count(Location, Location.status == "present"),
        "missing_locations": count(Location, Location.status == "missing"),
    }
    current_signature = None
    if settings.get("model_manifest"):
        try:
            path = settings["model_manifest"]
            if path.stat().st_size <= 1024 * 1024:
                current_signature = FeatureSignature(**json.loads(path.read_text(encoding="utf-8"))["specification"]).digest
        except (OSError, ValueError, KeyError, TypeError):
            pass
    present = select(Location.variant_id).where(Location.status == "present", Location.role != "liked_source").distinct().subquery()
    eligible = select(FeatureBundle.variant_id).where(FeatureBundle.status == "ready", FeatureBundle.feature_signature == current_signature).distinct().subquery()
    counts["present_variants"] = session.scalar(select(func.count()).select_from(present))
    counts["summarized_present_variants"] = session.scalar(select(func.count()).select_from(present).join(eligible, eligible.c.variant_id == present.c.variant_id))
    valid_samples = session.execute(select(Asset.label, func.count(func.distinct(Asset.id))).join(Variant, Variant.asset_id == Asset.id)
        .join(FeatureBundle, FeatureBundle.variant_id == Variant.id).where(Asset.label.is_not(None), FeatureBundle.status == "ready",
            FeatureBundle.feature_signature == current_signature).group_by(Asset.label)).all()
    counts["trainable_positive_assets"] = dict(valid_samples).get(1, 0)
    counts["trainable_negative_assets"] = dict(valid_samples).get(0, 0)
    rows = session.execute(select(ModelRun.id, ModelRun.model_type, ModelRun.threshold, ModelRun.validation, ModelRun.feature_signature, ModelRun.hyperparameters, ModelRun.training_config_digest)
                           .where(ModelRun.status == "active", ModelRun.active_slot.is_not(None))).all()
    models = {kind: {"id": identifier, "threshold": threshold, "validation": validation,
                    "feature_compatible": signature == current_signature, "hyperparameters": parameters,
                    "training_config_digest": digest} for identifier, kind, threshold, validation, signature, parameters, digest in rows}
    selected = settings.get("classifier", "logistic_regression")
    model = models.get(selected)
    metrics = actual_metrics(session, include_versions=include_versions)
    actual = {"models": metrics["models"], "paired": metrics["paired"]}
    if include_versions:
        actual["versions"] = metrics["versions"]
    return {"enabled": True, "configuration_valid": True, "implementation_stage": "P8",
        "group_name": settings.get("name"), "dataset_group_id": settings.get("dataset_group_id"),
        "reset_epoch": settings.get("reset_epoch"),
        "automatic_processing": bool(settings.get("transfer_enabled") and model and model["feature_compatible"]),
        "transfer_enabled": bool(settings.get("transfer_enabled")), "summary_storage": "database", "counts": counts,
        "summary_coverage": counts["summarized_present_variants"] / counts["present_variants"] if counts["present_variants"] else None,
        "model": {key: model[key] for key in ("id", "validation", "threshold")} if model else None,
        "selected_classifier": selected, "models": models, "actual_accuracy": actual,
        "model_manifest_configured": settings.get("model_manifest") is not None,
        "feedback_journal_pending": len(list((settings["state_directory"] / "feedback").glob("*.json"))) if settings.get("state_directory") else 0,
        "role_accessible": {role: path.is_dir() for role, path in settings["directories"].items()}}


def dashboard_details(session, settings):
    now = datetime.now(timezone.utc)
    tasks = []
    rows = session.execute(select(Task.id, Task.kind, Task.status, Task.created_at, Task.claimed_at, Task.finished_at,
        Task.attempts, Task.error_code, Task.input_snapshot)
        .order_by((Task.status == "running").desc(), Task.created_at.desc(), Task.id).limit(20)).all()
    for identifier, kind, status, created, claimed, finished, attempts, error, inputs in rows:
        name = Path(inputs.get("path", "")).name if inputs.get("path") else None
        progress = read_progress(settings["state_directory"], identifier) if status == "running" else None
        if progress:
            completed, total = progress.get("completed", progress.get("epoch")), progress.get("total", progress.get("epochs"))
            progress["percent"] = min(100, max(0, 100 * completed / total)) if type(completed) in (int, float) and type(total) in (int, float) and total > 0 else None
        elapsed = max(0, int(((finished.replace(tzinfo=timezone.utc) if finished else now) - claimed.replace(tzinfo=timezone.utc)).total_seconds())) if claimed else None
        tasks.append({"id": identifier, "kind": kind, "status": status, "created_at": iso(created), "elapsed": elapsed,
            "attempts": attempts, "error_code": error, "file_name": name, "model_type": inputs.get("model_type"), "progress": progress})
    last_scan = session.execute(select(ScanRun.finished_at, ScanRun.complete).order_by(ScanRun.created_at.desc()).limit(1)).first()
    return {"tasks": tasks, "current_task": next((task for task in tasks if task["status"] == "running"), None),
            "running_tasks": [task for task in tasks if task["status"] == "running"][:6],
            "last_scan": {"at": iso(last_scan[0]), "complete": last_scan[1]} if last_scan else None,
            "updated_at": now.isoformat()}


def groups_snapshot(session, settings):
    from .models import DatasetGroup
    from .scope import group_scope
    from .supervisor import snapshot
    from media_lineage.models import ResourceLease
    lease_counts = session.execute(select(ResourceLease.mode, ResourceLease.status, func.count()).where(
        ResourceLease.status.in_(("waiting", "active", "conflict"))).group_by(ResourceLease.mode, ResourceLease.status)).all()
    resources = {"extract_active": 0, "extract_waiting": 0, "exclusive_active": 0, "exclusive_waiting": 0, "conflicts": 0}
    for mode, status, count in lease_counts:
        if status == "conflict":
            resources["conflicts"] += count
        else:
            resources[("extract_" if mode == "extract_shared" else "exclusive_") + ("active" if status == "active" else "waiting")] += count
    groups = []
    running = []
    for group in settings.get("groups", []):
        row = session.execute(select(DatasetGroup).where(DatasetGroup.name == group["name"])).scalar_one_or_none()
        item = {"name": group["name"], "enabled": group["enabled"], "classifier": group["classifier"],
                "dataset_group_id": row.id if row else None, "initialized": row is not None}
        if row:
            with group_scope(session, group, create=False) as scoped:
                item.update(status_snapshot(session, scoped))
                details = dashboard_details(session, scoped)
                running.extend({**task, "group_name": group["name"]} for task in details["running_tasks"])
        groups.append(item)
    return {"enabled": settings.get("enabled", False), "groups": groups, "running_tasks": running[:6],
            "resources": {**snapshot(), **resources, "configured_concurrency": settings.get("extract_concurrency", 6)}}
