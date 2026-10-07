"""Short control ticks, independent sessions and bounded subprocess chains."""

import atexit
import logging
import os
import threading
import time
import hashlib
import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone, timedelta
from logging_config import log_performance
from sqlalchemy import select, update, func

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
_stopping = threading.Event()
_effective_limit = None
_rotation = 0
_recovery_successes = 0
_last_oom = 0
_last_control = {}
_tick_active = threading.Event()
_wake_requested = threading.Event()
_wake_lock = threading.Lock()


def wake_control(app):
    """Coalesce completion hints; APScheduler still owns the single control job."""
    if _stopping.is_set():
        return
    with _wake_lock:
        already_pending = _wake_requested.is_set()
        _wake_requested.set()
        should_wake = not already_pending and not _tick_active.is_set()
    if should_wake:
        _schedule_control(app)


def _schedule_control(app):
    from app import scheduler
    if _stopping.is_set() or not scheduler.running:
        return
    try:
        if scheduler.get_job("video_filter_process_one") is not None:
            scheduler.modify_job("video_filter_process_one", next_run_time=datetime.now(timezone.utc) + timedelta(milliseconds=50))
            log_performance("control_wakeup")
    except Exception as error:
        # The periodic two-second job remains the fallback during shutdown/races.
        log_failure(logger, "完成通知补位失败，等待定期控制轮", error)


def tick(app, settings):
    from media_lineage.resources import admission_round, gpu_identity, sample_gpu
    from .persistent_client import reap_idle
    reap_idle()
    if not settings.get("enabled") or _stopping.is_set():
        return
    with _wake_lock:
        _tick_active.set()
        _wake_requested.clear()
    try:
        with admission_round():
            if str(settings.get("device", "cpu")).startswith("cuda:"):
                try:
                    sample_gpu(gpu_identity(settings["device"]))
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    log_performance("gpu_sample_unavailable", error_type=type(error).__name__)
            _tick(app, settings)
    finally:
        with _wake_lock:
            _tick_active.clear()
            followup = _wake_requested.is_set()
        if followup:
            _schedule_control(app)


def resource_mode(kind, inputs):
    if kind == "classify" and inputs.get("prediction_ids"):
        return None
    if kind == "extract" or (kind in ("predict", "classify") and
            ("mil" in inputs.get("model_ids", {}) or inputs.get("selected_classifier") == "mil")):
        return "extract_shared"
    if kind == "train" and inputs.get("model_type") == "mil":
        return "exclusive_train"
    return None


def shutdown():
    from .worker_client import cancel_worker
    _stopping.set()
    with _lock:
        for identifier in list(_running):
            cancel_worker(identifier)
        for watches in _watches.values():
            for watch in watches[1]:
                watch.close()
        _pool.shutdown(wait=False, cancel_futures=True)
        _control_pool.shutdown(wait=False, cancel_futures=True)
    from .persistent_client import shutdown as shutdown_workers
    shutdown_workers()


def _submit(pool, function, *args):
    if _stopping.is_set():
        return None
    try:
        return pool.submit(function, *args)
    except RuntimeError as error:
        # The interpreter can close executors before ordinary atexit handlers.
        if not str(error).startswith("cannot schedule new futures after"):
            raise
        _stopping.set()
        logger.info("执行器正在关闭，本轮停止提交新任务")
        return None


def _return_unstarted_task(session, identifier, token, lease):
    from media_lineage.resources import release
    session.execute(update(Task).where(Task.id == identifier, Task.status == "running",
        Task.claim_token == token).values(status="queued", claim_token=None, claimed_at=None,
            heartbeat_at=None, execution_owner=None, finished_at=None, attempts=Task.attempts - 1))
    session.commit()
    if lease:
        release(session, lease)


atexit.register(shutdown)


def _scan_group(app, group, notice_state):
    if _stopping.is_set():
        return None
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


def _run(app, group, task_id, claim, lease, dispatched=None):
    if dispatched is not None:
        dispatched.wait()
    from app import db
    from .runtime import execute, task_failure, task_result, DEFERRED_TASK_REASONS
    from .progress import track_task
    from media_lineage.resources import release
    started = time.monotonic()
    phase_owners = set()
    def gpu_control(record):
        nonlocal lease
        from media_lineage.resources import request_lease, gpu_identity, process_identity, heartbeat, record_oom
        from .worker_client import worker_processes

        action, phase, size = record.get("action"), record.get("phase"), record.get("batch_size")
        with _lock, app.app_context():
            try:
                item = _running.get(task_id)
                if action == "cleanup":
                    for owner in phase_owners:
                        release(db.session, owner=owner)
                    lease = None
                    if item:
                        item.update(lease=None, resource_mode=None, phase=None)
                    wake_control(app)
                    return True
                if action not in ("acquire", "release", "failed") or phase not in ("dino", "videomae", "beats", "mil_train", "mil_predict") or type(size) is not int or not 1 <= size <= 64:
                    raise ValueError("invalid_gpu_phase_request")
                if action in ("release", "failed"):
                    if not lease or not item or item.get("phase") != phase:
                        raise ValueError("gpu_phase_lease_mismatch")
                    if record.get("oom"):
                        record_oom(db.session, lease)
                        item["gpu_concurrency_at_oom"] = sum(v["kind"] == "extract" and bool(v["lease"]) for v in _running.values())
                    if action == "release":
                        release(db.session, lease)
                        lease = None
                        item.update(lease=None, resource_mode=None, phase=None)
                        wake_control(app)
                    return True
                if lease or _stopping.is_set() or not item:
                    raise ValueError("gpu_phase_owner_unavailable")
                with group_scope(db.session, group, create=False) as scoped:
                    current = db.session.get(Task, task_id, populate_existing=True)
                    if current is None or current.claim_token != claim or current.status != "running":
                        raise ValueError("gpu_phase_task_cancelled")
                    allowed = {"extract": {"dino", "videomae", "beats"}, "train": {"mil_train"},
                               "predict": {"mil_predict"}, "classify": {"mil_predict"}}
                    if phase not in allowed.get(current.kind, set()):
                        raise ValueError("gpu_phase_task_mismatch")
                    device = gpu_identity(scoped["device"])
                    owner = "filter:" + task_id + ":" + str(os.getpid()) + ":" + process_identity(os.getpid()) + ":" + phase + ":" + str(size)
                    phase_owners.add(owner)
                    mode = "exclusive_train" if phase == "mil_train" else "extract_shared"
                    workload = "extract" if current.kind == "extract" else "predict"
                    base = scoped.get("gpu_extract_peak_mib", 1024) if workload == "extract" else (item.get("gpu_peak") or scoped.get("gpu_predict_peak_mib", 256))
                    # Preserve the configured initial budget; batch-dependent
                    # driver peaks and OOM feedback update matching profiles.
                    peak = base
                    profile = hashlib.sha256(((item.get("gpu_profile") or "") + ":" + phase + ":" + str(size)).encode()).hexdigest()
                    lease = request_lease(db.session, device, mode, owner,
                        min(scoped.get("extract_concurrency", 6), _effective_limit or scoped.get("extract_concurrency", 6)),
                        memory_budget_mib=peak, reserve_mib=scoped.get("gpu_safety_mib", 1024),
                        workload_type=workload, profile_key=profile)
                    if lease:
                        item.update(lease=lease, resource_mode=mode, phase=phase)
                        heartbeat(db.session, lease, processes=worker_processes(task_id))
                    return bool(lease)
            finally:
                db.session.remove()
    with app.app_context():
        try:
            with group_scope(db.session, group, create=False) as settings:
                task = db.session.get(Task, task_id, populate_existing=True)
                if task is None or task.claim_token != claim or task.status != "running":
                    return
                settings.update(resource_granted=bool(lease), resource_lease_id=lease)
                def release_gpu():
                    nonlocal lease
                    if lease:
                        # Serialize the phase change with the control tick so a
                        # heartbeat cannot read a half-released lease handle.
                        with _lock:
                            item = _running.get(task_id)
                            if item:
                                item.update(lease=None, resource_mode=None)
                        release(db.session, lease)
                        lease = None
                        settings.update(resource_granted=False, resource_lease_id=None)
                settings["release_gpu"] = release_gpu
                if task.kind in ("extract", "predict", "classify") or (task.kind == "train" and task.input_snapshot.get("model_type") == "mil"):
                    settings["gpu_control"] = gpu_control
                try:
                    with track_task(settings["state_directory"], task_id):
                        execute(db.session, settings, task)
                    status, code = "succeeded", None
                except Exception as error:
                    db.session.rollback()
                    status, code, expected = task_failure(error, task.attempts)
                    if status == "queued" and code in DEFERRED_TASK_REASONS:
                        logger.info("任务暂缓，等待反馈重放后继续 | group=%s | task_id=%s | reason=%s", group["name"], task_id, code)
                    elif expected:
                        logger.info("任务输入已过期，已取消，后续按最新数据发现任务 | group=%s | task_id=%s | reason=%s", group["name"], task_id, code)
                    else:
                        log_failure(logger, "任务失败 | group=%s | task_id=%s", error, group["name"], task_id)
                    if "out of memory" in str(error).lower() or "out of memory" in str(error.__cause__).lower():
                        code = "gpu_out_of_memory"
                        if lease:
                            from media_lineage.resources import record_oom
                            record_oom(db.session, lease)
                db.session.execute(update(Task).where(Task.id == task_id, Task.status == "running", Task.claim_token == claim).values(
                    **task_result(status, code)))
                db.session.commit()
                if status == "queued" and code not in DEFERRED_TASK_REASONS:
                    logger.warning("数据库事务冲突，任务已回滚并重新入队 | task_id=%s | attempt=%s/3 | reason=%s", task_id, task.attempts, code)
                logger.info("任务完成 | group=%s | task_id=%s | status=%s | error_code=%s", group["name"], task_id, status, code)
        except Exception as error:
            db.session.rollback()
            log_failure(logger, "执行器失败 | group=%s | task_id=%s", error, group["name"], task_id)
        finally:
            if lease:
                release(db.session, lease)
            for owner in phase_owners:
                release(db.session, owner=owner)
            db.session.remove()
            log_performance("task_finished", task_id=task_id,
                status=locals().get("status", "not_executed"), error_code=locals().get("code"),
                dataset_group_id=locals().get("settings", {}).get("dataset_group_id"),
                reset_epoch=locals().get("settings", {}).get("reset_epoch"),
                elapsed_seconds=round(time.monotonic() - started, 4))


def snapshot():
    items = list(_running.items())
    return {"running": len(items), "effective_concurrency": _effective_limit,
            "extract_running": sum(v["kind"] == "extract" and not v["future"].done() for _, v in items),
            "other_running": sum(v["kind"] != "extract" and not v["future"].done() for _, v in items),
            "control": dict(_last_control),
            "tasks": [{"task_id": key, "group": value["group"]["name"], "kind": value["kind"]} for key, value in items]}


def _dispatch(app, session, group, scoped, settings, devices):
    from .tasks import claim_task
    from media_lineage.resources import gpu_identity, process_identity, release
    for candidate in session.scalars(select(Task).where(Task.status == "queued").order_by(Task.created_at, Task.id).limit(32)):
        kind = candidate.kind
        if kind == "classify" and any((scoped["state_directory"] / "feedback").glob("*.json")):
            continue
        mode = resource_mode(kind, candidate.input_snapshot)
        if kind == "extract" and sum(v["kind"] == kind for v in _running.values()) >= settings["extract_concurrency"]:
            continue
        if kind == "classify" and sum(v["kind"] == kind for v in _running.values()) >= 2:
            continue
        if mode == "extract_shared" and kind != "extract" and sum(v.get("requested_resource_mode", v["resource_mode"]) == mode and v["kind"] != "extract" for v in _running.values()) >= 2:
            continue
        if kind == "predict" and sum(v["kind"] == kind for v in _running.values()) >= 2:
            continue
        if kind == "train" and any(v["kind"] == kind for v in _running.values()):
            continue
        lease, profile, peak = None, None, None
        if mode:
            if scoped["device"] not in devices:
                devices[scoped["device"]] = gpu_identity(scoped["device"])
            device = devices[scoped["device"]]
            workload = "extract" if kind == "extract" else "predict"
            inputs = candidate.input_snapshot
            from .models import FeatureBundle, Variant
            from .features.contract import canonical_json, DIMENSIONS
            metadata = (session.scalar(select(Variant.media_metadata).where(Variant.id == candidate.variant_id)) or {}) if candidate.variant_id else {}
            windows = (session.scalar(select(FeatureBundle.windows).where(FeatureBundle.id == inputs["bundle_id"])) or 0) if inputs.get("bundle_id") else 0
            profile = hashlib.sha256(canonical_json({"device": device, "signature": inputs.get("feature_signature"),
                "batch_size": scoped.get("batch_size", 1), "workload": workload, "decode": "nvdec-224-v1",
                "codec": metadata.get("video_codec", "unknown"), "width": metadata.get("width"),
                "height": metadata.get("height"), "window_bucket": math.ceil(windows / 128)}).encode()).hexdigest()
            peak = scoped.get("gpu_extract_peak_mib", 1024) if workload == "extract" else scoped.get("gpu_predict_peak_mib", 256)
            if workload == "predict":
                peak += math.ceil(windows * sum(DIMENSIONS.values()) * 16 / 1024**2)
            # CPU preparation starts without a GPU reservation. The worker asks
            # for its bounded GPU phase once its inputs/models are ready.
        claim_started = time.monotonic()
        task = claim_task(session, candidate.id)
        if task is None:
            if lease:
                release(session, lease)
            continue
        task.execution_owner = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
        session.commit()
        identifier, token = task.id, task.claim_token
        dispatched = threading.Event()
        future = _submit(_pool, _run, app, group, identifier, token, lease, dispatched)
        if future is None:
            _return_unstarted_task(session, identifier, token, lease)
            return False
        _running[identifier] = {"future": future, "group": group, "kind": kind,
            "model_type": task.input_snapshot.get("model_type"), "lease": lease, "resource_mode": None,
            "gpu_profile": profile, "gpu_peak": peak, "requested_resource_mode": mode}
        future.add_done_callback(lambda completed: wake_control(app))
        dispatched.set()
        log_performance("task_started", task_id=identifier, variant_id=task.variant_id, kind=kind,
            mode=mode, lease_id=lease, queue_wait_seconds=round((utc_now() - task.created_at).total_seconds(), 4),
            claim_seconds=round(time.monotonic() - claim_started, 4))
        logger.info("任务开始 | group=%s | task_id=%s | kind=%s | concurrent=%s | configured=%s | claim_seconds=%.3f", group["name"], identifier, kind, len(_running), settings["extract_concurrency"], time.monotonic() - claim_started)
        return True
    return False


def _tick(app, settings):
    global _effective_limit, _rotation, _recovery_successes, _last_oom, _last_control
    from app import db
    from .runtime import enqueue_automatic, reconciliation_pending
    from .tracking import reconcile
    from .configuration import record_configuration
    from .feedback import FeedbackJournal
    from .tasks import claim_task
    from .worker_client import cancel_worker
    from media_lineage.resources import gpu_lock, gpu_identity, request_lease, heartbeat, release
    from media_lineage.workflows import publish_binding
    if not settings.get("enabled") or _stopping.is_set():
        return
    started = time.monotonic()
    with _lock, gpu_lock(settings["state_directory"], name="control.lock") as acquired:
        if not acquired or _stopping.is_set():
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
                if item["future"].cancelled():
                    with group_scope(db.session, item["group"], create=False):
                        task = db.session.get(Task, identifier)
                        if task:
                            _return_unstarted_task(db.session, identifier, task.claim_token, item["lease"])
                    _running.pop(identifier)
                    continue
                item["future"].result()
                with group_scope(db.session, item["group"], create=False) as scoped:
                    task = db.session.get(Task, identifier)
                    if task and task.error_code == "gpu_out_of_memory":
                        active_count = item.get("gpu_concurrency_at_oom") or sum(value["kind"] == "extract" and bool(value["lease"]) for value in _running.values()) or 1
                        previous_limit = min(_effective_limit or settings["extract_concurrency"], active_count)
                        _effective_limit = max(1, previous_limit - 1)
                        _last_oom, _recovery_successes = time.monotonic(), 0
                        if task.attempts < 3 and previous_limit > 1:
                            task.status, task.claim_token, task.error_code = "queued", None, None
                            db.session.commit()
                        logger.warning("GPU OOM 降低实际准入 | configured=%s | effective=%s", settings["extract_concurrency"], _effective_limit)
                    elif task and task.kind == "extract" and task.status == "succeeded" and _effective_limit:
                        _recovery_successes += 1
                        if time.monotonic() - _last_oom >= 60 and _recovery_successes >= 2 * _effective_limit:
                            _effective_limit = min(settings["extract_concurrency"], _effective_limit + 1)
                            _recovery_successes = 0
                            logger.info("GPU 成功运行后恢复准入 | effective=%s", _effective_limit)
                _running.pop(identifier)
            elif item["lease"]:
                from .worker_client import worker_processes
                if not heartbeat(db.session, item["lease"], processes=worker_processes(identifier)):
                    item.update(lease=None, resource_mode=None)
        names = list(configured)
        if not names:
            return
        _rotation = (_rotation + 1) % len(names)
        names = names[_rotation:] + names[:_rotation]
        ready, devices = [], {}
        discovery_counts, discovery_seconds = {}, 0
        limits = {"extract": settings["extract_concurrency"] * 2, "predict": 4, "classify": 4}
        remaining = {kind: max(0, limit - db.session.scalar(select(func.count()).select_from(table)
            .where(table.c.status == "queued", table.c.kind == kind))) for kind, limit in limits.items()}
        for index, name in enumerate(names):
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
                    pending = name not in _controls and revision.active_slot == "active" and reconciliation_pending(db.session, scoped, revision.id)
                    if name not in _controls and (due or pending or any(changed for changed, overflow in hints) or revision.active_slot != "active" or notice_state["generation"] > notice_state["cleared"]):
                        future = _submit(_control_pool, _scan_group, app, group, notice_state)
                        if future is None:
                            return
                        _controls[name] = future
                        future.add_done_callback(lambda completed: wake_control(app))
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
                            from .runtime import DEFERRED_TASK_REASONS
                            if isinstance(error, ValueError) and str(error) in DEFERRED_TASK_REASONS:
                                logger.info("搬运恢复暂缓，等待反馈重放 | operation_id=%s | reason=%s", operation.id, error)
                                continue
                            operation.status = "conflict"
                            db.session.commit()
                            log_failure(logger, "搬运恢复保留冲突 | operation_id=%s", error, operation.id)
                    before = {kind: db.session.scalar(select(func.count()).select_from(Task).where(Task.kind == kind, Task.status == "queued")) for kind in limits}
                    scoped["discovery_budget"] = {kind: before[kind] + math.ceil(value / (len(names) - index)) for kind, value in remaining.items()}
                    scoped["discovery_counts"] = discovery_counts
                    discovery_started = time.monotonic()
                    enqueue_automatic(db.session, scoped)
                    discovery_seconds += time.monotonic() - discovery_started
                    for kind in remaining:
                        after = db.session.scalar(select(func.count()).select_from(Task).where(Task.kind == kind, Task.status == "queued"))
                        remaining[kind] = max(0, remaining[kind] - max(0, after - before[kind]))
                    ready.append(group)
            except Exception as error:
                db.session.rollback()
                log_failure(logger, "分组控制失败 | group=%s", error, name)
        dispatch_started = time.monotonic()
        for _ in range(settings["extract_concurrency"] + 5):
            progress = False
            for group in ready:
                try:
                    with group_scope(db.session, group, create=False) as scoped:
                        progress = _dispatch(app, db.session, group, scoped, settings, devices) or progress
                except Exception as error:
                    db.session.rollback()
                    log_failure(logger, "分组准入失败 | group=%s", error, group["name"])
            if not progress or _stopping.is_set():
                break
        queued_counts = dict(db.session.execute(select(table.c.kind, func.count()).where(table.c.status == "queued").group_by(table.c.kind)).all())
        oldest = db.session.scalar(select(func.min(table.c.created_at)).where(table.c.status == "queued"))
        _last_control = {"queued": queued_counts, "oldest_wait_seconds": round((utc_now() - oldest).total_seconds(), 1) if oldest else 0,
            "discovery_counts": discovery_counts, "discovery_seconds": round(discovery_seconds, 3),
            "dispatch_seconds": round(time.monotonic() - dispatch_started, 3),
            "elapsed_seconds": round(time.monotonic() - started, 3)}
        log_performance("control_round", **_last_control,
            extract_running=snapshot()["extract_running"], other_running=snapshot()["other_running"],
            configured_concurrency=settings["extract_concurrency"],
            effective_concurrency=_effective_limit or settings["extract_concurrency"], scan_running=len(_controls))
        db.session.commit()
        from .observability import status_log
        status_log(logger, "supervisor", "控制轮次 | extract_running=%s | other_running=%s | queued=%s | oldest_wait_seconds=%s | elapsed=%.3fs | discovery=%s | discovery_seconds=%.3f | dispatch_seconds=%.3f",
            snapshot()["extract_running"], snapshot()["other_running"], queued_counts, _last_control["oldest_wait_seconds"], _last_control["elapsed_seconds"], discovery_counts, discovery_seconds, _last_control["dispatch_seconds"], interval=15,
            state=(snapshot()["extract_running"], snapshot()["other_running"], tuple(sorted(queued_counts.items()))))
