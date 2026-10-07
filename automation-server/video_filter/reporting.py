"""Read-only metadata snapshots shared by JSON status and the Jinja dashboard."""

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Integer, func, select

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
        "automatic_training": False,
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
    queue = {kind: {"queued": queued, "running": active} for kind, queued, active in session.execute(
        select(Task.kind, func.sum((Task.status == "queued").cast(Integer)),
            func.sum((Task.status == "running").cast(Integer))).where(Task.status.in_(("queued", "running"))).group_by(Task.kind))}
    oldest = session.scalar(select(func.min(Task.created_at)).where(Task.status == "queued"))
    return {"tasks": tasks, "queue": queue,
            "oldest_wait_seconds": max(0, int((now - oldest.replace(tzinfo=timezone.utc)).total_seconds())) if oldest else 0,
            "current_task": next((task for task in tasks if task["status"] == "running"), None),
            "running_tasks": [task for task in tasks if task["status"] == "running"][:6],
            "last_scan": {"at": iso(last_scan[0]), "complete": last_scan[1]} if last_scan else None,
            "updated_at": now.isoformat()}


def training_details(session, settings):
    """Training metadata only; never deserialize model or feature payloads."""
    from .model_registry import MODEL_TYPES
    from .training_config import effective
    history = [dict(row._mapping) for row in session.execute(select(
        ModelRun.id, ModelRun.model_type, ModelRun.status, ModelRun.threshold,
        ModelRun.validation, ModelRun.created_at).order_by(ModelRun.created_at.desc(), ModelRun.id.desc()).limit(20))]
    for run in history:
        run["created_at"] = iso(run["created_at"])
    latest = {}
    for kind in MODEL_TYPES:
        row = session.execute(select(ModelRun.id, ModelRun.status, ModelRun.validation)
            .where(ModelRun.model_type == kind).order_by(ModelRun.created_at.desc(), ModelRun.id.desc()).limit(1)).first()
        latest[kind] = dict(row._mapping) if row else None
    now, tasks = datetime.now(timezone.utc), []
    rows = session.execute(select(Task.id, Task.status, Task.created_at, Task.claimed_at,
        Task.finished_at, Task.error_code, Task.input_snapshot).where(Task.kind == "train")
        .order_by((Task.status == "running").desc(), Task.created_at.desc(), Task.id.desc()).limit(20))
    for identifier, status, created, claimed, finished, error, inputs in rows:
        progress = read_progress(settings["state_directory"], identifier) if status == "running" else None
        elapsed = max(0, int(((finished.replace(tzinfo=timezone.utc) if finished else now)
            - claimed.replace(tzinfo=timezone.utc)).total_seconds())) if claimed else None
        tasks.append({"id": identifier, "status": status, "model_type": inputs.get("model_type", "logistic_regression"),
            "created_at": iso(created), "elapsed": elapsed, "error_code": error, "progress": progress,
            "acceptance": inputs.get("acceptance")})
    pending = list(session.scalars(select(Task.input_snapshot)
        .where(Task.kind == "train", Task.status.in_(("queued", "running")))))
    return {"training_history": history, "latest_training": latest, "training_tasks": tasks,
        "training_pending": {item.get("model_type", "logistic_regression") for item in pending},
        "training_settings": {kind: effective(settings, kind) for kind in MODEL_TYPES},
        "updated_at": now.isoformat()}


def groups_snapshot(session, settings):
    from .models import DatasetGroup
    from .scope import group_scope
    from .supervisor import snapshot
    from media_lineage.models import ResourceLease
    from media_lineage.resources import admission_decisions
    leases = session.scalars(select(ResourceLease).where(
        ResourceLease.status.in_(("waiting", "active", "conflict")))).all()
    resources = {"extract_active": 0, "extract_waiting": 0, "exclusive_active": 0, "exclusive_waiting": 0,
                 "compression_active": 0, "compression_waiting": 0, "conflicts": 0}
    for lease in leases:
        mode, status = lease.mode, lease.status
        if status == "conflict":
            resources["conflicts"] += 1
        else:
            suffix = "active" if status == "active" else "waiting"
            resources[("extract_" if mode == "extract_shared" else "exclusive_") + suffix] += 1
            if mode == "extract_shared" and (lease.memory_observation or {}).get("workload_type") == "compression":
                resources["compression_" + suffix] += 1
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
            "resources": {**snapshot(), **resources, "admission": list(admission_decisions().values()),
                "configured_concurrency": settings.get("extract_concurrency", 6)}}
