"""Public workflow bindings and pinned compression context."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from sqlalchemy import select
from .models import WorkflowBinding

_binding = ContextVar("media_workflow", default=None)


def current_binding():
    return _binding.get()


@contextmanager
def workflow_scope(binding):
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)


def publish_binding(session, settings, revision_id):
    row = session.get(WorkflowBinding, settings["dataset_group_id"])
    if row is None:
        row = WorkflowBinding(id=settings["dataset_group_id"])
        session.add(row)
    row.name, row.reset_epoch = settings["name"], settings["reset_epoch"]
    row.directory_revision_id = revision_id
    row.source_directory = str(settings["directories"]["liked_source"].resolve())
    row.destination_directory = str(settings["directories"]["liked"].resolve())
    row.enabled = bool(settings["enabled"] and settings.get("compression_enabled"))
    session.commit()


def evidence_for(source, destination, session):
    parent, target = Path(source).resolve().parent, Path(destination).resolve().parent
    rows = session.execute(select(WorkflowBinding).where(WorkflowBinding.enabled == True)).scalars()
    matches = [row for row in rows if Path(row.source_directory).resolve() == parent and Path(row.destination_directory).resolve() == target]
    if len(matches) > 1:
        raise ValueError("ambiguous_compression_workflow")
    if not matches:
        return {}
    row = matches[0]
    return {"dataset_group_id": row.id, "group_name": row.name, "reset_epoch": row.reset_epoch,
            "directory_revision_id": row.directory_revision_id}
