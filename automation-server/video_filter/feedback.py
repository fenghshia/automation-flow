"""Durable, replayable user feedback; model predictions never enter this module."""

import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update

from .features.contract import canonical_json, is_sha256
from .models import Asset, FeedbackEvent, ModelRun
from .models.records import utc_now
from .observability import log_failure, redact_paths


logger = logging.getLogger(__name__)


class FeedbackConflict(ValueError):
    pass


def apply_feedback(session, *, asset_id, label, expected_revision, event_key, evidence):
    if type(label) is not int or label not in (0, 1) or type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Feedback must be 0/1 with a nonnegative expected revision.")
    if not is_sha256(event_key) or not isinstance(evidence, dict) or evidence.get("reason") not in (
        "user_confirmed", "user_deleted", "entered_confirmed_like",
    ):
        raise ValueError("Feedback requires explicit user evidence and an idempotent key.")
    existing = session.execute(select(FeedbackEvent).filter_by(event_key=event_key)).scalar_one_or_none()
    if existing:
        if (existing.asset_id, existing.label, existing.expected_revision, existing.evidence) != (asset_id, label, expected_revision, evidence):
            raise FeedbackConflict("Feedback replay conflicts with its stored event.")
        return existing
    asset = session.execute(select(Asset).where(Asset.id == asset_id).with_for_update()).scalar_one_or_none()
    if asset is None or asset.label_revision != expected_revision:
        raise FeedbackConflict("The asset changed; stale feedback cannot overwrite it.")
    event = FeedbackEvent(event_key=event_key, asset_id=asset_id, label=label,
                          expected_revision=expected_revision, resulting_revision=expected_revision + 1,
                          evidence=evidence)
    session.add(event)
    session.flush()
    changed = session.execute(update(Asset).where(
        Asset.id == asset_id, Asset.label_revision == expected_revision,
    ).values(label=label, label_revision=expected_revision + 1,
             label_event_id=event.id, label_updated_at=utc_now())).rowcount
    if changed != 1:
        raise FeedbackConflict("The asset changed; stale feedback cannot overwrite it.")
    from .evaluation import record_outcomes
    record_outcomes(session, event)
    # A newly reviewed unseen video does not invalidate either classifier.
    # Only classifiers whose recorded dataset became stale must be retired.
    for model in session.execute(select(ModelRun).where(ModelRun.status == "active").with_for_update()).scalars():
        if any(item["asset_id"] == asset_id for item in model.dataset_snapshot):
            model.status, model.active_slot = "retired", None
    return event


class FeedbackJournal:
    def __init__(self, state_directory):
        self.root = Path(state_directory).resolve() / "feedback"
        redact_paths([state_directory])

    def write(self, values):
        if not isinstance(values, dict) or set(values) != {"asset_id", "label", "expected_revision", "event_key", "evidence"}:
            raise ValueError("Invalid feedback journal record.")
        from uuid import UUID

        if not isinstance(values["asset_id"], str) or str(UUID(values["asset_id"])) != values["asset_id"]:
            raise ValueError("Canonical asset ID required.")
        if type(values["label"]) is not int or values["label"] not in (0, 1) or type(values["expected_revision"]) is not int or values["expected_revision"] < 0:
            raise ValueError("Invalid feedback label or revision.")
        encoded = canonical_json(values).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("Feedback exceeds journal size limit.")
        key = values.get("event_key")
        if not is_sha256(key):
            raise ValueError("Invalid feedback event key.")
        self.root.mkdir(parents=True, exist_ok=True)
        final = self.root / (key + ".json")
        if final.exists():
            if final.read_bytes() != encoded:
                raise FeedbackConflict("Conflicting local feedback event.")
            return final
        temporary = self.root / (str(uuid4()) + ".pending")
        try:
            with temporary.open("xb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            # Hard-link publication is atomic and never overwrites an existing event.
            try:
                os.link(temporary, final)
            except FileExistsError:
                if final.read_bytes() != encoded:
                    raise FeedbackConflict("Conflicting local feedback event.")
        finally:
            temporary.unlink(missing_ok=True)
        return final

    def submit(self, session, values):
        path = self.write(values)
        try:
            event = apply_feedback(session, **values)
            session.commit()
        except Exception:
            session.rollback()
            raise
        path.unlink(missing_ok=True)
        logger.info("用户反馈已提交 | asset_id=%s | label=%s | label_revision=%s | reason=%s | 已记录预测结果，受影响模型等待重新训练",
            event.asset_id, event.label, event.resulting_revision, event.evidence.get("reason"))
        from .evaluation import actual_metrics
        try:
            metrics = actual_metrics(session)
            for kind, values in metrics["models"].items():
                logger.info("模型实际准确率 | model_type=%s | reviewed_assets=%s | accuracy=%s | dislike_precision=%s | paired_assets=%s | paired_accuracy=%s",
                    kind, values["reviewed_assets"], values["accuracy"], values["dislike_precision"],
                    metrics["paired"]["reviewed_assets"], metrics["paired"][kind]["accuracy"])
        except Exception as error:
            # The feedback/outcome transaction already committed. Reporting
            # failure must not turn a durable user decision into failed feedback.
            session.rollback()
            log_failure(logger, "反馈已提交，但实际准确率日志统计失败", error)
        return event

    def replay(self, session, limit=100):
        if not self.root.is_dir():
            return {"applied": 0, "conflicts": 0, "superseded": 0}
        result = {"applied": 0, "conflicts": 0, "superseded": 0}
        for path in sorted(self.root.glob("*.json"))[:limit]:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                logger.warning("反馈日志无法安全重放 | event_key=%s | reason=unsafe_or_oversized_record", path.stem)
                result["conflicts"] += 1
                continue
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
                if values["event_key"] + ".json" != path.name:
                    raise ValueError("Feedback journal identity mismatch.")
                self.submit(session, values)
                result["applied"] += 1
            except FeedbackConflict as error:
                session.rollback()
                asset = session.get(Asset, values["asset_id"])
                if asset and asset.label_revision > values["expected_revision"]:
                    archived = path.with_suffix(".superseded")
                    if archived.exists() and archived.read_bytes() != path.read_bytes():
                        result["conflicts"] += 1
                        continue
                    if not archived.exists():
                        os.link(path, archived)
                    path.unlink()
                    result["superseded"] += 1
                    logger.info("过期反馈已归档 | asset_id=%s | current_revision=%s | expected_revision=%s", asset.id, asset.label_revision, values["expected_revision"])
                else:
                    log_failure(logger, "反馈重放发生冲突 | event_key=%s", error, path.stem)
                    result["conflicts"] += 1
            except (ValueError, KeyError, TypeError) as error:
                log_failure(logger, "反馈重放失败 | event_key=%s", error, path.stem)
                session.rollback()
                result["conflicts"] += 1
        return result


def feedback_values(asset, label, evidence):
    values = {"asset_id": asset.id, "label": label, "expected_revision": asset.label_revision,
              "evidence": evidence}
    values["event_key"] = hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()
    return values
