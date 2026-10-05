"""Local preference filtering; importing this package performs no application I/O."""


def register_video_filter():
    """Register the local control API once, without starting workers or schedulers."""
    from app import app
    import video_filter.models
    import media_lineage.models
    from video_filter.apis.control import blueprint

    if blueprint.name not in app.blueprints:
        app.register_blueprint(blueprint)
    from .dashboard import register_dashboard
    register_dashboard(app)
    from env import EnvConfig

    if EnvConfig.video_filter_enabled():
        from app import scheduler
        from .runtime import register_schedules

        register_schedules(scheduler, app)


__all__ = ["register_video_filter"]
