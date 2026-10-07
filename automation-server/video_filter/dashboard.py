"""Read-only video_filter Jinja pages; all data stays local."""

import logging
from datetime import datetime, timezone

from flask import Blueprint, make_response, render_template, request

from env import EnvConfig
from .observability import log_failure

logger = logging.getLogger(__name__)
blueprint = Blueprint("video_filter_dashboard", __name__, url_prefix="/video_filter/dashboard",
                      template_folder="templates", static_folder="static")
MODEL_NAMES = {"logistic_regression": "逻辑回归", "mil": "注意力 MIL"}
TASK_NAMES = {"scan": "目录扫描", "extract": "特征提取", "train": "模型训练", "predict": "双模型预测", "classify": "分类搬运"}
STATE_NAMES = {"queued": "排队中", "running": "进行中", "succeeded": "已完成", "failed": "失败", "cancelled": "已取消", "active": "使用中", "validated": "验证未达标", "retired": "已退休"}


def _view(training=False):
    view = {"updated_at": datetime.now(timezone.utc).isoformat(), "tasks": [], "current_task": None,
            "error_code": None, "counts": {}, "models": {}, "actual_accuracy": {}, "last_scan": None}
    try:
        settings = EnvConfig.video_filter_settings()
        if not settings["enabled"]:
            return {**view, "enabled": False}
        from app import db
        from .reporting import dashboard_details, status_snapshot
        from .reporting import training_details
        details = training_details if training else dashboard_details
        if settings.get("grouped"):
            from .reporting import groups_snapshot
            from .scope import group_scope
            overview = groups_snapshot(db.session, settings)
            name = request.args.get("group")
            group = next((item for item in settings["groups"] if item["name"] == name), None)
            view.update(grouped=True, groups=overview["groups"], resources=overview["resources"], global_running=overview["running_tasks"], enabled=True)
            if group is None:
                return view
            if training and not group["enabled"]:
                return {**view, "enabled": False, "group_name": group["name"]}
            with group_scope(db.session, group, create=False) as scoped:
                return {**view, **status_snapshot(db.session, scoped, include_versions=True), **details(db.session, scoped)}
        return {**view, **status_snapshot(db.session, settings, include_versions=True), **details(db.session, settings)}
    except (RuntimeError, OSError, ValueError) as error:
        view["error_code"] = "invalid_configuration"
        log_failure(logger, "进度页面配置不可用", error)
    except Exception as error:
        from app import db
        db.session.rollback()
        view["error_code"] = "schema_unavailable"
        log_failure(logger, "进度页面数据不可用", error)
    return view


def _render(template, training=False):
    response = make_response(render_template(template, view=_view(training),
        active_service="video_filter_training" if training else "video_filter",
        model_names=MODEL_NAMES, task_names=TASK_NAMES, state_names=STATE_NAMES))
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.get("/")
def index():
    return _render("video_filter/dashboard.html")


@blueprint.get("/fragment")
def fragment():
    return _render("video_filter/_snapshot.html")


@blueprint.get("/training")
def training():
    return _render("video_filter/training.html", training=True)


@blueprint.get("/training/fragment")
def training_fragment():
    return _render("video_filter/_training.html", training=True)


def register_dashboard(app):
    if blueprint.name not in app.blueprints:
        app.register_blueprint(blueprint)
    from dashboard import register_service
    register_service(app, key="video_filter", title="视频偏好筛选", description="特征摘要、模型训练、双模型预测与用户反馈。",
                     endpoint="video_filter_dashboard.index")
    register_service(app, key="video_filter_training", title="模型训练",
                     description="查看模型验证成绩、训练进度，并手动启动训练。",
                     endpoint="video_filter_dashboard.training")
