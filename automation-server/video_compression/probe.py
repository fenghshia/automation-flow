import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class MediaProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    codec_name: str
    width: int
    height: int
    fps: float
    bit_rate: int | None
    duration: float | None
    rotation: int = 0

    @property
    def display_width(self):
        return self.height if self.rotation % 180 else self.width

    @property
    def display_height(self):
        return self.width if self.rotation % 180 else self.height


def _number(value, converter, default=None):
    if value in (None, "", "N/A"):
        return default
    try:
        return converter(value)
    except (TypeError, ValueError, ZeroDivisionError):
        # Optional ffprobe fields use invalid/missing values as a normal signal.
        return default


def _rotation(stream):
    tags = stream.get("tags", {})
    value = _number(tags.get("rotate"), int)
    if value is None:
        for side_data in stream.get("side_data_list", []):
            value = _number(side_data.get("rotation"), int)
            if value is not None:
                break
    return (value or 0) % 360


def probe_video(path, ffprobe_path, timeout=60):
    command = [
        str(ffprobe_path),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,bit_rate:stream_tags=rotate:stream_side_data=rotation:format=duration,bit_rate",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaProbeError(f"ffprobe could not inspect {Path(path).name}: {error}") from error

    if result.returncode != 0:
        detail = result.stderr.strip()[-1000:]
        raise MediaProbeError(f"ffprobe failed for {Path(path).name}: {detail}")

    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise MediaProbeError(f"ffprobe returned no usable video stream for {Path(path).name}") from error

    fps_fraction = _number(stream.get("avg_frame_rate"), Fraction, Fraction(0, 1))
    stream_rate = _number(stream.get("bit_rate"), int)
    format_data = payload.get("format", {})
    try:
        return VideoInfo(
            codec_name=(stream.get("codec_name") or "").lower(),
            width=int(stream["width"]),
            height=int(stream["height"]),
            fps=float(fps_fraction),
            bit_rate=stream_rate or _number(format_data.get("bit_rate"), int),
            duration=_number(format_data.get("duration"), float),
            rotation=_rotation(stream),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MediaProbeError(
            f"ffprobe returned incomplete video information for {Path(path).name}"
        ) from error


def verify_decodable(path, ffmpeg_path, timeout):
    command = [
        str(ffmpeg_path), "-v", "error", "-xerror", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaProbeError(f"FFmpeg decode validation failed for {Path(path).name}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip()[-1000:]
        raise MediaProbeError(f"FFmpeg could not decode {Path(path).name}: {detail}")
