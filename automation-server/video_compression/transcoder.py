import subprocess
from pathlib import Path

from .policy import TARGET_VIDEO_BIT_RATE


class TranscodeError(RuntimeError):
    pass


def build_ffmpeg_command(ffmpeg_path, source, destination, plan):
    filters = []
    if plan.cap_fps:
        filters.append("fps=30")
    filters.append(f"scale={plan.width}:{plan.height}:flags=lanczos")

    return [
        str(ffmpeg_path),
        "-hide_banner", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
        "-metadata:s:v:0", "rotate=0",
        "-vf", ",".join(filters),
        "-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq",
        "-rc", "vbr", "-b:v", str(TARGET_VIDEO_BIT_RATE),
        "-maxrate", "5000000", "-bufsize", "10000000", "-cq", "24",
        "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1",
        "-multipass", "fullres", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-f", "mp4", str(destination),
    ]


def transcode(ffmpeg_path, source, destination, plan, timeout):
    command = build_ffmpeg_command(ffmpeg_path, source, destination, plan)
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
        raise TranscodeError(f"FFmpeg failed while processing {Path(source).name}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip()[-2000:]
        raise TranscodeError(f"FFmpeg failed while processing {Path(source).name}: {detail}")
