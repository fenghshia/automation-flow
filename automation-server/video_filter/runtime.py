"""Bounded control rounds: recovery, reconciliation, then one claimed task."""

import hashlib
import logging
from logging_config import log_performance
import time
from pathlib import Path
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from .observability import log_failure as log_exception, redact_paths, progress_phase, status_log

from .configuration import record_configuration
from .feature_store import FeatureStore
from .features.contract import canonical_json
from .features.manifest import load_model_manifest
from .feedback import FeedbackJournal
from .identity import SourceMissing, source_snapshot as snapshot, source_hash as hash_stable
from .models import Asset, ConfigRevision, FeatureBundle, Location, ModelRun, Prediction, ScanRun, Task, TransferOperation, Variant
from .models.records import utc_now
from .tasks import claim_task, enqueue_task, transaction_error_code
from .model_registry import MODEL_TYPES, active_models, model_type as validate_model_type
from .progress import extra, track_task


logger = logging.getLogger(__name__)
_candidate_offsets = {}


def _pending_variants(session, config_id, signature, models=None):
    rows = session.execute(select(Task, Asset.label_revision).join(Asset, Asset.id == Task.asset_id)
        .where(Task.status.in_(("queued", "running")), Task.config_revision_id == config_id)).all()
    return {task.variant_id for task, revision in rows
        if task.input_snapshot.get("feature_signature") == signature
        and task.input_snapshot.get("label_revision") == revision
        and (models is None and task.kind == "extract" or models is not None and task.kind in ("predict", "classify")
             and task.input_snapshot.get("model_ids") == {kind: run.id for kind, run in models.items()})}


def _candidates(session, query, settings, kind):
    if not settings.get("grouped"):
        yield from session.execute(query.limit(20)).scalars()
        return
    key = (settings.get("dataset_group_id"), settings.get("reset_epoch"), kind)
    offset = _candidate_offsets.get(key, 0)
    rows = session.execute(query.limit(100).offset(offset)).scalars().all()
    _candidate_offsets[key] = offset + len(rows) if len(rows) == 100 else 0
    if len(_candidate_offsets) > 512:
        _candidate_offsets.pop(next(iter(_candidate_offsets)))
    yield from rows


def _budget(session, settings, kind, default):
    if not settings.get("grouped"):
        return 1
    maximum = settings.get("discovery_budget", {}).get(kind, default)
    return max(0, maximum - session.scalar(select(func.count()).select_from(Task)
        .where(Task.kind == kind, Task.status == "queued")))


EXPECTED_TASK_CHANGES = {
    "configuration_changed", "asset_feedback_changed", "feature_signature_changed",
    "training_configuration_changed", "training_dataset_changed", "training_labels_changed",
    "source_version_changed", "selected_classifier_changed", "transfer_disabled_or_configuration_changed",
    "active_compatible_model_required", "current_managed_location_required", "stable_registered_source_required",
}

DEFERRED_TASK_REASONS = {"unresolved_feedback_journal"}


def task_failure(error, attempts=None):
    database_code = transaction_error_code(error)
    code = database_code or ("source_missing" if isinstance(error, SourceMissing) else (
        str(error) if isinstance(error, ValueError) and str(error).replace("_", "").isalnum() and len(str(error)) <= 64 else type(error).__name__))
    expected = isinstance(error, SourceMissing) or (isinstance(error, ValueError) and code in EXPECTED_TASK_CHANGES)
    if isinstance(error, ValueError) and code in DEFERRED_TASK_REASONS:
        return "queued", code, True
    terminal = "cancelled" if expected or (isinstance(error, ValueError) and ("changed" in code or "stale_epoch" in code)) else "failed"
    if database_code and attempts is not None and attempts < 3:
        terminal = "queued"
    return terminal, code, expected


def task_result(terminal, code):
    changes = {"status": terminal, "error_code": code, "finished_at": utc_now()}
    if terminal == "queued":
        changes.update(claim_token=None, claimed_at=None, heartbeat_at=None, execution_owner=None, finished_at=None)
        if code in DEFERRED_TASK_REASONS:
            changes["attempts"] = Task.attempts - 1
    return changes


def _skip_changed_candidate(session, error, variant_id):
    _, code, expected = task_failure(error)
    if expected:
        session.rollback()
        logger.info("候选输入已变化，等待扫描或新模型后重新发现 | variant_id=%s | reason=%s", variant_id, code)
        return True
    return False


def reconciliation_pending(session, settings, config_revision_id):
    from .tracking import lineage_pending

    latest = session.execute(select(ScanRun).where(ScanRun.complete == True,
        ScanRun.config_revision_id == config_revision_id).order_by(ScanRun.created_at.desc()).limit(1)).scalar_one_or_none()
    return latest is None or lineage_pending(session, settings, latest.lineage_watermark)


def _extraction_baseline_ready(session, settings, config_id):
    if not settings.get("grouped"):
        return False
    from .tracking import lineage_pending
    completed = session.scalar(select(ScanRun.id).where(ScanRun.config_revision_id == config_id, ScanRun.complete == True).limit(1))
    latest = session.scalar(select(ScanRun).where(ScanRun.config_revision_id == config_id).order_by(ScanRun.created_at.desc(), ScanRun.id.desc()).limit(1))
    return completed is None and latest is not None and latest.role_results.get("_baseline_ready") is True and not lineage_pending(session, settings, settings.get("lineage_floor", 0))


def _defer_reconciliation(session, error):
    # Events can arrive between the round's initial check and task submission.
    if isinstance(error, ValueError) and str(error) == "lineage_reconciliation_pending":
        session.rollback()
        logger.info("自动任务等待：文件搬运或压缩记录尚待扫描对账 | reason=lineage_reconciliation_pending")
        return True
    return False


def enqueue_automatic(session, settings):
    """Discover a bounded batch; failed identical inputs wait for explicit retry."""
    if not settings.get("model_manifest") or not settings.get("ffmpeg_directory"):
        status_log(logger, (settings.get("name"), "missing_tools"), "自动任务等待：模型清单或 FFmpeg 尚未配置")
        return
    signature, _ = load_model_manifest(settings["model_manifest"])
    config = record_configuration(session, settings)
    if config.active_slot != "active":
        status_log(logger, (settings.get("name"), "baseline"), "自动任务等待：完整初始扫描尚未完成 | config_id=%s", config.id)
        return
    if reconciliation_pending(session, settings, config.id):
        if _extraction_baseline_ready(session, settings, config.id):
            _enqueue_extraction(session, settings, signature)
            return
        status_log(logger, (settings.get("name"), "reconciliation"), "自动任务等待：文件搬运或压缩记录尚待扫描对账 | reason=lineage_reconciliation_pending")
        return
    # Model training is manual. Discovery still extracts summaries and predicts.
    models = active_models(session, signature.digest)
    selected = settings.get("classifier", "logistic_regression")
    if not models:
        status_log(logger, (settings.get("name"), "classifier"), "等待手动训练个人分类器 | selected_classifier=%s", selected)
        _enqueue_extraction(session, settings, signature)
        return
    if settings.get("transfer_enabled") and selected not in models:
        logger.info("分类搬运等待：所选模型尚无兼容活动版本 | selected_classifier=%s | available_classifiers=%s",
                    selected, ",".join(models))
    from .prediction import prediction_complete
    missing = [~select(Prediction.id).where(Prediction.variant_id == Variant.id,
        Prediction.bundle_id == FeatureBundle.id, Prediction.label_revision == Asset.label_revision,
        Prediction.model_id == run.id, Prediction.prediction_batch_id.is_not(None)).correlate(Variant, FeatureBundle, Asset).exists()
        for run in models.values()]
    needs_transfer = and_(Location.role == "unclassified", bool(settings.get("transfer_enabled") and selected in models))
    candidates = (select(Variant.id, func.min(Location.created_at)).join(Location, Location.variant_id == Variant.id)
        .join(Asset, Asset.id == Variant.asset_id).join(FeatureBundle, FeatureBundle.variant_id == Variant.id)
        .where(Location.role.in_(("unclassified", "predicted_like", "predicted_dislike")), Location.status == "present",
            Asset.label.is_(None), FeatureBundle.status == "ready", FeatureBundle.feature_signature == signature.digest,
            or_(needs_transfer, *missing)).group_by(Variant.id).order_by(func.min(Location.created_at), Variant.id))
    if settings.get("grouped"):
        candidates = candidates.where(Variant.id.not_in(_pending_variants(session, config.id, signature.digest, models)))
    remaining = {kind: _budget(session, settings, kind, 4) for kind in ("predict", "classify")}
    for variant_id in _candidates(session, candidates, settings, "prediction"):
        try:
            bundle = session.execute(select(FeatureBundle).filter_by(variant_id=variant_id,
                feature_signature=signature.digest, status="ready")).scalar_one_or_none()
            if bundle is None:
                continue
            asset = session.get(Asset, session.get(Variant, variant_id).asset_id)
            if not prediction_complete(session, variant_id, bundle.id, asset.label_revision, models):
                if not remaining["predict"]:
                    continue
                task = enqueue(session, settings, "predict", variant_id)
            elif settings.get("transfer_enabled") and selected in models and session.execute(select(Location.id).where(
                    Location.variant_id == variant_id, Location.role == "unclassified", Location.status == "present").limit(1)).first():
                if not remaining["classify"]:
                    continue
                task = enqueue(session, settings, "classify", variant_id)
            else:
                continue
            if task.status == "queued":
                if not settings.get("grouped"):
                    return
                if getattr(task, "enqueue_disposition", "created") != "reused":
                    remaining[task.kind] -= 1
                if not any(remaining.values()):
                    break
        except (ValueError, OSError) as error:
            if _defer_reconciliation(session, error):
                return
            if _skip_changed_candidate(session, error, variant_id):
                continue
            log_exception(logger, "分类任务无法入队 | variant_id=%s", error, variant_id)
            session.rollback()
    _enqueue_extraction(session, settings, signature)


def _enqueue_extraction(session, settings, signature):
    # Finish predictions for ready summaries before extending the extraction
    # backlog, so a folder of long videos cannot indefinitely delay evaluation.
    # PostgreSQL requires DISTINCT sort expressions in the selected columns.
    remaining = _budget(session, settings, "extract", 2 * settings.get("extract_concurrency", 6))
    if not remaining:
        return
    query = (select(Variant.id, Variant.created_at).join(Location, Location.variant_id == Variant.id)
        .outerjoin(FeatureBundle, (FeatureBundle.variant_id == Variant.id) &
                   (FeatureBundle.feature_signature == signature.digest) & (FeatureBundle.status == "ready"))
        .where(Location.status == "present", Location.role != "liked_source", FeatureBundle.id.is_(None))
        .order_by(Variant.created_at, Variant.id).distinct())
    if settings.get("grouped"):
        config = record_configuration(session, settings)
        query = query.where(Variant.id.not_in(_pending_variants(session, config.id, signature.digest)))
    for variant_id in _candidates(session, query, settings, "extract"):
        try:
            task = enqueue(session, settings, "extract", variant_id)
            if task.status == "queued":
                if not settings.get("grouped"):
                    return
                if getattr(task, "enqueue_disposition", "created") != "reused":
                    remaining -= 1
                if not remaining:
                    break
        except (ValueError, OSError) as error:
            if _defer_reconciliation(session, error):
                return
            if _skip_changed_candidate(session, error, variant_id):
                continue
            log_exception(logger, "提取任务无法入队 | variant_id=%s", error, variant_id)
            session.rollback()


def enqueue(session, settings, kind, variant_id=None, model_type=None, *, acceptance=None):
    config = record_configuration(session, settings)
    if kind == "scan":
        # A bounded time bucket lets subsequent scans run after terminal tasks.
        interval = settings.get("scan_interval_seconds", 60)
        task = enqueue_task(session, kind, config.id, {"bucket": int(utc_now().timestamp()) // interval})
        session.commit()
        logger.info("扫描任务已登记 | task_id=%s | status=%s | config_id=%s", task.id, task.status, config.id)
        return task
    if config.active_slot != "active":
        raise ValueError("complete_initial_scan_required")
    if reconciliation_pending(session, settings, config.id) and not (kind == "extract" and _extraction_baseline_ready(session, settings, config.id)):
        raise ValueError("lineage_reconciliation_pending")
    signature, _ = load_model_manifest(settings["model_manifest"])
    if kind == "train":
        from .learning import training_snapshot
        from .training_config import effective, digest
        from uuid import uuid4

        data = training_snapshot(session, signature.digest)
        if any(sum(item["label"] == label for item in data) < effective(settings, "logistic_regression")["minimum_per_class"] for label in (0, 1)):
            raise ValueError("insufficient_confirmed_samples_minimum_10_per_class")
        classifier = validate_model_type(model_type or settings.get("classifier", "logistic_regression"))
        if settings.get("grouped"):
            from .models import DatasetGroup
            session.execute(select(DatasetGroup.id).where(DatasetGroup.id == settings["dataset_group_id"]).with_for_update()).first()
        pending = session.execute(select(Task).where(Task.kind == "train", Task.status.in_(("queued", "running")))).scalars()
        same = next((row for row in pending if row.input_snapshot.get("model_type") == classifier), None)
        if same:
            if acceptance is not None and same.input_snapshot.get("acceptance") != acceptance:
                raise ValueError("training_pending_with_different_acceptance")
            return same
        parameters = effective(settings, classifier, acceptance=acceptance)
        requirements = {name + "_precision": parameters["activation_" + name + "_precision"] for name in ("like", "dislike")}
        task = enqueue_task(session, kind, config.id, {"feature_signature": signature.digest, "model_type": classifier,
            "training_config_digest": digest(settings, classifier, acceptance=requirements), "hyperparameters": parameters,
            "acceptance": requirements, "request_id": str(uuid4()),
            "dataset_digest": hashlib.sha256(canonical_json(data).encode()).hexdigest()})
    else:
        variant = session.get(Variant, variant_id)
        if variant is None:
            raise ValueError("registered_variant_required")
        asset = session.get(Asset, variant.asset_id)
        query = select(Location).where(Location.variant_id == variant.id, Location.status == "present")
        if kind == "extract":
            query = query.where(Location.role != "liked_source")
        if kind == "classify":
            query = query.where(Location.role == "unclassified")
        location = session.execute(query.order_by(Location.created_at.desc()).limit(1)).scalar_one_or_none()
        if location is None:
            raise ValueError("current_managed_location_required")
        stat = snapshot(location.path)
        if settings.get("grouped"):
            from .group_config import require_scope
            require_scope(settings, location.path, sample=kind == "extract")
        if (stat["size_bytes"], stat["modified_ns"], stat["file_identity"]) != (location.size_bytes, location.modified_ns, location.file_identity):
            raise ValueError("stable_registered_source_required")
        values = {"path": location.path, "source_snapshot": stat, "label_revision": asset.label_revision,
                  "feature_signature": signature.digest}
        if settings.get("grouped"):
            values.update(dataset_group_id=settings["dataset_group_id"], reset_epoch=settings["reset_epoch"], group_name=settings["name"])
        if kind in ("classify", "predict"):
            models = active_models(session, signature.digest)
            selected = settings.get("classifier", "logistic_regression")
            if kind == "classify" and (not settings.get("transfer_enabled") or selected not in models):
                raise ValueError("selected_classifier_unavailable_or_transfer_disabled")
            bundle = session.execute(select(FeatureBundle).filter_by(variant_id=variant.id, feature_signature=signature.digest, status="ready")).scalar_one_or_none()
            if bundle is None or not models:
                raise ValueError("committed_summary_and_validated_model_required")
            FeatureStore().require_ready(session, bundle.id)
            values.update(bundle_id=bundle.id, model_ids={name: run.id for name, run in models.items()},
                selected_classifier=selected)
            if selected in models:
                values["model_id"] = models[selected].id
            if kind == "classify":
                from .prediction import compatible_predictions
                predictions = compatible_predictions(session, variant.id, bundle.id, asset.label_revision, models, stat)
                if predictions:
                    values["prediction_ids"] = {name: row.id for name, row in predictions.items()}
                    values["prediction_batch_id"] = next(iter(predictions.values())).prediction_batch_id
        task = enqueue_task(session, kind, config.id, values, asset.id, variant.id)
    session.commit()
    if settings.get("grouped") and "discovery_counts" in settings:
        disposition = getattr(task, "enqueue_disposition", "created")
        counts = settings["discovery_counts"]
        counts[disposition] = counts.get(disposition, 0) + 1
    if getattr(task, "enqueue_disposition", "created") != "reused":
        logger.info("任务已登记 | task_id=%s | kind=%s | status=%s | disposition=%s | variant_id=%s", task.id, kind, task.status, task.enqueue_disposition, variant_id or "-")
    return task


def execute(session, settings, task):
    if settings.get("grouped"):
        from env import EnvConfig
        current = next((g for g in EnvConfig.video_filter_settings(ignore_scope=True).get("groups", []) if g["name"] == settings["name"] and g["enabled"]), None)
        if current is None or current["directories"] != settings["directories"] or (task.dataset_group_id, task.reset_epoch) != (settings["dataset_group_id"], settings["reset_epoch"]):
            raise ValueError("configuration_changed")
    config = record_configuration(session, settings)
    if config.id != task.config_revision_id:
        raise ValueError("configuration_changed")
    if task.kind == "scan":
        from .tracking import reconcile

        return reconcile(session, settings)
    signature, _ = load_model_manifest(settings["model_manifest"])
    if task.input_snapshot["feature_signature"] != signature.digest:
        raise ValueError("feature_signature_changed")
    if task.kind == "extract":
        from .worker_client import run_extraction

        variant = session.get(Variant, task.variant_id)
        existing = session.execute(select(FeatureBundle).filter_by(variant_id=variant.id, feature_signature=signature.digest, status="ready")).scalar_one_or_none()
        if existing:
            logger.info("复用已提交摘要 | task_id=%s | bundle_id=%s | variant_id=%s", task.id, existing.id, variant.id)
            return FeatureStore().require_ready(session, existing.id)
        request = {**task.input_snapshot, "asset_id": variant.asset_id, "variant_id": variant.id, "task_id": task.id,
            "directory_revision_id": task.config_revision_id, "claim_token": task.claim_token,
            "batch_size": settings.get("batch_size", 1),
            **{key: str(settings[key]) for key in ("state_directory", "ffmpeg_directory", "model_manifest", "device")}}
        request.update(worker_cpu_threads=settings.get("worker_cpu_threads", 1), resource_granted=settings.get("resource_granted", False), resource_lease_id=settings.get("resource_lease_id"))
        request["audio_cache_mib"] = settings.get("audio_cache_mib", 32)
        request["gpu_control"] = settings.get("gpu_control")
        request.update({key: settings[key] for key in ("persistent_workers", "worker_model_cache_mib", "worker_idle_seconds", "worker_max_tasks") if key in settings})
        request["persistent_worker_capacity"] = settings.get("extract_concurrency", 6)
        request["batch_sizes"] = {name: settings.get(name + "_batch_size", settings.get("batch_size", 1))
                                  for name in ("dino", "videomae", "beats")}
        expected_digest = variant.sha256
        # Freeze all ORM inputs before ending the read transaction. Accessing
        # expired task/variant attributes would reserve a connection again while
        # the worker waits for a GPU lease in its independent parent session.
        session.commit()
        verified_at = time.monotonic()
        if hash_stable(request["path"], request["source_snapshot"])[0] != expected_digest:
            raise ValueError("source_version_changed")
        log_performance("extraction_phase", task_id=request["task_id"], phase="parent_hash_before_worker",
            elapsed_seconds=round(time.monotonic() - verified_at, 4))
        prepared, metadata = run_extraction(request, settings["task_timeout_seconds"])
        logger.info("特征摘要已收到，重新校验源并入库 | task_id=%s | variant_id=%s | payload_bytes=%s", request["task_id"], request["variant_id"], len(prepared.arrays_blob),
            extra=extra(request["task_id"], "摘要核验与入库"))
        if not (settings.get("grouped") and metadata.get("source_verified") and not Path(request["path"]).exists()) and hash_stable(request["path"], request["source_snapshot"])[0] != expected_digest:
            raise ValueError("source_version_changed")
        variant = session.get(Variant, request["variant_id"], populate_existing=True)
        if variant is None or variant.sha256 != expected_digest or variant.asset_id != request["asset_id"]:
            raise ValueError("source_version_changed")
        variant.media_metadata = {**metadata["media_metadata"], "extraction_measurements": metadata["measurements"]}
        stored_at = time.monotonic()
        stored = FeatureStore().save(session, prepared)
        log_performance("summary_committed", task_id=task.id, payload_bytes=len(prepared.arrays_blob),
            elapsed_seconds=round(time.monotonic() - stored_at, 4))
        logger.info("特征摘要已提交并验证可读 | task_id=%s | variant_id=%s | bundle_id=%s | windows=%s", task.id, variant.id, stored.bundle_id, stored.manifest["window_count"])
        return stored
    if task.kind == "train":
        from .learning import train, training_snapshot
        from .training_config import digest
        acceptance = task.input_snapshot.get("acceptance")
        if task.input_snapshot.get("training_config_digest") != digest(settings, task.input_snapshot.get("model_type", "logistic_regression"), acceptance=acceptance):
            raise ValueError("training_configuration_changed")

        with progress_phase(logger, "核对训练输入快照", task_id=task.id):
            data = training_snapshot(session, signature.digest)
        if hashlib.sha256(canonical_json(data).encode()).hexdigest() != task.input_snapshot["dataset_digest"]:
            raise ValueError("training_dataset_changed")
        return train(session, signature.digest, task_id=task.id,
            model_type=task.input_snapshot.get("model_type", "logistic_regression"), settings=settings, acceptance=acceptance)
    if task.kind == "predict":
        from .prediction import predict_both
        return predict_both(session, task, task.input_snapshot.get("selected_classifier", "logistic_regression"), settings=settings)
    if task.kind == "classify":
        from .transfer import classify

        return classify(session, settings, task)
    raise ValueError("unknown_task_kind")


def process_round(session, settings):
    from media_lineage.resources import gpu_lock
    from .tracking import reconcile
    from .transfer import advance_transfer

    if not settings.get("enabled"):
        return None
    redact_paths([*settings["directories"].values(), settings["state_directory"], settings.get("model_manifest"), settings.get("ffmpeg_directory")])
    started = time.monotonic()
    with gpu_lock(settings["state_directory"], name="control.lock") as acquired:
        if not acquired:
            logger.info("本轮跳过：其他进程持有控制锁")
            return None
        # A vanished worker is failed only after its configured hard timeout.
        cutoff = utc_now() - timedelta(seconds=settings["task_timeout_seconds"] + 180)
        expired = session.execute(update(Task).where(Task.status == "running", Task.claimed_at < cutoff).values(
            status="failed", error_code="interrupted_worker", finished_at=utc_now())).rowcount
        session.commit()
        if expired:
            logger.warning("检测到中断任务，已标记失败 | tasks=%s | reason=interrupted_worker", expired)
        replay = FeedbackJournal(settings["state_directory"]).replay(session)
        if any(replay.values()):
            logger.info("反馈日志重放完成 | applied=%s | superseded=%s | conflicts=%s", replay["applied"], replay["superseded"], replay["conflicts"])
        operations = session.execute(select(TransferOperation).where(
            TransferOperation.status.in_(("planned", "destination_verified", "published"))).order_by(TransferOperation.created_at).limit(1)).scalars().all()
        for operation in operations:
            logger.info("开始恢复分类搬运 | operation_id=%s | stage=%s", operation.id, operation.status)
            try:
                advance_transfer(session, settings, session.get(Task, operation.task_id), operation)
            except Exception as error:
                session.rollback()
                if isinstance(error, ValueError) and str(error) in DEFERRED_TASK_REASONS:
                    logger.info("分类搬运恢复暂缓，等待反馈重放 | operation_id=%s | reason=%s", operation.id, error)
                    continue
                log_exception(logger, "分类搬运恢复失败，保留冲突供核对 | operation_id=%s", error, operation.id)
                operation.status = "conflict"
                session.commit()
        reconcile(session, settings)
        if session.execute(select(Task.id).where(Task.status == "queued").limit(1)).first() is None:
            enqueue_automatic(session, settings)
        task = claim_task(session)
        if task is None:
            logger.info("本轮完成，无待执行任务 | elapsed=%.1fs", time.monotonic() - started)
            return None
        token = task.claim_token
        task_started = time.monotonic()
        logger.info("开始执行任务 | task_id=%s | kind=%s | attempt=%s | variant_id=%s", task.id, task.kind, task.attempts, task.variant_id or "-")
        try:
            with track_task(settings["state_directory"], task.id):
                execute(session, settings, task)
            terminal, code = "succeeded", None
        except Exception as error:
            session.rollback()
            terminal, code, expected = task_failure(error, task.attempts)
            if terminal == "queued" and code in DEFERRED_TASK_REASONS:
                logger.info("任务暂缓，等待反馈重放后继续 | task_id=%s | reason=%s", task.id, code)
            elif expected:
                logger.info("任务输入已过期，已取消，后续按最新数据发现任务 | task_id=%s | kind=%s | reason=%s", task.id, task.kind, code)
            else:
                log_exception(logger, "任务执行失败 | task_id=%s | kind=%s | error_code=%s", error, task.id, task.kind, code)
        session.execute(update(Task).where(Task.id == task.id, Task.claim_token == token, Task.status == "running").values(
            **task_result(terminal, code)))
        session.commit()
        if terminal == "queued" and code not in DEFERRED_TASK_REASONS:
            logger.warning("数据库事务冲突，任务已回滚并重新入队 | task_id=%s | attempt=%s/3 | reason=%s", task.id, task.attempts, code)
        logger.info("任务结束 | task_id=%s | kind=%s | status=%s | error_code=%s | elapsed=%.1fs",
            task.id, task.kind, terminal, code or "-", time.monotonic() - task_started)
        return task.id


def register_schedules(scheduler, app):
    from env import EnvConfig

    def process():
        from app import db

        with app.app_context():
            try:
                settings = EnvConfig.video_filter_settings()
                if settings.get("grouped"):
                    from .supervisor import tick
                    tick(app, settings)
                else:
                    process_round(db.session, settings)
            except Exception as error:
                log_exception(logger, "控制轮次失败", error)
                db.session.rollback()
    if scheduler.get_job("video_filter_process_one") is None:
        scheduler.add_job(id="video_filter_process_one", func=process, trigger="interval", seconds=2,
            max_instances=1, coalesce=True, misfire_grace_time=120)
        logger.info("筛选调度已注册 | supervisor_interval_seconds=2 | max_instances=1")
