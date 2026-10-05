"""Opt-in compression integration through the public durable operation contract."""

from contextlib import contextmanager
from functools import wraps
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select

from .models import LineageEvent
from .service import begin_operation, record_published, record_source_removed, verify_operation_source


def enabled():
    from env import EnvConfig

    if not EnvConfig.video_filter_enabled():
        return False
    return EnvConfig.video_filter_settings().get("grouped", False) or EnvConfig._boolean("VIDEO_FILTER_LINEAGE_ENABLED")


def generation_for(mission):
    return str(uuid5(NAMESPACE_URL, "automation-flow/compression/" + str(mission.id) + "/" + str(mission.attempts)))


def operation_for(session, generation):
    event = session.execute(select(LineageEvent).filter_by(producer="video_compression", generation=generation, sequence=0)).scalar_one_or_none()
    if event is None:
        raise ValueError("Compression lineage is missing; preserve source for review.")
    return event.operation_id


def compression_roles(source, destination):
    """A compression operation proves identity, not the user's preference."""
    from pathlib import Path
    from env import EnvConfig

    settings = EnvConfig.video_filter_settings()
    if settings.get("grouped"):
        from app import db
        from .workflows import evidence_for
        evidence = evidence_for(source, destination, db.session)
        return ("liked_source", "liked") if evidence else (None, None)
    def role_for(path):
        parent = Path(path).resolve().parent
        for role, root in settings["directories"].items():
            if parent == root or (parent.exists() and root.exists() and parent.samefile(root)):
                return role
        return None
    return role_for(source), role_for(destination)


def compression_begin(session, mission, source, destination):
    if not enabled():
        return
    _check_mission_binding(session, mission)
    generation = generation_for(mission)
    event = session.execute(select(LineageEvent).filter_by(producer="video_compression", generation=generation, sequence=0)).scalar_one_or_none()
    if event:
        verify_operation_source(session, event.operation_id)
    else:
        source_role, destination_role = compression_roles(source, destination)
        begin_operation(session, producer="video_compression", source_path=source, destination_path=destination,
            generation=generation, source_role=source_role, destination_role=destination_role,
            scope_evidence=_evidence(session, source, destination))
    session.commit()


def compression_published(session, mission, allow_missing=False):
    if not enabled():
        return
    _check_mission_binding(session, mission)
    operation = operation_for(session, generation_for(mission))
    verify_operation_source(session, operation, allow_missing=allow_missing)
    record_published(session, operation)
    session.commit()


def compression_cleaned(session, mission):
    if enabled():
        _check_mission_binding(session, mission)
        record_source_removed(session, operation_for(session, generation_for(mission)))
        session.commit()


def serialized_gpu(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from env import EnvConfig
        from .resources import gpu_lock

        if not EnvConfig.video_filter_enabled():
            return function(*args, **kwargs)
        settings = EnvConfig.video_filter_settings()
        if settings.get("grouped"):
            from app import db
            from .resources import gpu_identity, request_lease, release, lease_scope
            import os
            from .resources import process_identity
            from .workflows import current_binding
            binding = current_binding()
            scheduled = function.__name__ == "process_one_mission"
            request_id = "scheduled:" + (binding["id"] if binding else "standalone") if scheduled else "direct:" + str(uuid4())
            owner = "compression:" + str(os.getpid()) + ":" + process_identity(os.getpid()) + ":" + request_id
            # FFmpeg's NVENC uses physical adapter 0, independent of CUDA visibility.
            device = gpu_identity("driver:0")
            lease = request_lease(db.session, device, "exclusive_compression", owner)
            if not lease:
                if not scheduled:
                    release(db.session, owner=owner)
                return None
            try:
                with lease_scope(lease):
                    return function(*args, **kwargs)
            finally:
                release(db.session, lease)
        with gpu_lock(settings["state_directory"]) as acquired:
            if not acquired:
                return None
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def direct_compression(source, destination):
    """Direct service uses the same durable guard; registration stays lazy."""
    if not enabled():
        yield None
        return
    from app import db

    source_role, destination_role = compression_roles(source, destination)
    operation = begin_operation(db.session, producer="video_compression", source_path=source,
        destination_path=destination, generation=str(uuid4()), source_role=source_role, destination_role=destination_role,
        scope_evidence=_evidence(db.session, source, destination))
    db.session.commit()
    yield operation


def direct_published(operation):
    if operation:
        from app import db

        verify_operation_source(db.session, operation)
        record_published(db.session, operation)
        db.session.commit()


def direct_cleaned(operation):
    if operation:
        from app import db

        record_source_removed(db.session, operation)
        db.session.commit()


def _evidence(session, source, destination):
    from env import EnvConfig
    if EnvConfig.video_filter_settings().get("grouped"):
        from .workflows import evidence_for
        return evidence_for(source, destination, session)
    return {}


def _check_mission_binding(session, mission):
    identifier = getattr(mission, "workflow_id", None)
    if not identifier:
        return
    from .models import WorkflowBinding
    from env import EnvConfig
    row = session.get(WorkflowBinding, identifier, populate_existing=True)
    configured = next((g for g in EnvConfig.video_filter_settings(ignore_scope=True).get("groups", [])
                       if g["name"] == (row.name if row else None) and g["enabled"] and g["compression_enabled"]), None)
    if row is None or not row.enabled or configured is None or (row.reset_epoch, row.directory_revision_id, row.destination_directory) != (
            mission.reset_epoch, mission.directory_revision_id, mission.pinned_output_directory) or (
            str(configured["directories"]["liked_source"].resolve()), str(configured["directories"]["liked"].resolve())) != (row.source_directory, row.destination_directory):
        raise ValueError("compression_workflow_changed_preserve_source")
