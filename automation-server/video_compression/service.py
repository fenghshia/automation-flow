import os
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .policy import make_plan, validate_specification
from .probe import probe_video, verify_decodable
from .transcoder import transcode


logger = logging.getLogger(__name__)


class CompressionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompressionResult:
    destination: Path
    transcoded: bool


@dataclass(frozen=True)
class PreparedCompression:
    destination: Path
    staging: Path
    transcoded: bool


class CompressionService:
    def __init__(self, ffmpeg_bin_dir, output_dir, cache_dir=None):
        self.ffmpeg_bin_dir = Path(ffmpeg_bin_dir)
        self.output_dir = Path(output_dir)
        self.cache_dir = Path(cache_dir or Path(__file__).with_name("cache"))
        self.ffmpeg_path = self.ffmpeg_bin_dir / "ffmpeg.exe"
        self.ffprobe_path = self.ffmpeg_bin_dir / "ffprobe.exe"

    def _validate_tools_and_paths(self):
        for tool in (self.ffmpeg_path, self.ffprobe_path):
            if not tool.is_file():
                raise CompressionError(f"Required executable does not exist: {tool.name}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def validate_output(self, path):
        logger.info("开始验证视频 | file=%s", Path(path).name)
        info = probe_video(path, self.ffprobe_path)
        errors = validate_specification(info)
        if errors:
            raise CompressionError("Output validation failed: " + "; ".join(errors))
        duration = info.duration or 0
        verify_decodable(path, self.ffmpeg_path, timeout=max(120, int(duration * 2 + 60)))
        logger.info(
            "视频验证通过 | file=%s | codec=%s | size=%sx%s | fps=%.3f | bitrate=%s",
            Path(path).name,
            info.codec_name,
            info.display_width,
            info.display_height,
            info.fps,
            info.bit_rate if info.bit_rate is not None else "unknown",
        )
        return info

    @staticmethod
    def _direct_child(path, directory, description):
        directory = Path(directory).resolve()
        path = Path(path)
        parent = path.parent.resolve()
        if parent != directory:
            raise CompressionError(f"{description} is outside its configured directory")
        path = parent / path.name
        if path.is_symlink():
            raise CompressionError(f"{description} must not be a symbolic link")
        return path

    def destination_for(self, file_name):
        if not file_name or Path(file_name).name != file_name:
            raise CompressionError("Mission file name is invalid")
        return self._direct_child(
            self.output_dir / file_name,
            self.output_dir,
            "Output path",
        )

    def work_path_for(self, mission_id):
        return self._direct_child(
            self.cache_dir / f".mission-{int(mission_id)}.transcoding.mp4",
            self.cache_dir,
            "Work path",
        )

    def staging_path_for(self, mission_id, destination):
        destination = self._direct_child(
            destination,
            self.output_dir,
            "Output path",
        )
        return self._direct_child(
            destination.with_name(
                f".{destination.name}.mission-{int(mission_id)}.part"
            ),
            self.output_dir,
            "Staging path",
        )

    @staticmethod
    def _copy_without_overwrite(source, destination):
        with Path(source).open("rb") as source_stream, Path(destination).open(
            "xb"
        ) as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
        shutil.copystat(source, destination)

    def discard_work_file(self, mission_id):
        self.work_path_for(mission_id).unlink(missing_ok=True)

    def discard_staging_file(self, mission_id, destination):
        self.staging_path_for(mission_id, destination).unlink(missing_ok=True)

    def prepare(self, source, mission_id, destination):
        source = Path(source).resolve()
        if not source.is_file():
            raise CompressionError(f"Source file does not exist: {source.name}")
        if source.suffix.lower() != ".mp4":
            raise CompressionError(f"Only MP4 files are supported: {source.name}")

        self._validate_tools_and_paths()
        expected_destination = self.destination_for(source.name)
        destination = self._direct_child(
            destination,
            self.output_dir,
            "Output path",
        )
        if destination != expected_destination:
            raise CompressionError("Mission output path does not match its file name")
        if destination == source:
            raise CompressionError("Source and output directories must be different")
        if destination.exists():
            raise CompressionError(f"Output file already exists: {destination.name}")

        staging = self.staging_path_for(mission_id, destination)
        work = self.work_path_for(mission_id)
        if staging.exists():
            raise CompressionError("Mission staging file already exists")
        if work.exists():
            raise CompressionError("Mission work file already exists")

        source_info = probe_video(source, self.ffprobe_path)
        plan = make_plan(source_info)
        logger.info(
            "视频分析完成 | mission_id=%s | source=%s | codec=%s | "
            "size=%sx%s | fps=%.3f | bitrate=%s | action=%s | reasons=%s",
            mission_id,
            source.name,
            source_info.codec_name,
            source_info.display_width,
            source_info.display_height,
            source_info.fps,
            source_info.bit_rate if source_info.bit_rate is not None else "unknown",
            "transcode" if plan.transcode else "copy",
            "; ".join(plan.reasons) if plan.reasons else "already compliant",
        )
        if not plan.transcode:
            self.validate_output(source)
            logger.info(
                "源视频符合规格，开始复制到暂存区 | mission_id=%s | source=%s",
                mission_id,
                source.name,
            )
            self._copy_without_overwrite(source, staging)
            logger.info(
                "视频暂存完成 | mission_id=%s | source=%s | transcoded=false",
                mission_id,
                source.name,
            )
            return PreparedCompression(
                destination=destination,
                staging=staging,
                transcoded=False,
            )

        try:
            duration = source_info.duration or 0
            timeout = max(900, int(duration * 10 + 300))
            logger.info(
                "开始视频转码 | mission_id=%s | source=%s | target=%sx%s | cap_fps=%s | timeout=%ss",
                mission_id,
                source.name,
                plan.width,
                plan.height,
                plan.cap_fps,
                timeout,
            )
            transcode(
                self.ffmpeg_path,
                source,
                work,
                plan,
                timeout=timeout,
                duration=duration,
            )
            self.validate_output(work)
            logger.info(
                "转码成品验证通过，开始复制到暂存区 | mission_id=%s | source=%s",
                mission_id,
                source.name,
            )
            self._copy_without_overwrite(work, staging)
            logger.info(
                "视频暂存完成 | mission_id=%s | source=%s | transcoded=true",
                mission_id,
                source.name,
            )
            return PreparedCompression(
                destination=destination,
                staging=staging,
                transcoded=True,
            )
        finally:
            work.unlink(missing_ok=True)

    def publish_prepared(self, mission_id, destination):
        destination = self._direct_child(
            destination,
            self.output_dir,
            "Output path",
        )
        staging = self.staging_path_for(mission_id, destination)
        if not staging.is_file():
            raise CompressionError("Mission staging file does not exist")
        logger.info(
            "开始发布视频 | mission_id=%s | file=%s",
            mission_id,
            destination.name,
        )
        self.validate_output(staging)
        os.link(staging, destination)
        logger.info(
            "视频发布完成 | mission_id=%s | file=%s",
            mission_id,
            destination.name,
        )
        return staging

    def _publish_and_remove_source(
        self, artifact, source, destination, published_callback=None
    ):
        staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            shutil.copy2(artifact, staging)
            self.validate_output(staging)
            os.link(staging, destination)
            if published_callback is not None:
                published_callback(destination)
            source.unlink()
        finally:
            staging.unlink(missing_ok=True)

    def process(self, source, published_callback=None):
        source = Path(source).resolve()
        if not source.is_file():
            raise CompressionError(f"Source file does not exist: {source.name}")
        if source.suffix.lower() != ".mp4":
            raise CompressionError(f"Only MP4 files are supported: {source.name}")

        self._validate_tools_and_paths()
        destination = (self.output_dir / source.name).resolve()
        if destination == source:
            raise CompressionError("Source and output directories must be different")
        if destination.exists():
            raise CompressionError(f"Output file already exists: {destination.name}")

        source_info = probe_video(source, self.ffprobe_path)
        plan = make_plan(source_info)
        if not plan.transcode:
            self.validate_output(source)
            self._publish_and_remove_source(
                source, source, destination, published_callback
            )
            return CompressionResult(destination=destination, transcoded=False)

        temporary = self.cache_dir / f"{uuid.uuid4().hex}.mp4"
        try:
            duration = source_info.duration or 0
            transcode(
                self.ffmpeg_path,
                source,
                temporary,
                plan,
                timeout=max(900, int(duration * 10 + 300)),
                duration=duration,
            )
            self.validate_output(temporary)
            self._publish_and_remove_source(
                temporary, source, destination, published_callback
            )
            return CompressionResult(destination=destination, transcoded=True)
        finally:
            temporary.unlink(missing_ok=True)
