from pathlib import Path

from app import app, db, scheduler
from env import EnvConfig
from ..models import CompressionMission, CompressionStatus
from ..service import CompressionService


SUPPORTED_SUFFIXES = {".mp4"}


def discover_source_files(source_directory):
    source_directory = Path(source_directory)
    if not source_directory.is_dir():
        raise RuntimeError("VIDEO_COMPRESSION_SOURCE_DIR does not exist or is not a directory.")
    return sorted(
        path.resolve()
        for path in source_directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def refresh_missions(source_directory):
    for path in discover_source_files(source_directory):
        stat = path.stat()
        source_path = str(path)
        mission = CompressionMission.query.filter_by(source_path=source_path).first()
        if mission is None:
            db.session.add(
                CompressionMission(
                    source_path=source_path,
                    file_name=path.name,
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    stable_checks=0,
                    status=CompressionStatus.WAITING_STABLE,
                )
            )
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


def claim_next_mission():
    candidate = (
        CompressionMission.query.filter_by(status=CompressionStatus.READY)
        .order_by(CompressionMission.id)
        .first()
    )
    if candidate is None:
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
                CompressionMission.error_message: None,
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    return db.session.get(CompressionMission, candidate.id) if claimed == 1 else None


def finish_cleanup_pending():
    mission = (
        CompressionMission.query.filter_by(status=CompressionStatus.CLEANUP_PENDING)
        .order_by(CompressionMission.id)
        .first()
    )
    if mission is None:
        return False
    output = Path(mission.output_path) if mission.output_path else None
    if output is None or not output.is_file():
        mission.status = CompressionStatus.FAILED
        mission.error_message = "Published output is missing; source was preserved."
        db.session.commit()
        return True
    service = CompressionService(
        EnvConfig.video_compression_ffmpeg_bin_directory(), output.parent
    )
    service.validate_output(output)
    Path(mission.source_path).unlink(missing_ok=True)
    mission.status = CompressionStatus.COMPLETED
    mission.error_message = None
    db.session.commit()
    return True


def process_one_mission():
    source_dir = EnvConfig.video_compression_source_directory()
    output_dir = EnvConfig.video_compression_output_directory()
    if source_dir == output_dir:
        raise RuntimeError("Video source and output directories must be different.")
    if finish_cleanup_pending():
        return None
    refresh_missions(source_dir)
    mission = claim_next_mission()
    if mission is None:
        return None

    service = CompressionService(
        EnvConfig.video_compression_ffmpeg_bin_directory(), output_dir
    )
    mission_id = mission.id
    try:
        def record_published(destination):
            mission.status = CompressionStatus.CLEANUP_PENDING
            mission.output_path = str(destination)
            db.session.commit()

        result = service.process(mission.source_path, record_published)
        mission.status = CompressionStatus.COMPLETED
        mission.output_path = str(result.destination)
        mission.error_message = None
        db.session.commit()
        return result
    except Exception as error:
        db.session.rollback()
        mission = db.session.get(CompressionMission, mission_id)
        if mission.status != CompressionStatus.CLEANUP_PENDING:
            mission.status = CompressionStatus.FAILED
            mission.error_message = str(error)[-2000:]
        db.session.commit()
        print(f"视频压缩失败: {mission.file_name}: {mission.error_message}")
        return None


@scheduler.task(
    "interval",
    id="video_compression_process_one",
    seconds=30,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=120,
)
def compress_videos():
    with app.app_context():
        process_one_mission()
