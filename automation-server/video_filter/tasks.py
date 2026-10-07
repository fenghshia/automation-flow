"""Durable deduplication without worker startup. Caller owns transaction commits."""

import hashlib

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from .features.contract import canonical_json
from .models import Task


def transaction_error_code(error):
    if not isinstance(error, DBAPIError):
        return None
    state = getattr(error.orig, "pgcode", None) or getattr(error.orig, "sqlstate", None)
    return {"40P01": "database_deadlock", "40001": "database_serialization_failure"}.get(state)


def _existing_task(session, task):
    task.enqueue_disposition = "reused"
    # Compatibility with previously recorded OperationalError failures. Only
    # recomputation tasks with identical, freshly checked inputs may auto-retry.
    if task.kind in ("extract", "train", "predict") and task.status == "failed" and task.attempts < 3 and task.error_code in (
            "database_deadlock", "database_serialization_failure", "OperationalError"):
        session.execute(update(Task).where(Task.id == task.id, Task.status == "failed", Task.attempts < 3,
            Task.error_code.in_(("database_deadlock", "database_serialization_failure", "OperationalError"))).values(
                status="queued", error_code=None, claim_token=None, claimed_at=None, finished_at=None,
                heartbeat_at=None, execution_owner=None))
        session.expire(task)
        task.enqueue_disposition = "retry_queued"
    return task


def claim_task(session, task_id=None):
    from uuid import uuid4
    from sqlalchemy import update
    from .models.records import utc_now

    query = select(Task).where(Task.status == "queued")
    if task_id:
        query = query.where(Task.id == task_id)
    candidate = session.execute(query.order_by(Task.created_at).with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
    if candidate is None:
        return None
    token, now = str(uuid4()), utc_now()
    changed = session.execute(update(Task).where(Task.id == candidate.id, Task.status == "queued").values(
        status="running", claim_token=token, claimed_at=now, heartbeat_at=now, attempts=Task.attempts + 1)).rowcount
    session.commit()
    if changed != 1:
        return None
    return session.get(Task, candidate.id, populate_existing=True)


def enqueue_task(session, kind, config_revision_id, input_snapshot, asset_id=None, variant_id=None):
    if kind not in ("scan", "extract", "train", "predict", "classify"):
        raise ValueError("Unknown video_filter task kind.")
    if not isinstance(input_snapshot, dict) or not input_snapshot:
        raise ValueError("A versioned input snapshot is required.")
    values = {
        "kind": kind, "config_revision_id": config_revision_id,
        "input_snapshot": input_snapshot, "asset_id": asset_id, "variant_id": variant_id,
    }
    from .scope import current_scope
    scope = current_scope()
    if scope:
        values["input_snapshot"] = {**input_snapshot, "dataset_group_id": scope["id"], "reset_epoch": scope["epoch"], "group_name": scope["settings"]["name"]}
    dedup_key = hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()
    query = select(Task).filter_by(dedup_key=dedup_key)
    existing = session.execute(query).scalar_one_or_none()
    if existing is not None:
        return _existing_task(session, existing)
    task = Task(dedup_key=dedup_key, **values)
    task.enqueue_disposition = "created"
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        existing = session.execute(query).scalar_one_or_none()
        if existing is None:
            raise
        return _existing_task(session, existing)
    return task
