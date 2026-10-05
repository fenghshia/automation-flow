"""Idempotent append/lookup; the caller owns the durable transaction commit."""

import re
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .models import LineageEvent


PHASES = ("planned", "destination_verified", "published", "source_cleaned")
FIELDS = (
    "operation_id", "sequence", "producer", "generation", "phase",
    "source_sha256", "destination_sha256", "source_path", "destination_path", "evidence",
)


def append_event(session, **values):
    values.setdefault("evidence", {})
    if not isinstance(values["evidence"], dict):
        raise ValueError("Lineage evidence must be a dictionary.")
    if set(values) != set(FIELDS):
        raise ValueError("Incomplete lineage event.")
    for field in ("operation_id", "generation"):
        if not isinstance(values[field], str) or str(UUID(values[field])) != values[field]:
            raise ValueError("Canonical operation/generation UUID required.")
    sequence = values["sequence"]
    if type(sequence) is not int or not 0 <= sequence < len(PHASES) or values["phase"] != PHASES[sequence]:
        raise ValueError("Invalid lineage phase sequence.")
    if values["producer"] not in ("video_filter", "video_compression"):
        raise ValueError("Unknown lineage producer.")
    for field in ("source_sha256", "destination_sha256"):
        digest = values[field]
        if digest is None and field == "destination_sha256" and sequence == 0:
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("A verified SHA-256 is required.")
    if not all(isinstance(values[field], str) and values[field] for field in ("source_path", "destination_path")):
        raise ValueError("Source and destination snapshots are required.")
    # A sequence ID alone does not order PostgreSQL commits: a later ID can commit
    # first and cause a cursor reader to skip the earlier, uncommitted event.
    # Serialize event allocation until the caller commits the short transaction.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 731596007})
    query = select(LineageEvent).filter_by(operation_id=values["operation_id"], sequence=sequence)
    existing = session.execute(query).scalar_one_or_none()
    if existing is not None:
        if any(getattr(existing, field) != values[field] for field in FIELDS):
            raise ValueError("Conflicting replay of a lineage event.")
        return existing
    if sequence:
        previous = session.execute(select(LineageEvent).filter_by(
            operation_id=values["operation_id"], sequence=sequence - 1,
        )).scalar_one_or_none()
        if previous is None:
            raise ValueError("Previous lineage phase is missing.")
        for field in ("producer", "generation", "source_sha256", "source_path", "destination_path"):
            if getattr(previous, field) != values[field]:
                raise ValueError("Lineage operation identity changed.")
        if sequence > 1 and previous.destination_sha256 != values["destination_sha256"]:
            raise ValueError("Lineage destination changed.")
    record = LineageEvent(**values)
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        existing = session.execute(query).scalar_one_or_none()
        if existing is None or any(getattr(existing, field) != values[field] for field in FIELDS):
            raise
        return existing
    return record


def read_events(session, after_id=0, limit=100):
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid event cursor or batch size.")
    return session.execute(select(LineageEvent).where(
        LineageEvent.id > after_id,
    ).order_by(LineageEvent.id).limit(limit)).scalars().all()


def operation_events(session, operation_id):
    return session.execute(select(LineageEvent).filter_by(operation_id=operation_id).order_by(LineageEvent.sequence)).scalars().all()


def begin_operation(session, *, producer, source_path, destination_path, generation, source_role=None, destination_role=None, scope_evidence=None):
    from pathlib import Path
    from uuid import uuid4
    from .files import hash_stable

    digest, stat = hash_stable(source_path)
    operation_id = str(uuid4())
    append_event(session, operation_id=operation_id, sequence=0, phase="planned", producer=producer,
                 generation=generation, source_sha256=digest, destination_sha256=None,
                 source_path=str(Path(source_path).resolve()), destination_path=str(Path(destination_path).resolve()),
                 evidence={"source_snapshot": stat, "source_role": source_role, "destination_role": destination_role, **(scope_evidence or {})})
    return operation_id


def verify_operation_source(session, operation_id, allow_missing=False):
    from pathlib import Path
    from .files import hash_stable

    events = operation_events(session, operation_id)
    if not events:
        raise ValueError("Media operation was not registered.")
    start = events[0]
    if not Path(start.source_path).exists() and allow_missing:
        return start
    digest, _ = hash_stable(start.source_path, start.evidence["source_snapshot"])
    if digest != start.source_sha256:
        raise ValueError("Media operation source changed.")
    return start


def record_published(session, operation_id):
    from pathlib import Path
    from .files import hash_stable

    start = verify_operation_source(session, operation_id, allow_missing=True)
    events = operation_events(session, operation_id)
    if not Path(start.source_path).exists() and len(events) < 3:
        raise ValueError("Cannot establish new lineage after source disappeared.")
    digest, stat = hash_stable(start.destination_path)
    if len(events) > 1 and events[1].destination_sha256 != digest:
        raise ValueError("Published destination changed.")
    if len(events) > 1 and events[1].evidence.get("destination_identity") != stat["file_identity"]:
        raise ValueError("Published destination identity changed.")
    evidence = {**start.evidence, "destination_size_bytes": stat["size_bytes"],
                "destination_modified_ns": stat["modified_ns"], "destination_identity": stat["file_identity"]}
    for sequence in (1, 2):
        if len(events) > sequence:
            continue
        append_event(session, operation_id=operation_id, sequence=sequence, phase=PHASES[sequence],
                     producer=start.producer, generation=start.generation, source_sha256=start.source_sha256,
                     destination_sha256=digest, source_path=start.source_path, destination_path=start.destination_path,
                     evidence=evidence)
    return digest


def record_source_removed(session, operation_id):
    from pathlib import Path
    from .files import hash_stable

    events = operation_events(session, operation_id)
    if len(events) < 3:
        raise ValueError("Publication must be recorded before source cleanup.")
    if len(events) == 4:
        return events[-1]
    start, published = events[0], events[2]
    if Path(start.source_path).exists():
        raise ValueError("Source still exists.")
    digest, _ = hash_stable(start.destination_path)
    if digest != published.destination_sha256:
        raise ValueError("Published output is missing or changed.")
    return append_event(session, operation_id=operation_id, sequence=3, phase="source_cleaned",
                        producer=start.producer, generation=start.generation, source_sha256=start.source_sha256,
                        destination_sha256=digest, source_path=start.source_path, destination_path=start.destination_path,
                        evidence=published.evidence)
