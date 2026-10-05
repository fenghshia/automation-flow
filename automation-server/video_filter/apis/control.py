from flask import Blueprint, jsonify, request, g
import logging

from env import EnvConfig
from video_filter.observability import log_failure


logger = logging.getLogger(__name__)


blueprint = Blueprint("video_filter", __name__, url_prefix="/video_filter")


@blueprint.before_request
def enter_group():
    try:
        settings = EnvConfig.video_filter_settings()
        if not settings.get("grouped"):
            return None
        name = (request.view_args or {}).get("group_name")
        if name is None:
            if request.endpoint in ("video_filter.groups", "video_filter.status"):
                return None
            return jsonify(error_code="group_required"), 409
        group = next((item for item in settings["groups"] if item["name"] == name), None)
        if group is None or not group["enabled"]:
            return jsonify(error_code="group_not_found"), 404
        from app import db
        from video_filter.scope import group_scope
        manager = group_scope(db.session, group, create=False)
        manager.__enter__()
        g.video_filter_scope = manager
    except (RuntimeError, ValueError, OSError) as error:
        code = "group_not_initialized" if str(error) == "group_not_initialized" else "invalid_configuration"
        if code == "invalid_configuration":
            log_failure(logger, "分组入口配置不可用", error)
        return jsonify(error_code=code), 503
    except Exception as error:
        from app import db
        db.session.rollback()
        log_failure(logger, "分组入口不可用", error)
        return jsonify(error_code="schema_unavailable"), 503


@blueprint.teardown_request
def leave_group(error):
    manager = getattr(g, "video_filter_scope", None)
    if manager:
        manager.__exit__(None, None, None)


@blueprint.get("/groups")
def groups():
    from app import db
    from video_filter.reporting import groups_snapshot
    try:
        return jsonify(groups_snapshot(db.session, EnvConfig.video_filter_settings()))
    except Exception as error:
        db.session.rollback()
        log_failure(logger, "分组概览不可用", error)
        return jsonify(error_code="schema_unavailable"), 503


@blueprint.get("/status")
def status():
    try:
        settings = EnvConfig.video_filter_settings()
    except (RuntimeError, OSError) as error:
        log_failure(logger, "状态查询失败：配置无效", error)
        return jsonify({"enabled": None, "configuration_valid": False,
                        "error_code": "invalid_configuration"}), 503
    if not settings["enabled"]:
        return jsonify({"enabled": False, "implementation_stage": "P7", "automatic_processing": False})
    if settings.get("groups") is not None:
        return groups()
    from app import db
    from sqlalchemy.exc import SQLAlchemyError
    from video_filter.reporting import status_snapshot

    try:
        result = status_snapshot(db.session, settings)
    except SQLAlchemyError as error:
        log_failure(logger, "状态查询失败：数据库 schema 不可用", error)
        db.session.rollback()
        return jsonify({"enabled": True, "configuration_valid": True, "error_code": "schema_unavailable"}), 503
    return jsonify(result)


@blueprint.post("/tasks/<task_id>/retry")
def retry(task_id):
    from app import db
    from sqlalchemy import update
    from video_filter.models import Task, TransferOperation
    from video_filter.configuration import record_configuration

    try:
        settings = _settings()
        config = record_configuration(db.session, settings)
        task = db.session.get(Task, task_id)
        if task is None:
            return jsonify(error_code="task_not_found"), 404
        if task.config_revision_id != config.id or task.status != "failed" or task.attempts >= 3:
            raise ValueError()
        operation = db.session.execute(db.select(TransferOperation).filter_by(task_id=task_id)).scalar_one_or_none()
        if operation and operation.status == "conflict":
            raise ValueError()
        changed = db.session.execute(update(Task).where(Task.id == task_id, Task.status == "failed").values(
            status="queued", claim_token=None, claimed_at=None, finished_at=None, error_code=None)).rowcount
        db.session.commit()
        if changed != 1:
            raise ValueError()
        logger.info("失败任务已重新入队 | task_id=%s | previous_attempts=%s", task_id, task.attempts)
        return jsonify(task_id=task_id, status="queued"), 202
    except Exception as error:
        log_failure(logger, "任务重试请求失败 | task_id=%s", error, task_id)
        db.session.rollback()
        return jsonify(error_code="retry_requires_current_failed_task_without_conflict"), 409


def _settings():
    settings = EnvConfig.video_filter_settings()
    if not settings["enabled"]:
        raise ValueError("video_filter_disabled")
    return settings


@blueprint.post("/<kind>")
def enqueue(kind):
    if kind not in ("scan", "extract", "train", "predict", "classify"):
        return jsonify(error_code="unknown_operation"), 404
    from app import db
    from video_filter.runtime import enqueue as submit

    data = request.get_json(silent=True)
    if data is None:
        data = {}
    allowed = {"model_type"} if kind == "train" else {"variant_id"}
    if not isinstance(data, dict) or set(data) - allowed:
        return jsonify(error_code="invalid_request"), 400
    if kind == "train" and "model_type" in data and data["model_type"] not in ("logistic_regression", "mil"):
        return jsonify(error_code="invalid_classifier_type"), 400
    if kind in ("extract", "predict", "classify"):
        from uuid import UUID

        try:
            value = data["variant_id"]
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            return jsonify(error_code="canonical_variant_id_required"), 400
    try:
        if kind == "train":
            task = submit(db.session, _settings(), kind, model_type=data.get("model_type"))
        else:
            task = submit(db.session, _settings(), kind, data.get("variant_id"))
        return jsonify(task_id=task.id, status=task.status), 202
    except Exception as error:
        if isinstance(error, ValueError) and str(error) == "insufficient_confirmed_samples_minimum_10_per_class":
            logger.info("训练请求暂缓：每类有效样本需至少 10 个")
        else:
            log_failure(logger, "任务入队请求失败 | kind=%s", error, kind)
        db.session.rollback()
        code = str(error) if isinstance(error, ValueError) and len(str(error)) <= 64 and str(error).replace("_", "").isalnum() else "operation_unavailable"
        return jsonify(error_code=code), 409


@blueprint.get("/metrics")
def metrics():
    from app import db
    from video_filter.evaluation import actual_metrics

    try:
        return jsonify(actual_metrics(db.session))
    except Exception as error:
        log_failure(logger, "实际准确率查询失败", error)
        db.session.rollback()
        return jsonify(error_code="schema_unavailable"), 503


@blueprint.get("/tasks/<task_id>")
def task_status(task_id):
    from app import db
    from video_filter.models import Task

    try:
        record = db.session.get(Task, task_id)
        if record is None:
            return jsonify(error_code="task_not_found"), 404
        return jsonify(task_id=record.id, kind=record.kind, status=record.status,
            attempts=record.attempts, error_code=record.error_code)
    except Exception as error:
        log_failure(logger, "任务状态查询失败 | task_id=%s", error, task_id)
        db.session.rollback()
        return jsonify(error_code="schema_unavailable"), 503


@blueprint.get("/tasks")
def tasks():
    from app import db
    from sqlalchemy import select
    from video_filter.models import Task
    try:
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
        if not 1 <= limit <= 100 or offset < 0:
            return jsonify(error_code="invalid_page"), 400
        rows = db.session.execute(select(Task.id, Task.kind, Task.status, Task.attempts, Task.error_code)
            .order_by(Task.created_at.desc(), Task.id).limit(limit).offset(offset)).all()
        return jsonify(tasks=[dict(row._mapping) for row in rows], next_offset=offset + len(rows))
    except (TypeError, ValueError):
        return jsonify(error_code="invalid_page"), 400
    except Exception as error:
        db.session.rollback()
        log_failure(logger, "任务列表查询失败", error)
        return jsonify(error_code="schema_unavailable"), 503


@blueprint.get("/assets")
def assets():
    """Bounded local inventory for feedback; never load feature payloads."""
    from pathlib import Path
    from sqlalchemy import select
    from app import db
    from video_filter.models import Asset, Variant, Location, FeatureBundle

    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify(error_code="invalid_page"), 400
    try:
        rows = db.session.execute(select(Asset.id, Asset.label, Asset.label_revision)
            .order_by(Asset.created_at, Asset.id).limit(limit).offset(offset)).all()
        result = []
        for asset_id, label, revision in rows:
            variants = []
            for variant_id in db.session.execute(select(Variant.id).where(Variant.asset_id == asset_id)).scalars():
                locations = [{"role": role, "file_name": Path(path).name, "status": status} for role, path, status in
                    db.session.execute(select(Location.role, Location.path, Location.status).where(Location.variant_id == variant_id))]
                summaries = list(db.session.execute(select(FeatureBundle.id).where(FeatureBundle.variant_id == variant_id, FeatureBundle.status == "ready")).scalars())
                variants.append({"variant_id": variant_id, "locations": locations, "ready_bundle_ids": summaries})
            result.append({"asset_id": asset_id, "label": label, "label_revision": revision, "variants": variants})
        return jsonify(assets=result, next_offset=offset + len(result))
    except Exception as error:
        log_failure(logger, "资产列表查询失败", error)
        db.session.rollback()
        return jsonify(error_code="schema_unavailable"), 503


@blueprint.post("/feedback")
def feedback():
    from app import db
    from video_filter.feedback import FeedbackJournal

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {"asset_id", "label", "expected_revision", "event_key"}:
        return jsonify(error_code="invalid_feedback"), 400
    from uuid import UUID

    try:
        if not isinstance(data["asset_id"], str) or str(UUID(data["asset_id"])) != data["asset_id"]:
            raise ValueError()
        settings = _settings()
        event = FeedbackJournal(settings["state_directory"]).submit(db.session,
            {**data, "evidence": {"reason": "user_confirmed"}})
        return jsonify(event_id=event.id, label=event.label, label_revision=event.resulting_revision)
    except Exception as error:
        log_failure(logger, "人工反馈提交失败", error)
        db.session.rollback()
        return jsonify(error_code="feedback_invalid_or_stale"), 409


def _grouped(view):
    def handle(group_name, **values):
        return view(**values)
    handle.__name__ = "grouped_" + view.__name__
    return handle


for _path, _view, _methods in (
    ("/status", status, ["GET"]), ("/assets", assets, ["GET"]), ("/metrics", metrics, ["GET"]),
    ("/tasks/<task_id>", task_status, ["GET"]), ("/tasks/<task_id>/retry", retry, ["POST"]),
    ("/tasks", tasks, ["GET"]),
    ("/<kind>", enqueue, ["POST"]), ("/feedback", feedback, ["POST"]),
):
    blueprint.add_url_rule("/groups/<group_name>" + _path, view_func=_grouped(_view), methods=_methods)
