from dataclasses import dataclass

from .probe import VideoInfo


MAX_VIDEO_BIT_RATE = 5_000_000
MAX_VALIDATED_VIDEO_BIT_RATE = 5_500_000
TARGET_VIDEO_BIT_RATE = 4_500_000
MAX_FPS = 30.0


@dataclass(frozen=True)
class CompressionPlan:
    transcode: bool
    width: int
    height: int
    cap_fps: bool
    reasons: tuple[str, ...]


def _even(value):
    return max(2, int(value) // 2 * 2)


def target_dimensions(width, height):
    max_width, max_height = ((1080, 1920) if height > width else (1920, 1080))
    scale = min(1.0, max_width / width, max_height / height)
    return _even(width * scale), _even(height * scale)


def make_plan(info: VideoInfo):
    display_width, display_height = info.display_width, info.display_height
    width, height = target_dimensions(display_width, display_height)
    reasons = []
    if info.bit_rate is None or info.bit_rate > MAX_VIDEO_BIT_RATE:
        reasons.append("video bitrate is unknown or above 5 Mbps")
    if (width, height) != (display_width, display_height):
        reasons.append("display dimensions exceed the permitted bounds")
    if info.codec_name not in {"hevc", "h265"}:
        reasons.append("video codec is not HEVC")
    if info.fps <= 0:
        reasons.append("frame rate is unavailable")
    if info.fps > MAX_FPS + 0.01:
        reasons.append("frame rate is above 30 fps")
    return CompressionPlan(
        transcode=bool(reasons),
        width=width,
        height=height,
        cap_fps=info.fps > MAX_FPS + 0.01,
        reasons=tuple(reasons),
    )


def validate_specification(info: VideoInfo):
    expected = target_dimensions(info.display_width, info.display_height)
    errors = []
    if info.codec_name not in {"hevc", "h265"}:
        errors.append("codec is not HEVC")
    if info.bit_rate is None:
        errors.append("average video bitrate is unavailable")
    elif info.bit_rate > MAX_VALIDATED_VIDEO_BIT_RATE:
        errors.append(f"average video bitrate is {info.bit_rate}, above 5500000")
    if expected != (info.display_width, info.display_height):
        errors.append("display dimensions exceed the permitted bounds")
    if info.fps > MAX_FPS + 0.01:
        errors.append(f"frame rate is {info.fps:.3f}, above 30")
    elif info.fps <= 0:
        errors.append("frame rate is unavailable")
    return errors
