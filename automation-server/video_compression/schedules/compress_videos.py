from contextlib import contextmanager
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app import app, db, scheduler
from env import EnvConfig
from logging_config import log_exception
from ..models import CompressionMission, CompressionStatus
from ..service import CompressionError, CompressionService


SUPPORTED_SUFFIXES = {".mp4"}
VIDEO_COMPRESSION_ADVISORY_LOCK_ID = 0x564944454F434D50
logger = logging.getLogger(__name__)


@contextmanager
def video_compression_lock(engine):
    with engine.connect() as connection:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": VIDEO_COMPRESSION_ADVISORY_LOCK_ID},
            ).scalar_one()
        )
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": VIDEO_COMPRESSION_ADVISORY_LOCK_ID},
                )


def discover_source_files(source_directory):
    source_directory = Path(source_directory)
    if not source_directory.is_dir():
        raise RuntimeError("VIDEO_COMPRESSION_SOURCE_DIR does not exist or is not a directory.")
    return sorted(
        path.resolve()
        for path in source_directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def insert_mission_if_missing(session, path, stat):
    statement = (
        insert(CompressionMission)
        .values(
            source_path=str(path),
            file_name=path.name,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            stable_checks=0,
            status=CompressionStatus.WAITING_STABLE,
        )
        .on_conflict_do_nothing(index_elements=[CompressionMission.source_path])
        .returning(CompressionMission.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None


def refresh_missions(source_directory):
    for path in discover_source_files(source_directory):
        stat = path.stat()
        source_path = str(path)
        if insert_mission_if_missing(db.session, path, stat):
            continue
        mission = CompressionMission.query.filter_by(source_path=source_path).first()
        if mission is None:
            continue
        if mission.status == CompressionStatus.COMPLETED:
            mission.file_name = path.name
            mission.size_bytes = stat.st_size
            mission.modified_ns = stat.st_mtime_ns
            mission.stable_checks = 0
            mission.status = CompressionStatus.WAITING_STABLE
            mission.output_path = None
            mission.error_message = None
            continue
        if mission.status not in {
            CompressionStatus.WAITING_STABLE,
            CompressionStatus.READY,
            CompressionStatus.FAILED,
        }:
            continue
        if (mission.size_bytes, mission.modified_ns) == (stat.st_size, stat.st_mtime_ns):
            if mission.status == CompressionStatus.WAITING_STABLE:
                mission.stable_checks += 1
                if mission.stable_checks >= 1:
                    mission.status = CompressionStatus.READY
        else:
            mission.size_bytes = stat.st_size
            mission.modified_ns = stat.st_mtime_ns
            mission.stable_checks = 0
            mission.status = CompressionStatus.WAITING_STABLE
            mission.error_message = None
    db.session.commit()


def claim_next_mission(service):
    candidate = (
        CompressionMission.query.filter_by(status=CompressionStatus.READY)
        .order_by(CompressionMission.id)
        .first()
    )
    if candidate is None:
        return None
    try:
        destination = service.destination_for(candidate.file_name)
    except (OSError, RuntimeError, ValueError) as error:
        fail_mission(candidate, error)
        return None
    claimed = (
        db.session.query(CompressionMission)
        .filter(
            CompressionMission.id == candidate.id,
            CompressionMission.status == CompressionStatus.READY,
        )
        .update(
            {
                CompressionMission.status: CompressionStatus.PROCESSING,
                CompressionMission.attempts: CompressionMission.attempts + 1,
                CompressionMission.output_path: str(destination),
                CompressionMission.error_message: None,
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    return db.session.get(CompressionMission, candidate.id) if claimed == 1 else None


def mission_paths(mission, service, source_directory):
    source_directory = Path(source_directory).resolve()
    source = Path(mission.source_path).resolve()
    if source.parent != source_directory or source.name != mission.file_name:
        raise CompressionError("Mission source path does not match its configured directory")

    expected_output = service.destination_for(mission.file_name)
    if mission.output_path:
        output = Path(mission.output_path).resolve()
        if output != expected_output:
            raise CompressionError(
                "Mission output path does not match its configured directory"
            )
    else:
        output = expected_output
    staging = service.staging_path_for(mission.id, output)
    work = service.work_path_for(mission.id)
    return source, output, staging, work


def fail_mission(mission, error, *, log_error=True):
    if log_error:
        log_exception(
            logger,
            "视频压缩失败 | mission_id=%s",
            error,
            mission.id,
        )
    if isinstance(error, OSError):
        detail = error.strerror or error.__class__.__name__
        message = f"Filesystem operation failed for {mission.file_name}: {detail}"
    else:
        message = str(error)
        for value in (mission.source_path, mission.output_path):
            if not value:
                continue
            path = Path(value)
            message = message.replace(str(path), path.name)
            message = message.replace(str(path.parent), "<configured-directory>")
    mission.status = CompressionStatus.FAILED
    mission.error_message = message[-2000:]
    try:
        db.session.commit()
    except Exception as commit_error:
        log_exception(
            logger,
            "视频失败状态保存失败 | mission_id=%s",
            commit_error,
            mission.id,
        )
        try:
            db.session.rollback()
        except Exception as rollback_error:
            log_exception(
                logger,
                "视频失败状态回滚失败 | mission_id=%s",
                rollback_error,
                mission.id,
            )


def files_are_same(first, second):
    try:
        return first.samefile(second)
    except OSError:
        # Failure to compare is treated as "not owned by this mission" so the
        # caller takes its tested, non-destructive conflict path.
        return False


def finish_cleanup_pending(service, source_directory):
    mission = (
        CompressionMission.query.filter_by(status=CompressionStatus.CLEANUP_PENDING)
        .order_by(CompressionMission.id)
        .first()
    )
    if mission is None:
        return False
    try:
        source, output, staging, _ = mission_paths(
            mission, service, source_directory
        )
        if not output.is_file():
            raise CompressionError(
                "Published output is missing; source was preserved."
            )
        if staging.exists() and not files_are_same(staging, output):
            raise CompressionError(
                "Mission staging file does not match the published output"
            )
        service.validate_output(output)
        service.discard_staging_file(mission.id, output)
        service.discard_work_file(mission.id)
        source.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError) as error:
        fail_mission(mission, error)
        return True

    mission.status = CompressionStatus.COMPLETED
    mission.output_path = str(output)
    mission.error_message = None
    db.session.commit()
    return True


def recover_validating_mission(service, source_directory):
    mission = (
        CompressionMission.query.filter_by(status=CompressionStatus.VALIDATING)
        .order_by(CompressionMission.id)
        .first()
    )
    if mission is None:
        return False

    try:
        source, output, staging, _ = mission_paths(
            mission, service, source_directory
        )
        source_exists = source.is_file()
        output_exists = output.is_file()
        staging_exists = staging.is_file()

        if not source_exists:
            if not output_exists:
                raise CompressionError(
                    "Source and published output are both missing"
                )
            if staging_exists and not files_are_same(staging, output):
                raise CompressionError(
                    "Mission staging file does not match the published output"
                )
            service.validate_output(output)
            service.discard_staging_file(mission.id, output)
            service.discard_work_file(mission.id)
            mission.status = CompressionStatus.COMPLETED
            mission.output_path = str(output)
            mission.error_message = None
            db.session.commit()
            return True

        if not staging_exists:
            if output_exists:
                raise CompressionError(
                    "Published output exists but its ownership cannot be verified"
                )
            service.discard_work_file(mission.id)
            mission.status = CompressionStatus.READY
            mission.output_path = str(output)
            mission.error_message = "Recovered interrupted validation; retry queued."
            db.session.commit()
            return True

        if output_exists:
            if not files_are_same(staging, output):
                raise CompressionError(
                    "Published output conflicts with the mission staging file"
                )
            service.validate_output(output)
        else:
            service.publish_prepared(mission.id, output)

        mission.status = CompressionStatus.CLEANUP_PENDING
        mission.output_path = str(output)
        mission.error_message = None
        db.session.commit()
        service.discard_staging_file(mission.id, output)
        service.discard_work_file(mission.id)
    except (OSError, RuntimeError, ValueError) as error:
        fail_mission(mission, error)
    return True


def recover_processing_mission(service, source_directory):
    mission = (
        CompressionMission.query.filter_by(status=CompressionStatus.PROCESSING)
        .order_by(CompressionMission.id)
        .first()
    )
    if mission is None:
        return False

    try:
        source, output, staging, _ = mission_paths(
            mission, service, source_directory
        )
        source_exists = source.is_file()
        output_exists = output.is_file()
        staging_exists = staging.is_file()

        if not source_exists:
            if not output_exists:
                raise CompressionError(
                    "Source and published output are both missing"
                )
            if staging_exists and not files_are_same(staging, output):
                raise CompressionError(
                    "Mission staging file does not match the published output"
                )
            service.validate_output(output)
            service.discard_staging_file(mission.id, output)
            service.discard_work_file(mission.id)
            mission.status = CompressionStatus.COMPLETED
            mission.output_path = str(output)
            mission.error_message = None
            db.session.commit()
            return True

        if output_exists:
            if not staging_exists or not files_are_same(staging, output):
                raise CompressionError(
                    "Published output exists but its ownership cannot be verified"
                )
            service.validate_output(output)
            mission.status = CompressionStatus.CLEANUP_PENDING
            mission.output_path = str(output)
            mission.error_message = None
            db.session.commit()
            service.discard_staging_file(mission.id, output)
            service.discard_work_file(mission.id)
            return True

        if staging_exists:
            try:
                service.validate_output(staging)
            except RuntimeError as validation_error:
                log_exception(
                    logger,
                    "恢复视频任务时暂存文件校验失败，将重新排队 | mission_id=%s",
                    validation_error,
                    mission.id,
                )
                service.discard_staging_file(mission.id, output)
                service.discard_work_file(mission.id)
                mission.status = CompressionStatus.READY
                mission.output_path = str(output)
                mission.error_message = (
                    "Recovered interrupted processing; retry queued."
                )
                db.session.commit()
                return True

            service.discard_work_file(mission.id)
            mission.status = CompressionStatus.VALIDATING
            mission.output_path = str(output)
            mission.error_message = None
            db.session.commit()
            return True

        service.discard_work_file(mission.id)
        mission.status = CompressionStatus.READY
        mission.output_path = str(output)
        mission.error_message = "Recovered interrupted processing; retry queued."
        db.session.commit()
    except (OSError, RuntimeError, ValueError) as error:
        fail_mission(mission, error)
    return True


def process_one_mission():
    source_dir = EnvConfig.video_compression_source_directory()
    output_dir = EnvConfig.video_compression_output_directory()
    if source_dir == output_dir:
        raise RuntimeError("Video source and output directories must be different.")
    service = CompressionService(
        EnvConfig.video_compression_ffmpeg_bin_directory(), output_dir
    )
    if finish_cleanup_pending(service, source_dir):
        return None
    if recover_validating_mission(service, source_dir):
        return None
    if recover_processing_mission(service, source_dir):
        return None
    refresh_missions(source_dir)
    mission = claim_next_mission(service)
    if mission is None:
        return None

    mission_id = mission.id
    output = None
    try:
        source, output, _, _ = mission_paths(mission, service, source_dir)
        prepared = service.prepare(source, mission.id, output)
    except Exception as error:
        log_exception(
            logger,
            "视频处理阶段失败 | mission_id=%s",
            error,
            mission_id,
        )
        try:
            db.session.rollback()
        except Exception as rollback_error:
            log_exception(
                logger,
                "视频处理失败后回滚失败 | mission_id=%s",
                rollback_error,
                mission_id,
            )
            return None
        mission = db.session.get(CompressionMission, mission_id)
        if mission is None:
            logger.error("视频失败任务不存在 | mission_id=%s", mission_id)
            return None
        if output is not None:
            try:
                service.discard_staging_file(mission.id, output)
            except OSError as cleanup_error:
                log_exception(
                    logger,
                    "视频暂存文件清理失败 | mission_id=%s",
                    cleanup_error,
                    mission_id,
                )
        try:
            service.discard_work_file(mission.id)
        except OSError as cleanup_error:
            log_exception(
                logger,
                "视频工作文件清理失败 | mission_id=%s",
                cleanup_error,
                mission_id,
            )
        fail_mission(mission, error, log_error=False)
        return None

    mission.status = CompressionStatus.VALIDATING
    mission.output_path = str(prepared.destination)
    mission.error_message = None
    db.session.commit()

    try:
        service.publish_prepared(mission.id, prepared.destination)
    except (OSError, RuntimeError, ValueError) as error:
        fail_mission(mission, error)
        return None

    mission.status = CompressionStatus.CLEANUP_PENDING
    mission.output_path = str(prepared.destination)
    mission.error_message = None
    db.session.commit()

    try:
        service.discard_staging_file(mission.id, prepared.destination)
        service.discard_work_file(mission.id)
        source.unlink(missing_ok=True)
    except OSError as error:
        fail_mission(mission, error)
        return None

    mission.status = CompressionStatus.COMPLETED
    mission.error_message = None
    db.session.commit()
    return prepared


@scheduler.task(
    "interval",
    id="video_compression_process_one",
    seconds=30,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=120,
)
def compress_videos():
    try:
        with app.app_context():
            with video_compression_lock(db.engine) as acquired:
                if acquired:
                    process_one_mission()
    except Exception as error:
        log_exception(logger, "视频定时任务未处理异常", error)
        try:
            db.session.rollback()
        except Exception as rollback_error:
            log_exception(logger, "视频定时任务回滚失败", rollback_error)
        return None
