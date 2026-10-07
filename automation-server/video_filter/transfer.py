"""Non-overwriting publication with a retained hard-link witness for recovery."""

import os
import logging
import math
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from media_lineage.service import begin_operation, record_published, record_source_removed, verify_operation_source
from .configuration import record_configuration
from .feature_store import FeatureStore
from .identity import hash_stable, path_key, snapshot, source_hash
from .learning import lock_assets, require_model
from .prediction import predict_both
from .models import Asset, ConfigRevision, FeatureBundle, Location, ModelRun, Prediction, TransferOperation, Variant


logger = logging.getLogger(__name__)


def check_gate(session, settings, task, require_prediction=True):
    config = record_configuration(session, settings)
    if not settings.get("transfer_enabled") or config.id != task.config_revision_id or config.active_slot != "active":
        raise ValueError("transfer_disabled_or_configuration_changed")
    if any((settings["state_directory"] / "feedback").glob("*.json")):
        raise ValueError("unresolved_feedback_journal")
    variant = session.get(Variant, task.variant_id)
    model = session.get(ModelRun, task.input_snapshot["model_id"], populate_existing=True)
    if model is None or model.model_type != settings.get("classifier", "logistic_regression"):
        raise ValueError("selected_classifier_changed")
    model_ids = task.input_snapshot.get("model_ids", {model.model_type: model.id})
    models = [session.get(ModelRun, run_id, populate_existing=True) for run_id in model_ids.values()]
    asset_ids = {variant.asset_id}
    for run in models:
        if run is not None:
            asset_ids.update(item["asset_id"] for item in run.dataset_snapshot)
    assets = lock_assets(session, asset_ids)
    asset = assets.get(variant.asset_id)
    if asset is None or asset.label_revision != task.input_snapshot["label_revision"]:
        raise ValueError("asset_feedback_changed")
    model = session.get(ModelRun, model.id, populate_existing=True)
    bundle = FeatureStore().require_ready(session, task.input_snapshot["bundle_id"])
    if bundle.manifest["variant_id"] != variant.id:
        raise ValueError("summary_variant_mismatch")
    require_model(session, model, bundle)
    # A persisted prediction is part of the transfer evidence. Recovery checks
    # must validate it, not start a GPU worker from the scheduler control tick.
    batch = task.input_snapshot.get("prediction_batch_id", task.id)
    prediction = session.execute(select(Prediction).filter_by(prediction_batch_id=batch, model_id=model.id)).scalar_one_or_none()
    if prediction is None:
        if require_prediction:
            raise ValueError("committed_prediction_required")
        return variant, asset, model, None
    if (prediction.variant_id != variant.id or prediction.bundle_id != bundle.bundle_id or
            prediction.label_revision != asset.label_revision or prediction.threshold != model.threshold or
            not math.isfinite(prediction.score) or not 0 <= prediction.score <= 1):
        raise ValueError("prediction_snapshot_mismatch")
    probability = prediction.score
    return variant, asset, model, probability


def classify(session, settings, task):
    operation = session.execute(select(TransferOperation).filter_by(task_id=task.id)).scalar_one_or_none()
    if operation is None:
        variant, asset, model, _ = check_gate(session, settings, task, require_prediction=False)
        expected_digest = variant.sha256
        source = Path(task.input_snapshot["path"])
        if source.parent.resolve() != settings["directories"]["unclassified"].resolve():
            raise ValueError("source_outside_unclassified")
        source_stat = dict(task.input_snapshot["source_snapshot"])
        session.commit()  # No asset/model locks during the full-file read.
        digest, stat = source_hash(source, source_stat)
        if digest != expected_digest:
            raise ValueError("source_version_changed")
        variant, asset, model, _ = check_gate(session, settings, task, require_prediction=False)
        if task.input_snapshot.get("prediction_ids"):
            predictions = {}
            for kind, identifier in task.input_snapshot["prediction_ids"].items():
                row = session.get(Prediction, identifier)
                run = session.get(ModelRun, task.input_snapshot["model_ids"].get(kind))
                if run is None or run.model_type != kind:
                    raise ValueError("invalid_prediction_model")
                require_model(session, run, FeatureStore().require_ready(session, task.input_snapshot["bundle_id"]))
                if row is None or row.prediction_batch_id != task.input_snapshot["prediction_batch_id"] or row.model_id != run.id or row.variant_id != variant.id or row.bundle_id != task.input_snapshot["bundle_id"] or row.label_revision != asset.label_revision or row.threshold != run.threshold or not math.isfinite(row.score) or not 0 <= row.score <= 1 or row.predicted_label != int(row.score >= run.threshold):
                    raise ValueError("prediction_snapshot_mismatch")
                predictions[kind] = row
            if set(predictions) != set(task.input_snapshot["model_ids"]):
                raise ValueError("prediction_snapshot_mismatch")
            session.commit()
            logger.info("分类复用双模型预测 | task_id=%s | prediction_batch_id=%s", task.id, task.input_snapshot["prediction_batch_id"])
        else:
            predictions = predict_both(session, task, settings.get("classifier", "logistic_regression"), settings=settings)
        if settings.get("release_gpu"):
            settings["release_gpu"]()
        prediction = predictions[model.model_type]
        probability = prediction.score
        label = int(probability >= model.threshold)
        role = "predicted_like" if label else "predicted_dislike"
        target = (settings["directories"][role] / source.name).resolve()
        if target.exists():
            raise ValueError("destination_name_conflict")
        lineage_id = begin_operation(session, producer="video_filter", source_path=source, destination_path=target,
            generation=task.id, source_role="unclassified", destination_role=role,
            scope_evidence={key: settings[key] for key in ("dataset_group_id", "reset_epoch") if key in settings})
        operation = TransferOperation(task_id=task.id, variant_id=variant.id, prediction_id=prediction.id,
            source_path=str(source), destination_path=str(target), source_sha256=digest,
            evidence={"lineage_id": lineage_id, "source_snapshot": stat, "destination_role": role})
        session.add(operation)
        session.commit()
        logger.info("分类结果及搬运计划已提交 | task_id=%s | operation_id=%s | model_id=%s | variant_id=%s | score=%.4f | threshold=%.3f | predicted_label=%s | target_role=%s",
            task.id, operation.id, model.id, variant.id, probability, model.threshold, label, role)
    advance_transfer(session, settings, task, operation)
    return operation


def advance_transfer(session, settings, task, operation):
    if operation.status == "source_cleaned":
        return
    if operation.status == "conflict":
        raise ValueError("transfer_conflict_requires_review")
    # Any feedback/config/model change stops an unpublished or pending cleanup operation.
    check_gate(session, settings, task)
    source, target = Path(operation.source_path), Path(operation.destination_path)
    if settings.get("grouped"):
        from .group_config import require_scope
        require_scope(settings, source)
        require_scope(settings, target)
    witness = target.parent / (".video-filter-" + operation.id + ".stage")
    lineage_id = operation.evidence["lineage_id"]
    if target.parent.resolve() != settings["directories"][operation.evidence["destination_role"]].resolve():
        raise ValueError("destination_configuration_changed")
    if operation.status == "planned":
        source_digest = operation.source_sha256
        session.commit()
        logger.info("分类搬运开始复制及校验 | task_id=%s | operation_id=%s", task.id, operation.id)
        verify_operation_source(session, lineage_id)
        if target.exists():
            raise ValueError("destination_name_conflict")
        if witness.exists():
            # An uncommitted staging file may be incomplete. Preserve it for review.
            operation.status = "conflict"
            session.commit()
            raise ValueError("unverified_staging_conflict")
        session.commit()  # Copy owns no row locks or DB transaction.
        copy_started = time.monotonic()
        with source.open("rb") as origin, witness.open("xb") as output:
            shutil.copyfileobj(origin, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        digest, stat = hash_stable(witness)
        logger.info("分类复制及暂存哈希结束 | task_id=%s | elapsed_seconds=%.3f", task.id, time.monotonic() - copy_started)
        verify_operation_source(session, lineage_id)
        if digest != source_digest:
            raise ValueError("staging_checksum_mismatch")
        # Persist the verified witness before rechecking transient feedback.
        # Publication below still requires the current gate; a pending journal
        # must not leave an owned, verified stage looking like an unknown file.
        operation.evidence = {**operation.evidence, "witness_snapshot": stat}
        operation.status = "destination_verified"
        session.commit()
        logger.info("分类暂存已核验并提交 | task_id=%s | operation_id=%s | stage=%s", task.id, operation.id, operation.status)
    if operation.status == "destination_verified":
        source_digest, witness_stat = operation.source_sha256, operation.evidence["witness_snapshot"]
        session.commit()
        digest, _ = hash_stable(witness, witness_stat)
        if digest != source_digest:
            raise ValueError("staging_changed")
        check_gate(session, settings, task)
        if target.exists():
            if not witness.samefile(target):
                raise ValueError("destination_ownership_conflict")
        else:
            os.link(witness, target)  # Atomic publication, fails on an existing name.
        record_published(session, lineage_id)
        stat = snapshot(target)
        key = path_key(target)
        current = session.execute(select(Location).filter_by(current_path_key=key)).scalar_one_or_none()
        if current is not None and current.variant_id != operation.variant_id:
            raise ValueError("destination_tracking_conflict")
        if current is None:
            session.add(Location(variant_id=operation.variant_id, role=operation.evidence["destination_role"],
                path=str(target), current_path_key=key, **stat))
        operation.status = "published"
        session.commit()
        logger.info("分类文件已发布，目标位置及血缘已提交 | task_id=%s | operation_id=%s | target_role=%s", task.id, operation.id, operation.evidence["destination_role"])
    if operation.status == "published":
        check_gate(session, settings, task)
        if not witness.is_file() or not target.is_file() or not witness.samefile(target):
            operation.status = "conflict"
            session.commit()
            raise ValueError("published_destination_changed")
        source_digest, witness_stat = operation.source_sha256, operation.evidence["witness_snapshot"]
        session.commit()
        if hash_stable(target, witness_stat)[0] != source_digest:
            raise ValueError("published_checksum_mismatch")
        verify_operation_source(session, lineage_id, allow_missing=True)
        check_gate(session, settings, task)
        if not witness.is_file() or not target.is_file() or not witness.samefile(target) or snapshot(target) != witness_stat:
            raise ValueError("published_destination_changed")
        # Mapping and destination Location have committed before this source cleanup.
        source.unlink(missing_ok=True)
        record_source_removed(session, lineage_id)
        for old in session.execute(select(Location).filter_by(current_path_key=path_key(source))).scalars():
            if old.variant_id == operation.variant_id:
                old.status, old.current_path_key, old.missing_since = "retired", None, None
        operation.status = "source_cleaned"
        session.commit()
        witness.unlink(missing_ok=True)
        logger.info("分类搬运完成，源清理已记录 | task_id=%s | operation_id=%s | stage=%s | asset_label_unchanged=true", task.id, operation.id, operation.status)
