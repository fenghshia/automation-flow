"""Whole-scope reconciliation. File disappearance alone is never a label event."""

from datetime import timedelta
import logging
import time
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
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


def lineage_pending(session, settings, after_id):
    """Public events block only the group and epoch that can consume them."""
    query = select(LineageEvent.id).where(LineageEvent.id > max(after_id, settings.get("lineage_floor", 0)))
    if settings.get("grouped"):
        query = query.where(
            LineageEvent.evidence["dataset_group_id"].as_string() == settings["dataset_group_id"],
            LineageEvent.evidence["reset_epoch"].as_string() == settings["reset_epoch"],
        )
    return session.execute(query.limit(1)).first() is not None


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


def _insert_scan_location(session, **values):
    """A concurrent publication may reserve this path after our last lookup."""
    location = Location(**values)
    try:
        with session.begin_nested():
            session.add(location)
            session.flush()
        return location, True
    except IntegrityError as error:
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint != "vf_group_location_current_path_key" and not (
            session.get_bind().dialect.name == "sqlite" and
            "UNIQUE constraint failed: video_filter_location.dataset_group_id, video_filter_location.current_path_key" in str(error.orig)
        ):
            raise
        location = session.scalar(select(Location).where(
            Location.current_path_key == values["current_path_key"]
        ).with_for_update().execution_options(populate_existing=True))
        if location is None:
            raise
        return location, False


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
    if settings.get("grouped"):
        from .group_config import require_current_group
        require_current_group(settings)
        # Complete enumeration establishes scope only. Partial identity commits
        # never advance the complete watermark or provide deletion evidence.
        scan.role_results = {**scan.role_results, "_baseline_ready": True}
        session.commit()
    observations = {item.path_key: item for item in session.scalars(select(Observation).where(Observation.config_revision_id == config.id))}
    currents = {item.current_path_key: item for item in session.scalars(select(Location).where(Location.current_path_key.is_not(None)))}
    unfinished = session.scalars(select(ScanRun).where(ScanRun.config_revision_id == config.id,
        ScanRun.complete == False, ScanRun.id != scan.id,
        ScanRun.role_results["_arrivals_consumed"].as_boolean().is_not(True))).all() if settings.get("grouped") else []
    arrival_ids = {identifier for pending in unfinished if not pending.role_results.get("_arrivals_consumed")
        for identifier in pending.role_results.get("_arrival_location_ids", [])}
    last_scope_check = time.monotonic()
    hash_seconds, partial_commits = 0, 0
    for role, path, stat in files:
        if settings.get("grouped") and time.monotonic() - last_scope_check >= 1:
            require_current_group(settings)
            last_scope_check = time.monotonic()
        key = path_key(path)
        observed_keys.add(key)
        observation = observations.get(key)
        if observation is None:
            observation = Observation(config_revision_id=config.id, path_key=key, stable_since=now,
                                      last_seen_at=now, baseline_entry=baseline or settings.get("notifications_uncertain", False), **stat)
            session.add(observation)
            observations[key] = observation
        elif (observation.size_bytes, observation.modified_ns, observation.file_identity) != (
            stat["size_bytes"], stat["modified_ns"], stat["file_identity"],
        ):
            observation.size_bytes, observation.modified_ns = stat["size_bytes"], stat["modified_ns"]
            observation.file_identity, observation.stable_since = stat["file_identity"], now
        observation.last_seen_at = now
        if now - observation.stable_since < timedelta(seconds=settings["stable_seconds"]):
            unstable_files += 1
            continue
        current = currents.get(key)
        try:
            if current and current.status == "present" and (current.size_bytes, current.modified_ns, current.file_identity) == (
                stat["size_bytes"], stat["modified_ns"], stat["file_identity"]):
                digest = session.get(Variant, current.variant_id).sha256
            else:
                if settings.get("grouped"):
                    # Commit verified predecessors before the next long read;
                    # refresh this input afterwards instead of trusting stale ORM state.
                    session.commit()
                    partial_commits += 1
                hash_started = time.monotonic()
                digest, _ = hash_stable(path, stat)
                hash_seconds += time.monotonic() - hash_started
                if settings.get("grouped"):
                    require_current_group(settings)
                    if snapshot(path) != stat:
                        raise ValueError("source_version_changed")
            if settings.get("grouped"):
                # A transfer/lineage consumer may have inserted, retired or
                # replaced this path while hashing released our transaction.
                current = session.scalar(select(Location).where(
                    Location.current_path_key == key
                ).with_for_update().execution_options(populate_existing=True))
                if current is None:
                    currents.pop(key, None)
                else:
                    currents[key] = current
        except (OSError, ValueError) as error:
            if isinstance(error, FileNotFoundError):
                logger.info("扫描时视频已移走或删除，暂缓本轮对账 | scan_id=%s | role=%s", scan.id, role)
            else:
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
            current, inserted = _insert_scan_location(session, variant_id=variant.id, role=role,
                path=str(path), current_path_key=key, size_bytes=stat["size_bytes"], modified_ns=stat["modified_ns"])
            if not inserted:
                if current.variant_id != variant.id:
                    # Preserve the competing publication and pause deletion
                    # inference. The next scan will verify its file identity.
                    complete = False
                    continue
                entered = current.status == "missing" or current.role != role
            else:
                new_locations.append(current)
            currents[key] = current
        current.role, current.file_identity = role, stat["file_identity"]
        current.size_bytes, current.modified_ns = stat["size_bytes"], stat["modified_ns"]
        current.status, current.missing_since, current.last_scan_id = "present", None, scan.id
        session.flush()
        if settings.get("grouped") and current in new_locations:
            scan.role_results = {**scan.role_results,
                "_arrival_location_ids": [*scan.role_results.get("_arrival_location_ids", []), current.id]}
            arrival_ids.add(current.id)
        seen_locations.add(current.id)
        if role in ("confirmed_like", "liked", "liked_source") and entered and (not settings.get("grouped") or
                (not baseline and not observation.baseline_entry) or session.get(Asset, variant.asset_id).label is None):
            positive_ids.add(variant.asset_id)
            if settings.get("grouped"):
                # Arrival and its label share one DB transaction. A restart can
                # never preserve the location while losing its positive intent.
                # No journal is published for an asset that has not committed.
                from .feedback import apply_feedback
                apply_feedback(session, **feedback_values(session.get(Asset, variant.asset_id), 1,
                    {"reason": "entered_confirmed_like", "scan_id": scan.id}))
                require_current_group(settings)
                session.commit()
                partial_commits += 1
                logger.info("喜欢到达反馈及文件位置已原子提交 | asset_id=%s | scan_id=%s", variant.asset_id, scan.id)
        if settings.get("grouped") and len(seen_locations) % 20 == 0:
            require_current_group(settings)
            session.commit()
            partial_commits += 1
    if not complete:
        # A mid-scan source change must not activate a partial baseline.
        session.rollback()
        if not settings.get("grouped"):
            config = record_configuration(session, settings)
            scan = ScanRun(config_revision_id=config.id, role_results=role_results, complete=False, finished_at=now)
            session.add(scan)
        else:
            scan.finished_at = now
        session.commit()
        logger.warning("扫描途中变化，未提交部分已回滚 | scan_id=%s | partial_commits=%s | deletion_feedback_paused=true", scan.id, partial_commits)
        return scan
    previous_scan = session.execute(select(ScanRun).where(ScanRun.complete == True).order_by(ScanRun.created_at.desc()).limit(1)).scalar_one_or_none()
    watermark = previous_scan.lineage_watermark if previous_scan else floor
    scan.lineage_watermark = consume_lineage(session, watermark)
    if settings.get("grouped"):
        require_current_group(settings)
    backlog = lineage_pending(session, settings, scan.lineage_watermark)
    feedback = []
    for asset_id in positive_ids if not settings.get("grouped") else ():
        asset = session.get(Asset, asset_id)
        feedback.append(feedback_values(asset, 1, {"reason": "entered_confirmed_like", "scan_id": scan.id}))
    if complete:
        missing_assets = set()
        move_variants = set(session.scalars(select(Location.variant_id).where(Location.id.in_(arrival_ids),
            Location.status == "present", Location.current_path_key.in_(observed_keys)))) if arrival_ids else set()
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
            if settings.get("grouped") and location.variant_id in move_variants:
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
    for pending in unfinished:
        pending.role_results = {**pending.role_results, "_arrivals_consumed": True}
    journal = FeedbackJournal(settings["state_directory"])
    for values in feedback:
        journal.write(values)
    session.commit()
    # Journal is durable before feedback; DB outage keeps the event for replay.
    for values in feedback:
        journal.submit(session, values)
    logger.info("扫描对账完成 | scan_id=%s | stable_locations=%s | waiting_stable=%s | positive_feedback=%s | negative_feedback=%s | lineage_watermark=%s | lineage_backlog=%s | elapsed=%.1fs",
        scan.id, len(seen_locations), unstable_files, len(positive_ids), sum(item["label"] == 0 for item in feedback), scan.lineage_watermark, bool(backlog), time.monotonic() - started)
    logger.info("扫描阶段耗时 | scan_id=%s | hash_seconds=%.3f | files=%s | partial_identity_commits=%s", scan.id, hash_seconds, len(files), partial_commits)
    return scan
