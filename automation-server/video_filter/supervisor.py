"""Short control ticks, independent sessions and bounded subprocess chains."""

import atexit
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlalchemy import select, update

from .scope import group_scope
from .models import ConfigRevision, DatasetGroup, Task, TransferOperation
from .models.records import utc_now
from .observability import log_failure
from .notifications import DirectoryWatch

logger = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=66, thread_name_prefix="video-filter-supervisor")
_control_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video-filter-reconcile")
_controls = {}
_uncertainty = {}
_running, _scans, _watches = {}, {}, {}
_lock = threading.Lock()
_effective_limit = None
_rotation = 0


def shutdown():
    from .worker_client import cancel_worker
    for identifier in list(_running):
        cancel_worker(identifier)
    for watches in _watches.values():
        for watch in watches[1]:
            watch.close()
    _pool.shutdown(wait=False, cancel_futures=True)
    _control_pool.shutdown(wait=False, cancel_futures=True)


atexit.register(shutdown)


def _scan_group(app, group, notice_state):
    from app import db
    from .tracking import reconcile
    from .feedback import FeedbackJournal
    from .configuration import record_configuration
    from media_lineage.resources import gpu_lock
    from media_lineage.workflows import publish_binding
    with app.app_context():
        try:
            with group_scope(db.session, group, create=False) as scoped, gpu_lock(scoped["state_directory"], name="control.lock") as acquired:
                if not acquired:
                    return
                generation = notice_state["generation"]
                uncertain = generation > notice_state["cleared"]
                FeedbackJournal(scoped["state_directory"]).replay(db.session)
                scan = reconcile(db.session, {**scoped, "notifications_uncertain": uncertain,
                    "notification_guard": lambda: notice_state["generation"] > notice_state["cleared"]})
                revision = record_configuration(db.session, scoped)
                db.session.commit()
                from .group_config import require_current_group
                require_current_group(scoped)
                publish_binding(db.session, scoped, revision.id)
                return generation if uncertain and scan.complete else None
        except Exception as error:
            db.session.rollback()
            log_failure(logger, "分组对账失败 | group=%s", error, group["name"])
        finally:
            db.session.remove()


def _run(app, group, task_id, claim, lease):
    from app import db
    from .runtime import execute
    from .progress import track_task
    from media_lineage.resources import release
    with app.app_context():
        try:
            with group_scope(db.session, group, create=False) as settings:
                task = db.session.get(Task, task_id, populate_existing=True)
                if task is None or task.claim_token != claim or task.status != "running":
                    return
                settings.update(resource_granted=bool(lease), resource_lease_id=lease)
                try:
                    with track_task(settings["state_directory"], task_id):
                        execute(db.session, settings, task)
                    status, code = "succeeded", None
                except Exception as error:
                    db.session.rollback()
                    code = str(error) if isinstance(error, ValueError) and str(error).replace("_", "").isalnum() and len(str(error)) <= 64 else type(error).__name__
                    status = "cancelled" if "changed" in code or "stale_epoch" in code else "failed"
                    log_failure(logger, "任务失败 | group=%s | task_id=%s", error, group["name"], task_id)
                    if "out of memory" in str(error).lower() or "out of memory" in str(error.__cause__).lower():
                        code = "gpu_out_of_memory"
                db.session.execute(update(Task).where(Task.id == task_id, Task.status == "running", Task.claim_token == claim).values(
                    status=status, error_code=code, finished_at=utc_now()))
                db.session.commit()
                logger.info("任务完成 | group=%s | task_id=%s | status=%s | error_code=%s", group["name"], task_id, status, code)
        except Exception as error:
            db.session.rollback()
            log_failure(logger, "执行器失败 | group=%s | task_id=%s", error, group["name"], task_id)
        finally:
            if lease:
                release(db.session, lease)
            db.session.remove()


def snapshot():
    return {"running": len(_running), "effective_concurrency": _effective_limit,
            "tasks": [{"task_id": key, "group": value["group"]["name"], "kind": value["kind"]} for key, value in list(_running.items())]}


def tick(app, settings):
    global _effective_limit, _rotation
    from app import db
    from .runtime import enqueue_automatic
    from .tracking import reconcile
    from .configuration import record_configuration
    from .feedback import FeedbackJournal
    from .tasks import claim_task
    from .worker_client import cancel_worker
    from media_lineage.resources import gpu_lock, gpu_identity, request_lease, heartbeat, release
    from media_lineage.workflows import publish_binding
    if not settings.get("enabled"):
        return
    with _lock, gpu_lock(settings["state_directory"], name="control.lock") as acquired:
        if not acquired:
            return
        configured = {g["name"]: g for g in settings["groups"] if g["enabled"]}
        for name, future in list(_controls.items()):
            if future.done():
                completed_generation = future.result()
                if type(completed_generation) is int and _uncertainty[name]["generation"] == completed_generation:
                    _uncertainty[name]["cleared"] = completed_generation
                _controls.pop(name)
        for name in list(_watches):
            if name not in configured:
                for watch in _watches.pop(name)[1]:
                    watch.close()
        # Administrative cross-group metadata only; never inspect media paths.
        from media_lineage.models import ResourceLease, WorkflowBinding
        active_ids = list(db.session.scalars(select(DatasetGroup.id).where(DatasetGroup.name.in_(list(configured)))))
        table = Task.__table__
        db.session.execute(table.update().where(table.c.status == "queued", table.c.dataset_group_id.not_in(active_ids)).values(
            status="cancelled", error_code="group_disabled", finished_at=utc_now()))
        db.session.execute(update(WorkflowBinding).where(WorkflowBinding.name.not_in(list(configured))).values(enabled=False))
        db.session.execute(update(DatasetGroup).where(DatasetGroup.name.not_in(list(configured))).values(enabled=False))
        db.session.commit()
        for pending in db.session.scalars(select(ResourceLease).where(ResourceLease.status == "waiting")):
            if pending.owner.startswith("filter:"):
                task_id = pending.owner.split(":")[1]
                state = db.session.scalar(select(table.c.status).where(table.c.id == task_id))
                if state not in ("queued", "running"):
                    pending.status = "released"
        db.session.commit()
        for identifier, item in list(_running.items()):
            current = configured.get(item["group"]["name"])
            if current is None or current["directories"] != item["group"]["directories"]:
                cancel_worker(identifier)
                with group_scope(db.session, item["group"], create=False):
                    db.session.execute(update(Task).where(Task.id == identifier, Task.status == "running").values(
                        status="cancelled", error_code="configuration_changed", finished_at=utc_now()))
                    db.session.commit()
            else:
                with group_scope(db.session, item["group"], create=False):
                    db.session.execute(update(Task).where(Task.id == identifier, Task.status == "running").values(heartbeat_at=utc_now()))
                    db.session.commit()
            if item["future"].done():
                item["future"].result()
                with group_scope(db.session, item["group"], create=False) as scoped:
                    task = db.session.get(Task, identifier)
                    if task and task.error_code == "gpu_out_of_memory":
                        active_count = sum(value["kind"] == "extract" for value in _running.values())
                        previous_limit = min(_effective_limit or settings["extract_concurrency"], active_count)
                        _effective_limit = max(1, previous_limit - 1)
                        if task.attempts < 3 and previous_limit > 1:
                            task.status, task.claim_token, task.error_code = "queued", None, None
                            db.session.commit()
                        logger.warning("GPU OOM 降低实际准入 | configured=%s | effective=%s", settings["extract_concurrency"], _effective_limit)
                _running.pop(identifier)
            elif item["lease"]:
                heartbeat(db.session, item["lease"])
        names = list(configured)
        if not names:
            return
        _rotation = (_rotation + 1) % len(names)
        names = names[_rotation:] + names[:_rotation]
        for name in names:
            group = configured[name]
            try:
                with group_scope(db.session, group) as scoped:
                    revision = record_configuration(db.session, scoped)
                    db.session.commit()
                    roots = tuple(scoped["directories"].values())
                    old = _watches.get(name)
                    if old is None or old[0] != roots:
                        if old:
                            for watch in old[1]:
                                watch.close()
                        _watches[name] = (roots, [DirectoryWatch(root) for root in roots])
                    hints = [w.poll() for w in _watches[name][1]]
                    notice_state = _uncertainty.setdefault(name, {"generation": 0, "cleared": 0})
                    if any(overflow for changed, overflow in hints):
                        notice_state["generation"] += 1
                    due = time.monotonic() - _scans.get(name, 0) >= scoped["scan_interval_seconds"]
                    if name not in _controls and (due or any(changed for changed, overflow in hints) or revision.active_slot != "active" or notice_state["generation"] > notice_state["cleared"]):
                        _controls[name] = _control_pool.submit(_scan_group, app, group, notice_state)
                        _scans[name] = time.monotonic()
                    # Interrupted process recovery needs proof of owner death, not a lease timeout.
                    from media_lineage.resources import process_identity
                    for task in db.session.execute(select(Task).where(Task.status == "running")).scalars():
                        owner = task.execution_owner or {}
                        if owner and process_identity(owner["pid"]) != owner["identity"]:
                            task.status, task.error_code, task.finished_at = "failed", "interrupted_worker", utc_now()
                    db.session.commit()
                    from .transfer import advance_transfer
                    for operation in db.session.scalars(select(TransferOperation).where(TransferOperation.status.in_(("planned", "destination_verified", "published"))).limit(10)):
                        task = db.session.get(Task, operation.task_id)
                        if task is None or task.status == "running":
                            continue
                        try:
                            advance_transfer(db.session, scoped, task, operation)
                            task.status, task.finished_at = "succeeded", utc_now()
                            db.session.commit()
                        except Exception as error:
                            db.session.rollback()
                            operation.status = "conflict"
                            db.session.commit()
                            log_failure(logger, "搬运恢复保留冲突 | operation_id=%s", error, operation.id)
                    if len(_running) >= settings["extract_concurrency"] + 2:
                        continue
                    enqueue_automatic(db.session, scoped)
                    queued = db.session.execute(select(Task).where(Task.status == "queued").order_by(Task.created_at).limit(20)).scalars().all()
                    for candidate in queued:
                        if candidate.kind == "train" and any(v["kind"] == "train" and v["group"]["name"] == name and v["model_type"] == candidate.input_snapshot.get("model_type") for v in _running.values()):
                            continue
                        mode = "extract_shared" if candidate.kind == "extract" else "exclusive_train" if candidate.kind == "train" and candidate.input_snapshot.get("model_type") == "mil" else None
                        lease = None
                        if mode:
                            device = gpu_identity(scoped["device"])
                            owner = "filter:" + candidate.id + ":" + str(os.getpid()) + ":" + process_identity(os.getpid())
                            lease = request_lease(db.session, device, mode, owner,
                                min(settings["extract_concurrency"], _effective_limit or settings["extract_concurrency"]))
                            if not lease:
                                continue
                        task = claim_task(db.session, candidate.id)
                        if task is None:
                            if lease:
                                release(db.session, lease)
                            continue
                        task.execution_owner = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
                        db.session.commit()
                        identifier, token = task.id, task.claim_token
                        kind, model = task.kind, task.input_snapshot.get("model_type")
                        _running[identifier] = {"future": _pool.submit(_run, app, group, identifier, token, lease),
                            "group": group, "kind": kind, "model_type": model, "lease": lease}
                        logger.info("任务开始 | group=%s | task_id=%s | kind=%s | concurrent=%s | configured=%s", name, identifier, kind, len(_running), settings["extract_concurrency"])
                        break
            except Exception as error:
                db.session.rollback()
                log_failure(logger, "分组控制失败 | group=%s", error, name)
