"""Shared Flask/Jinja shell. Services explicitly register their own pages."""

from flask import Blueprint, current_app, redirect, render_template, url_for

blueprint = Blueprint("dashboard", __name__, url_prefix="/dashboard",
                      template_folder="templates", static_folder="static")


@blueprint.app_context_processor
def navigation():
    return {"dashboard_services": list(current_app.extensions.get("autoflow_dashboard", {}).values())}


@blueprint.get("/")
def index():
    return render_template("dashboard/index.html", active_service="overview")


def register_dashboard(app):
    if blueprint.name not in app.blueprints:
        app.register_blueprint(blueprint)
    app.extensions.setdefault("autoflow_dashboard", {})
    if not any(rule.rule == "/" for rule in app.url_map.iter_rules()):
        app.add_url_rule("/", "dashboard_home", lambda: redirect(url_for("dashboard.index")))


def register_service(app, *, key, title, description, endpoint):
    register_dashboard(app)
    entry = {"key": key, "title": title, "description": description, "endpoint": endpoint}
    services = app.extensions["autoflow_dashboard"]
    if key in services and services[key] != entry:
        raise ValueError("Conflicting dashboard service registration.")
    services[key] = entry


__all__ = ["register_dashboard", "register_service"]
