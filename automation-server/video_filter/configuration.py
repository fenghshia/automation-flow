"""Record pending immutable configuration generations; activation is separate."""

import hashlib
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .features.contract import canonical_json
from .models import ConfigRevision


def _snapshot(value):
    if isinstance(value, Path):
        return os.path.normcase(str(value.resolve()))
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot(item) for item in value]
    return value


def record_configuration(session, settings):
    if settings.get("enabled") is not True:
        raise ValueError("Disabled configuration cannot become a scan generation.")
    snapshot = _snapshot({"name": settings["name"], "directories": settings["directories"]} if settings.get("grouped") else
        {key: value for key, value in settings.items() if key != "release_gpu"})
    digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
    query = select(ConfigRevision).filter_by(signature=digest)
    existing = session.execute(query).scalar_one_or_none()
    if existing is not None:
        return existing
    record = ConfigRevision(signature=digest, snapshot=snapshot, status="pending")
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        existing = session.execute(query).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    return record
