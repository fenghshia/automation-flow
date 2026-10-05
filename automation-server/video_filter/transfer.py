"""Non-overwriting publication with a retained hard-link witness for recovery."""

import os
import logging
import shutil
from pathlib import Path

from sqlalchemy import select

from media_lineage.service import begin_operation, record_published, record_source_removed, verify_operation_source
from .configuration import record_configuration
from .feature_store import FeatureStore
from .identity import hash_stable, path_key, snapshot
from .learning import score
from .prediction import predict_both
from .models import Asset, ConfigRevision, FeatureBundle, Location, ModelRun, Prediction, TransferOperation, Variant


logger = logging.getLogger(__name__)


def check_gate(session, settings, task):
    config = record_configuration(session, settings)
    if not settings.get("transfer_enabled") or config.id != task.config_revision_id or config.active_slot != "active":
        raise ValueError("transfer_disabled_or_configuration_changed")
    if any((settings["state_directory"] / "feedback").glob("*.json")):
        raise ValueError("unresolved_feedback_journal")
    variant = session.get(Variant, task.variant_id)
    asset = session.get(Asset, variant.asset_id, populate_existing=True, with_for_update=True)
    if asset.label_revision != task.input_snapshot["label_revision"]:
        raise ValueError("asset_feedback_changed")
    model = session.get(ModelRun, task.input_snapshot["model_id"], populate_existing=True)
    if model is None or model.model_type != settings.get("classifier", "logistic_regression"):
        raise ValueError("selected_classifier_changed")
    bundle = FeatureStore().require_ready(session, task.input_snapshot["bundle_id"])
    if bundle.manifest["variant_id"] != variant.id:
        raise ValueError("summary_variant_mismatch")
    probability = score(session, model, bundle)
    return variant, asset, model, probability


def classify(session, settings, task):
    operation = session.execute(select(TransferOperation).filter_by(task_id=task.id)).scalar_one_or_none()
    if operation is None:
        variant, asset, model, probability = check_gate(session, settings, task)
        source = Path(task.input_snapshot["path"])
        if source.parent.resolve() != settings["directories"]["unclassified"].resolve():
            raise ValueError("source_outside_unclassified")
        digest, stat = hash_stable(source, task.input_snapshot["source_snapshot"])
        if digest != variant.sha256:
            raise ValueError("source_version_changed")
        label = int(probability >= model.threshold)
        role = "predicted_like" if label else "predicted_dislike"
        target = (settings["directories"][role] / source.name).resolve()
        if target.exists():
            raise ValueError("destination_name_conflict")
        predictions = predict_both(session, task, settings.get("classifier", "logistic_regression"))
        prediction = predictions[model.model_type]
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
        logger.info("分类搬运开始复制及校验 | task_id=%s | operation_id=%s", task.id, operation.id)
        verify_operation_source(session, lineage_id)
        if target.exists():
            raise ValueError("destination_name_conflict")
        if witness.exists():
            # An uncommitted staging file may be incomplete. Preserve it for review.
            operation.status = "conflict"
            session.commit()
            raise ValueError("unverified_staging_conflict")
        with source.open("rb") as origin, witness.open("xb") as output:
            shutil.copyfileobj(origin, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        digest, stat = hash_stable(witness)
        verify_operation_source(session, lineage_id)
        if digest != operation.source_sha256:
            raise ValueError("staging_checksum_mismatch")
        operation.evidence = {**operation.evidence, "witness_snapshot": stat}
        operation.status = "destination_verified"
        session.commit()
        logger.info("分类暂存已核验并提交 | task_id=%s | operation_id=%s | stage=%s", task.id, operation.id, operation.status)
    if operation.status == "destination_verified":
        digest, _ = hash_stable(witness, operation.evidence["witness_snapshot"])
        if digest != operation.source_sha256:
            raise ValueError("staging_changed")
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
        if hash_stable(target, operation.evidence["witness_snapshot"])[0] != operation.source_sha256:
            raise ValueError("published_checksum_mismatch")
        verify_operation_source(session, lineage_id, allow_missing=True)
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
