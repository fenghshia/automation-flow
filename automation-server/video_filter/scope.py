"""Explicit dataset scope for ORM queries, inserts, manifests and runtime settings."""

import sys
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

TEST_GROUP = "00000000-0000-4000-8000-000000000001"
TEST_EPOCH = "00000000-0000-4000-8000-000000000002"
_scope = ContextVar("video_filter_dataset_scope", default=None)


def current_scope():
    return _scope.get()


def isolated_test():
    return getattr(sys.modules.get("app"), "_video_filter_test_app", False)


def group_default():
    value = current_scope()
    if value:
        return value["id"]
    if isolated_test():
        return TEST_GROUP
    raise ValueError("dataset_group_scope_required")


def epoch_default():
    value = current_scope()
    if value:
        return value["epoch"]
    if isolated_test():
        return TEST_EPOCH
    raise ValueError("dataset_group_scope_required")


@contextmanager
def group_scope(session, settings, create=True):
    from sqlalchemy import select, func
    from .models.records import DatasetGroup, RuntimeState
    from media_lineage.models import LineageEvent
    if session.new or session.dirty or session.deleted:
        raise ValueError("uncommitted_scope_transition")
    session.expunge_all()
    if create and session.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 731596008})
    state = session.get(RuntimeState, "active")
    if state is None:
        if not create:
            raise ValueError("group_not_initialized")
        state = RuntimeState(id="active", epoch=str(uuid4()))
        session.add(state)
        session.flush()
    group = session.execute(select(DatasetGroup).where(DatasetGroup.name == settings["name"])).scalar_one_or_none()
    if group is None:
        if not create:
            raise ValueError("group_not_initialized")
        group = DatasetGroup(name=settings["name"], reset_epoch=state.epoch,
            lineage_floor=session.scalar(select(func.max(LineageEvent.id))) or 0)
        session.add(group)
        session.flush()
    if create:
        group.enabled = settings.get("enabled", True)
    session.commit()
    resolved = {**settings, "dataset_group_id": group.id, "reset_epoch": group.reset_epoch,
                "lineage_floor": group.lineage_floor, "resource_state_directory": settings["state_directory"],
                "state_directory": settings["state_directory"] / "groups" / group.id / group.reset_epoch}
    value = {"id": group.id, "epoch": group.reset_epoch, "settings": resolved}
    token = _scope.set(value)
    session.expunge_all()
    try:
        yield resolved
    finally:
        session.rollback()
        session.expunge_all()
        _scope.reset(token)


def install_scope_guards(record):
    from sqlalchemy import event
    from sqlalchemy.orm import Session, with_loader_criteria

    @event.listens_for(Session, "do_orm_execute")
    def restrict_query(state):
        value = current_scope()
        if value:
            group_id = value["id"]
            epoch = value["epoch"]
            state.statement = state.statement.options(with_loader_criteria(record,
                lambda cls: (cls.dataset_group_id == group_id) & (cls.reset_epoch == epoch), include_aliases=True))
        elif not isolated_test() and state.bind_mapper is not None and issubclass(state.bind_mapper.class_, record):
            raise ValueError("dataset_group_scope_required")

    @event.listens_for(Session, "before_flush")
    def restrict_write(session, context, instances):
        for item in session.new | session.dirty | session.deleted:
            if isinstance(item, record):
                expected, epoch = group_default(), epoch_default()
                if item.dataset_group_id is None and item in session.new:
                    item.dataset_group_id, item.reset_epoch = expected, epoch
                if item.dataset_group_id != expected or item.reset_epoch != epoch:
                    raise ValueError("cross_group_or_stale_epoch_write")
