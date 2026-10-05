"""Whole-scope reconciliation. File disappearance alone is never a label event."""

from datetime import timedelta
import logging
import time
from pathlib import Path

from sqlalchemy import select, update
from .observability import log_failure as log_exception, redact_paths

from media_lineage.models import LineageEvent
from .configuration import record_configuration
from .feedback import FeedbackJournal, feedback_values
from .identity import SUPPORTED_SUFFIXES, hash_stable, path_key, snapshot
from .models import Asset, ConfigRevision, FeatureBundle, Location, Observation, ScanRun, Task, Variant
from .models.records import utc_now


logger = logging.getLogger(__name__)


def scan_directories(settings):
    files, results = [], {}
    # The compressor owns its transient source files. Observe only the five
    # explicitly managed directories and reconcile compression through events.
    roots = settings["directories"]
    redact_paths(roots.values())
    for role, root in roots.items():
        try:
            if settings.get("grouped"):
                from .group_config import require_current_group
                require_current_group(settings)
            from .group_config import reject_links
            reject_links(root)
            if not root.is_dir() or root.is_symlink():
                raise OSError("Managed directory is inaccessible.")
            count = 0
            for path in sorted(root.iterdir()):
                reject_links(path)
                if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_file():
                    continue
                redact_paths([path])
                if path.is_symlink() or path.resolve().parent != root.resolve():
                    raise OSError("Unsafe managed file path.")
                files.append((role, path.resolve(), snapshot(path)))
                count += 1
            results[role] = {"accessible": True, "files": count}
        except (OSError, ValueError) as error:
            log_exception(logger, "受管目录扫描失败 | role=%s | deletion_feedback_paused=true", error, role)
            results[role] = {"accessible": False}
    return files, results, all(value["accessible"] for value in results.values())


def consume_lineage(session, after_id=0, limit=1000):
    """Replay verified terminal relationships idempotently before missing inference."""
    watermark = after_id
    for event in session.execute(select(LineageEvent).where(
        LineageEvent.id > after_id,
    ).order_by(LineageEvent.id).limit(limit)).scalars():
        watermark = event.id
        from .scope import current_scope
        scope = current_scope()
        if scope:
            if event.id <= scope["settings"]["lineage_floor"] or (event.evidence.get("dataset_group_id"), event.evidence.get("reset_epoch")) != (scope["id"], scope["epoch"]):
                continue
            from .group_config import require_scope
            try:
                require_scope(scope["settings"], event.source_path)
                require_scope(scope["settings"], event.destination_path)
            except ValueError:
                continue
        if scope and event.phase == "planned" and event.evidence.get("source_role") == "liked_source":
            source = session.execute(select(Variant).filter_by(sha256=event.source_sha256)).scalar_one_or_none()
            if source is None:
                asset = Asset()
                session.add(asset)
                session.flush()
                source = Variant(asset_id=asset.id, sha256=event.source_sha256, size_bytes=event.evidence["source_snapshot"]["size_bytes"])
                session.add(source)
                session.flush()
            if source is not None:
                asset = session.get(Asset, source.asset_id)
                from .feedback import apply_feedback
                import hashlib
                key = hashlib.sha256((scope["id"] + scope["epoch"] + event.operation_id).encode()).hexdigest()
                from .models import FeedbackEvent
                if not session.scalar(select(FeedbackEvent.id).where(FeedbackEvent.event_key == key)):
                    apply_feedback(session, **{**feedback_values(asset, 1, {"reason": "entered_confirmed_like", "operation_id": event.operation_id}), "event_key": key})
            continue
        if event.phase not in ("published", "source_cleaned"):
            continue
        source = session.execute(select(Variant).filter_by(sha256=event.source_sha256)).scalar_one_or_none()
        if source is None:
            stat = event.evidence.get("source_snapshot", {})
            if not stat.get("size_bytes"):
                # Do not advance past an uninterpretable relationship.
                return event.id - 1
            asset = Asset(label=1 if event.evidence.get("source_role") in ("confirmed_like", "liked_source") else None)
            session.add(asset)
            session.flush()
            source = Variant(asset_id=asset.id, sha256=event.source_sha256, size_bytes=stat["size_bytes"])
            session.add(source)
            session.flush()
        destination = session.execute(select(Variant).filter_by(sha256=event.destination_sha256)).scalar_one_or_none()
        if destination is None:
            size = event.evidence.get("destination_size_bytes")
            if type(size) is not int or size <= 0:
                continue
            destination = Variant(asset_id=source.asset_id, sha256=event.destination_sha256,
                                  size_bytes=size, source_variant_id=source.id)
            session.add(destination)
            session.flush()
        elif destination.asset_id != source.asset_id:
            other = session.get(Asset, destination.asset_id)
            has_summary = session.execute(select(FeatureBundle.id).filter_by(variant_id=destination.id).limit(1)).first()
            if other.label is not None or has_summary:
                session.execute(update(Location).where(Location.variant_id == destination.id).values(status="conflict"))
                continue
            destination.asset_id = source.asset_id
            destination.source_variant_id = source.id
        if event.phase == "source_cleaned":
            for location in session.execute(select(Location).where(
                Location.variant_id == source.id, Location.path == event.source_path,
            )).scalars():
                if not Path(location.path).exists():
                    location.status, location.current_path_key = "retired", None
                    location.missing_since = None
        # Preserve a published target even when the user deletes it before scanning.
        key = path_key(event.destination_path)
        current = session.execute(select(Location).filter_by(current_path_key=key)).scalar_one_or_none()
        historical = session.execute(select(Location).where(
            Location.variant_id == destination.id, Location.path == event.destination_path,
        )).first()
        if current is None and historical is None:
            role = event.evidence.get("destination_role", "compressed_like" if event.producer == "video_compression" else None)
            if role not in ("compressed_like", "predicted_like", "predicted_dislike", "liked"):
                continue
            session.add(Location(variant_id=destination.id, role=role, path=event.destination_path,
                                 current_path_key=key, file_identity=event.evidence.get("destination_identity"),
                                 size_bytes=destination.size_bytes, modified_ns=event.evidence.get("destination_modified_ns", 0),
                                 status="present"))
    session.flush()
    return watermark


def _operations_pending(session, asset_id, settings=None, now=None):
    hashes = set(session.execute(select(Variant.sha256).where(Variant.asset_id == asset_id)).scalars())
    events = session.execute(select(LineageEvent).where(LineageEvent.source_sha256.in_(hashes))).scalars().all()
    phases = {}
    latest = {}
    for event in events:
        from .scope import current_scope
        scope = current_scope()
        if scope and (event.evidence.get("dataset_group_id"), event.evidence.get("reset_epoch")) != (scope["id"], scope["epoch"]):
            continue
        phases[event.operation_id] = max(phases.get(event.operation_id, -1), event.sequence)
        latest[event.operation_id] = event
    if settings and settings.get("grouped"):
        for operation_id, sequence in phases.items():
            event = latest[operation_id]
            if sequence < 3 and (now or utc_now()) - event.created_at > timedelta(seconds=settings["task_timeout_seconds"]):
                # Unfinished publication/cleanup is ambiguous, expose a conflict
                # for explicit feedback rather than silently shielding forever.
                variants = session.scalars(select(Variant.id).where(Variant.asset_id == asset_id)).all()
                session.execute(update(Location).where(Location.variant_id.in_(variants), Location.status == "missing").values(status="conflict"))
                logger.warning("血缘超时，缺失位置待核对 | operation_id=%s | asset_id=%s", operation_id, asset_id)
    return any(sequence < 3 for sequence in phases.values())


def reconcile(session, settings, now=None):
    if not settings.get("enabled"):
        raise ValueError("video_filter is disabled.")
    now = now or utc_now()
    started = time.monotonic()
    logger.info("目录扫描开始 | roles=%s", len(settings["directories"]))
    files, role_results, complete = scan_directories(settings)
    config = record_configuration(session, settings)
    scan = ScanRun(config_revision_id=config.id, role_results=role_results, complete=False)
    session.add(scan)
    session.flush()
    logger.info("目录枚举完成 | scan_id=%s | files=%s | roles=%s", scan.id, len(files), role_results)
    if not complete:
        scan.finished_at = now
        session.commit()
        logger.warning("扫描不完整，本轮不判删除 | scan_id=%s | elapsed=%.1fs", scan.id, time.monotonic() - started)
        return scan
    previous = session.execute(select(ConfigRevision).filter_by(active_slot="active")).scalar_one_or_none()
    baseline = previous is None or previous.id != config.id
    if baseline:
        logger.info("建立配置基线，本轮不判删除 | scan_id=%s | config_id=%s", scan.id, config.id)
        if previous:
            previous.status, previous.active_slot = "retired", None
            session.execute(update(Task).where(Task.config_revision_id == previous.id, Task.status == "queued").values(status="cancelled", error_code="configuration_changed"))
            session.flush()
        config.status, config.active_slot = "active", "active"
        # Locations outside the new scope remain historical; no negative feedback.
        roots = {path_key(root) for root in settings["directories"].values()}
        for location in session.execute(select(Location).where(Location.current_path_key.is_not(None))).scalars():
            if path_key(Path(location.path).parent) not in roots:
                location.status, location.current_path_key = "retired", None
                location.missing_since = None
    observed_keys, seen_locations, positive_ids, new_locations = set(), set(), set(), []
    unstable_files = 0
    floor = settings.get("lineage_floor", 0)
    consume_lineage(session, floor if baseline else (session.scalar(select(ScanRun.lineage_watermark).where(ScanRun.complete == True).order_by(ScanRun.created_at.desc()).limit(1)) or floor))
    for role, path, stat in files:
        if settings.get("grouped"):
            from .group_config import require_current_group
            require_current_group(settings)
        key = path_key(path)
        observed_keys.add(key)
        observation = session.execute(select(Observation).filter_by(
            config_revision_id=config.id, path_key=key,
        )).scalar_one_or_none()
        if observation is None:
            observation = Observation(config_revision_id=config.id, path_key=key, stable_since=now,
                                      last_seen_at=now, baseline_entry=baseline or settings.get("notifications_uncertain", False), **stat)
            session.add(observation)
        elif (observation.size_bytes, observation.modified_ns, observation.file_identity) != (
            stat["size_bytes"], stat["modified_ns"], stat["file_identity"],
        ):
            observation.size_bytes, observation.modified_ns = stat["size_bytes"], stat["modified_ns"]
            observation.file_identity, observation.stable_since = stat["file_identity"], now
        observation.last_seen_at = now
        if now - observation.stable_since < timedelta(seconds=settings["stable_seconds"]):
            unstable_files += 1
            continue
        current = session.execute(select(Location).filter_by(current_path_key=key)).scalar_one_or_none()
        try:
            if current and current.status == "present" and (current.size_bytes, current.modified_ns, current.file_identity) == (
                stat["size_bytes"], stat["modified_ns"], stat["file_identity"]):
                digest = session.get(Variant, current.variant_id).sha256
            else:
                digest, _ = hash_stable(path, stat)
        except (OSError, ValueError) as error:
            log_exception(logger, "扫描时文件发生变化，暂缓本轮对账 | scan_id=%s | role=%s", error, scan.id, role)
            complete = False
            continue
        variant = session.execute(select(Variant).filter_by(sha256=digest)).scalar_one_or_none()
        if variant is None:
            asset = Asset()
            session.add(asset)
            session.flush()
            variant = Variant(asset_id=asset.id, sha256=digest, size_bytes=stat["size_bytes"])
            session.add(variant)
            session.flush()
        entered = current is None or current.variant_id != variant.id or current.status == "missing" or current.role != role
        if current is not None and current.variant_id != variant.id:
            current.status, current.current_path_key = "retired", None
            current.missing_since = None
            session.flush()
            current = None
        if current is None:
            current = Location(variant_id=variant.id, role=role, path=str(path), current_path_key=key,
                               size_bytes=stat["size_bytes"], modified_ns=stat["modified_ns"])
            session.add(current)
            new_locations.append(current)
        current.role, current.file_identity = role, stat["file_identity"]
        current.size_bytes, current.modified_ns = stat["size_bytes"], stat["modified_ns"]
        current.status, current.missing_since, current.last_scan_id = "present", None, scan.id
        session.flush()
        seen_locations.add(current.id)
        if role in ("confirmed_like", "liked", "liked_source") and entered and (not settings.get("grouped") or
                (not baseline and not observation.baseline_entry) or session.get(Asset, variant.asset_id).label is None):
            positive_ids.add(variant.asset_id)
    if not complete:
        # A mid-scan source change must not activate a partial baseline.
        session.rollback()
        config = record_configuration(session, settings)
        scan = ScanRun(config_revision_id=config.id, role_results=role_results, complete=False, finished_at=now)
        session.add(scan)
        session.commit()
        logger.warning("扫描途中变化，已回滚部分对账 | scan_id=%s | deletion_feedback_paused=true", scan.id)
        return scan
    previous_scan = session.execute(select(ScanRun).where(ScanRun.complete == True).order_by(ScanRun.created_at.desc()).limit(1)).scalar_one_or_none()
    watermark = previous_scan.lineage_watermark if previous_scan else floor
    scan.lineage_watermark = consume_lineage(session, watermark)
    if settings.get("grouped"):
        from .group_config import require_current_group
        require_current_group(settings)
    backlog = session.execute(select(LineageEvent.id).where(LineageEvent.id > scan.lineage_watermark).limit(1)).first()
    feedback = []
    for asset_id in positive_ids:
        asset = session.get(Asset, asset_id)
        feedback.append(feedback_values(asset, 1, {"reason": "entered_confirmed_like", "scan_id": scan.id}))
    if complete:
        missing_assets = set()
        for location in session.execute(select(Location).where(Location.current_path_key.is_not(None))).scalars():
            if location.id in seen_locations or location.current_path_key in observed_keys:
                continue
            if path_key(Path(location.path).parent) not in {path_key(root) for root in settings["directories"].values()}:
                continue
            if location.status == "conflict":
                continue
            if baseline or settings.get("notifications_uncertain"):
                # Reconfiguration establishes observations, never deletion evidence.
                location.status, location.current_path_key = "retired", None
                location.missing_since = None
                continue
            if settings.get("grouped") and any(new.variant_id == location.variant_id for new in new_locations):
                # New hash-verified arrival plus disappearance proves a move;
                # a pre-existing duplicate does not suppress deletion.
                location.status, location.current_path_key, location.missing_since = "retired", None, None
                continue
            location.status = "missing"
            location.missing_since = location.missing_since or now
            missing_assets.add(session.get(Variant, location.variant_id).asset_id)
        uncertain_now = settings.get("notifications_uncertain") or settings.get("notification_guard", lambda: False)()
        if settings["deletion_feedback_enabled"] and not baseline and not backlog and not uncertain_now:
            for asset_id in missing_assets - positive_ids:
                asset = session.get(Asset, asset_id)
                variants = session.execute(select(Variant.id).where(Variant.asset_id == asset_id)).scalars().all()
                locations = session.execute(select(Location).where(Location.variant_id.in_(variants))).scalars().all()
                if any(loc.status == "conflict" or (loc.status == "present" and not settings.get("grouped")) for loc in locations) or _operations_pending(session, asset_id, settings, now):
                    continue
                missing = [loc for loc in locations if loc.status == "missing"]
                mature = [loc for loc in missing if now - loc.missing_since >= timedelta(seconds=settings["missing_seconds"])]
                if not missing or (not mature if settings.get("grouped") else len(mature) != len(missing)):
                    continue
                if asset.label != 0:
                    feedback.append(feedback_values(asset, 0, {"reason": "user_deleted", "scan_id": scan.id,
                        **({"location_ids": [loc.id for loc in mature]} if settings.get("grouped") else {})}))
                if settings.get("grouped"):
                    for location in mature:
                        location.status, location.current_path_key = "retired", None
    scan.complete, scan.finished_at = complete, now
    journal = FeedbackJournal(settings["state_directory"])
    for values in feedback:
        journal.write(values)
    session.commit()
    # Journal is durable before feedback; DB outage keeps the event for replay.
    for values in feedback:
        journal.submit(session, values)
    logger.info("扫描对账完成 | scan_id=%s | stable_locations=%s | waiting_stable=%s | positive_feedback=%s | negative_feedback=%s | lineage_watermark=%s | lineage_backlog=%s | elapsed=%.1fs",
        scan.id, len(seen_locations), unstable_files, sum(item["label"] == 1 for item in feedback), sum(item["label"] == 0 for item in feedback), scan.lineage_watermark, bool(backlog), time.monotonic() - started)
    return scan
