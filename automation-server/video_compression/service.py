import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .policy import make_plan, validate_specification
from .probe import probe_video, verify_decodable
from .transcoder import transcode


class CompressionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompressionResult:
    destination: Path
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
        info = probe_video(path, self.ffprobe_path)
        errors = validate_specification(info)
        if errors:
            raise CompressionError("Output validation failed: " + "; ".join(errors))
        duration = info.duration or 0
        verify_decodable(path, self.ffmpeg_path, timeout=max(120, int(duration * 2 + 60)))
        return info

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
            )
            self.validate_output(temporary)
            self._publish_and_remove_source(
                temporary, source, destination, published_callback
            )
            return CompressionResult(destination=destination, transcoded=True)
        finally:
            temporary.unlink(missing_ok=True)
