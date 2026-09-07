from contextlib import contextmanager
from datetime import datetime
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app import app, db, scheduler
from env import EnvConfig
from logging_config import log_exception
from ..manifest import (
    ContentManifest,
    SourceSnapshot,
    UnsafeSourceError,
    source_snapshot,
)
from ..models import ImageCompressionMission, ImageCompressionStatus
from ..naming import collision_key, output_name
from ..policy import is_archive_name
from ..service import ImageCompressionService


IMAGE_COMPRESSION_ADVISORY_LOCK_ID = 0x494D47434D505245
STABILITY_INTERVAL_SECONDS = 30
logger = logging.getLogger(__name__)


@contextmanager
def image_compression_lock(engine):
    with engine.connect() as connection:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": IMAGE_COMPRESSION_ADVISORY_LOCK_ID},
            ).scalar_one()
        )
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": IMAGE_COMPRESSION_ADVISORY_LOCK_ID},
                )


def build_service():
    return ImageCompressionService(
        EnvConfig.image_compression_source_directory(),
        EnvConfig.image_compression_output_directory(),
        EnvConfig.image_compression_7zip_bin_directory(),
    )


def _source_kind(path):
    if path.is_dir():
        return "directory"
    return "archive" if is_archive_name(path.name) else "file"


def discover_source_items(source_directory):
    source_directory = Path(source_directory)
    if not source_directory.is_dir():
        raise RuntimeError(
            "IMAGE_COMPRESSION_SOURCE_DIR does not exist or is not a directory"
        )
    return sorted(
        (
            path.absolute()
            for path in source_directory.iterdir()
            if not path.name.startswith(ImageCompressionService.QUARANTINE_PREFIX)
        ),
        key=lambda path: (collision_key(str(path)), str(path)),
    )


def _snapshot_values(snapshot):
    return {
        "source_size_bytes": snapshot.size_bytes,
        "source_file_count": snapshot.file_count,
        "source_modified_ns": snapshot.modified_ns,
        "source_manifest_sha256": snapshot.digest,
    }


def register_next_source(source_directory, output_directory=None):
    for path in discover_source_items(source_directory):
        source_path = str(path)
        if ImageCompressionMission.query.filter_by(source_path=source_path).first():
            continue
        kind = _source_kind(path)
        destination_key = output_name(path.name, kind)
        try:
            snapshot = source_snapshot(path)
            initial_status = ImageCompressionStatus.WAITING_STABLE
            initial_error = None
        except UnsafeSourceError as error:
            log_exception(
                logger,
                "图片来源安全检查失败 | source=%s",
                error,
                path.name,
            )
            snapshot = SourceSnapshot(0, 0, None, "0" * 64)
            initial_status = ImageCompressionStatus.FAILED
            initial_error = str(error)[-2000:]
        normalized_destination = collision_key(destination_key)
        destination_exists = bool(
            output_directory
            and (Path(output_directory) / destination_key).exists()
        )
        conflicting = ImageCompressionMission.query.filter(
            ImageCompressionMission.destination_key_normalized
            == normalized_destination,
            ImageCompressionMission.source_path != source_path,
            ImageCompressionMission.status != ImageCompressionStatus.FAILED,
        ).first()
        values = {
            "source_path": source_path,
            "source_kind": kind,
            "source_name": path.name,
            "destination_key": destination_key,
            "destination_key_normalized": normalized_destination,
            "stable_checks": 0,
            "status": (
                ImageCompressionStatus.FAILED
                if conflicting is not None or destination_exists
                else initial_status
            ),
            "error_message": (
                "Another mission maps to the same output name"
                if conflicting is not None
                else (
                    "Output already exists"
                    if destination_exists
                    else initial_error
                )
            ),
            "last_checked_at": datetime.utcnow(),
            **_snapshot_values(snapshot),
        }
        statement = (
            insert(ImageCompressionMission)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ImageCompressionMission.source_path])
        )
        db.session.execute(statement)
        db.session.commit()
        return True
    return False


def _snapshot_matches(mission, snapshot):
    return (
        mission.source_size_bytes == snapshot.size_bytes
        and mission.source_file_count == snapshot.file_count
        and mission.source_modified_ns == snapshot.modified_ns
        and mission.source_manifest_sha256 == snapshot.digest
    )


def refresh_waiting_mission(now=None):
    now = now or datetime.utcnow()
    mission = (
        ImageCompressionMission.query.filter_by(
            status=ImageCompressionStatus.WAITING_STABLE
        )
        .order_by(ImageCompressionMission.last_checked_at, ImageCompressionMission.id)
        .first()
    )
    if mission is None:
        return None
    if (now - mission.last_checked_at).total_seconds() < STABILITY_INTERVAL_SECONDS:
        return mission
    path = Path(mission.source_path)
    if not path.exists():
        mission.status = ImageCompressionStatus.FAILED
        mission.error_message = "Source disappeared while waiting for stability"
        db.session.commit()
        return mission
    try:
        snapshot = source_snapshot(path)
    except UnsafeSourceError as error:
        log_exception(
            logger,
            "等待中的图片来源安全检查失败 | mission_id=%s | source=%s",
            error,
            mission.id,
            mission.source_name,
        )
        mission.status = ImageCompressionStatus.FAILED
        mission.error_message = str(error)[-2000:]
        db.session.commit()
        return mission
    if _snapshot_matches(mission, snapshot):
        mission.stable_checks += 1
        mission.status = ImageCompressionStatus.READY
    else:
        for key, value in _snapshot_values(snapshot).items():
            setattr(mission, key, value)
        mission.stable_checks = 0
        mission.error_message = None
    mission.last_checked_at = now
    db.session.commit()
    return mission


def claim_ready_mission():
    candidate = (
        ImageCompressionMission.query.filter_by(status=ImageCompressionStatus.READY)
        .order_by(ImageCompressionMission.id)
        .first()
    )
    if candidate is None:
        return None
    claimed = (
        db.session.query(ImageCompressionMission)
        .filter(
            ImageCompressionMission.id == candidate.id,
            ImageCompressionMission.status == ImageCompressionStatus.READY,
        )
        .update(
            {
                ImageCompressionMission.status: ImageCompressionStatus.MOVING,
                ImageCompressionMission.attempts: ImageCompressionMission.attempts + 1,
                ImageCompressionMission.processing_started_at: datetime.utcnow(),
                ImageCompressionMission.error_message: None,
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    return db.session.get(ImageCompressionMission, candidate.id) if claimed == 1 else None


def _expected_manifest(mission):
    if not mission.output_manifest_sha256:
        raise RuntimeError("Mission has no output manifest for recovery")
    return ContentManifest(
        mission.output_manifest_sha256,
        int(mission.output_file_count),
        int(mission.output_size_bytes),
    )


def _complete_cleanup(service, mission):
    expected = _expected_manifest(mission)
    service.validate_path(mission.output_path, expected)
    service.cleanup_completed(mission.id)
    mission.status = ImageCompressionStatus.COMPLETED
    mission.error_message = None
    db.session.commit()


def _recover_publishing(service, mission):
    expected = _expected_manifest(mission)
    plan = service.recover_publish_plan(
        mission.id,
        mission.output_path,
        mission.source_kind in {"directory", "archive"},
        expected,
    )
    service.publish(plan)
    mission.status = ImageCompressionStatus.CLEANUP_PENDING
    mission.error_message = None
    db.session.commit()
    _complete_cleanup(service, mission)


def process_mission(service, mission):
    mission_id = mission.id
    source_name = mission.source_name
    try:
        if mission.status == ImageCompressionStatus.MOVING:
            service.ensure_ingested(mission.source_path, mission.id)
            mission.status = ImageCompressionStatus.PROCESSING
            mission.error_message = None
            db.session.commit()

        if mission.status == ImageCompressionStatus.PROCESSING:
            service.ensure_ingested(mission.source_path, mission.id)
            prepared = service.prepare_batch(
                mission.id,
                mission.source_kind,
                mission.source_name,
                mission.destination_key,
            )
            publish_plan = service.stage_for_publish(mission.id, prepared)
            mission.output_path = str(publish_plan.destination)
            mission.output_manifest_sha256 = publish_plan.manifest.digest
            mission.output_file_count = publish_plan.manifest.file_count
            mission.output_size_bytes = publish_plan.manifest.size_bytes
            mission.status = ImageCompressionStatus.PUBLISHING
            db.session.commit()
            service.publish(publish_plan)
            mission.status = ImageCompressionStatus.CLEANUP_PENDING
            db.session.commit()
            _complete_cleanup(service, mission)
            return publish_plan.destination

        if mission.status == ImageCompressionStatus.PUBLISHING:
            _recover_publishing(service, mission)
            return Path(mission.output_path)

        if mission.status == ImageCompressionStatus.CLEANUP_PENDING:
            _complete_cleanup(service, mission)
            return Path(mission.output_path)
        return None
    except Exception as error:
        log_exception(
            logger,
            "图片流水线失败 | mission_id=%s | source=%s",
            error,
            mission_id,
            source_name,
        )
        try:
            db.session.rollback()
        except Exception as rollback_error:
            log_exception(
                logger,
                "图片任务回滚失败 | mission_id=%s",
                rollback_error,
                mission_id,
            )
            return None
        mission = db.session.get(ImageCompressionMission, mission_id)
        if mission is None:
            logger.error("图片失败任务不存在 | mission_id=%s", mission_id)
            return None
        if (
            mission.status != ImageCompressionStatus.CLEANUP_PENDING
            or not isinstance(error, OSError)
        ):
            mission.status = ImageCompressionStatus.FAILED
        if mission.status == ImageCompressionStatus.FAILED:
            try:
                service.discard_publish_staging(mission.id)
            except Exception as cleanup_error:
                log_exception(
                    logger,
                    "图片发布暂存清理失败 | mission_id=%s",
                    cleanup_error,
                    mission_id,
                )
        mission.error_message = str(error)[-2000:]
        try:
            db.session.commit()
        except Exception as commit_error:
            log_exception(
                logger,
                "图片失败状态保存失败 | mission_id=%s",
                commit_error,
                mission_id,
            )
            try:
                db.session.rollback()
            except Exception as rollback_error:
                log_exception(
                    logger,
                    "图片失败状态二次回滚失败 | mission_id=%s",
                    rollback_error,
                    mission_id,
                )
        return None


def process_one_mission():
    service = build_service()
    active = (
        ImageCompressionMission.query.filter(
            ImageCompressionMission.status.in_(
                {
                    ImageCompressionStatus.MOVING,
                    ImageCompressionStatus.PROCESSING,
                    ImageCompressionStatus.PUBLISHING,
                    ImageCompressionStatus.CLEANUP_PENDING,
                }
            )
        )
        .order_by(ImageCompressionMission.updated_at, ImageCompressionMission.id)
        .first()
    )
    if active is not None:
        return process_mission(service, active)

    waiting = refresh_waiting_mission()
    if waiting is not None:
        if waiting.status == ImageCompressionStatus.READY:
            claimed = claim_ready_mission()
            return process_mission(service, claimed) if claimed else None
        return None

    ready = claim_ready_mission()
    if ready is not None:
        return process_mission(service, ready)
    register_next_source(service.source_directory, service.output_directory)
    return None


@scheduler.task(
    "interval",
    id="image_compression_process_one",
    seconds=30,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=120,
)
def process_images():
    try:
        with app.app_context():
            with image_compression_lock(db.engine) as acquired:
                if acquired:
                    process_one_mission()
    except Exception as error:
        log_exception(logger, "图片定时任务未处理异常", error)
        try:
            db.session.rollback()
        except Exception as rollback_error:
            log_exception(logger, "图片定时任务回滚失败", rollback_error)
        return None
