import logging
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .policy import TARGET_VIDEO_BIT_RATE


class TranscodeError(RuntimeError):
    pass


logger = logging.getLogger(__name__)
PROGRESS_PERCENT_STEP = 10
PROGRESS_LOG_INTERVAL_SECONDS = 60


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


def _read_progress(stream, messages):
    try:
        for line in stream:
            messages.put(line.rstrip("\r\n"))
    finally:
        messages.put(None)


def _timestamp_seconds(value):
    try:
        hours, minutes, seconds = value.split(":", 2)
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (AttributeError, TypeError, ValueError):
        return None


def _log_progress(source, media_seconds, duration, state):
    now = time.monotonic()
    if media_seconds is None:
        return state

    next_percent, last_logged_at = state
    if duration and duration > 0:
        percent = min(99, max(0, int(media_seconds / duration * 100)))
        if percent < next_percent and now - last_logged_at < PROGRESS_LOG_INTERVAL_SECONDS:
            return state
        logger.info(
            "视频转码进度 | source=%s | progress=%s%% | media_time=%.1fs/%.1fs",
            Path(source).name,
            percent,
            media_seconds,
            duration,
        )
        if percent >= next_percent:
            next_percent = (
                percent // PROGRESS_PERCENT_STEP + 1
            ) * PROGRESS_PERCENT_STEP
        return next_percent, now

    if now - last_logged_at >= PROGRESS_LOG_INTERVAL_SECONDS:
        logger.info(
            "视频转码进度 | source=%s | media_time=%.1fs",
            Path(source).name,
            media_seconds,
        )
        return next_percent, now
    return state


def transcode(ffmpeg_path, source, destination, plan, timeout, duration=None):
    command = build_ffmpeg_command(ffmpeg_path, source, destination, plan)
    started_at = time.monotonic()
    messages = queue.Queue()
    process = None
    with tempfile.TemporaryFile(
        mode="w+t", encoding="utf-8", errors="replace"
    ) as stderr_stream:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_stream,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            reader = threading.Thread(
                target=_read_progress,
                args=(process.stdout, messages),
                name="video-ffmpeg-progress",
                daemon=True,
            )
            reader.start()
            deadline = started_at + timeout
            progress_state = (PROGRESS_PERCENT_STEP, started_at)
            media_seconds = None

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    line = messages.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if line is None:
                    break
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                if key == "out_time":
                    media_seconds = _timestamp_seconds(value)
                elif key == "progress":
                    progress_state = _log_progress(
                        source, media_seconds, duration, progress_state
                    )

            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired) as error:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            raise TranscodeError(
                f"FFmpeg failed while processing {Path(source).name}: {error}"
            ) from error
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()

        if return_code != 0:
            stderr_stream.seek(0)
            detail = stderr_stream.read().strip()[-2000:]
            raise TranscodeError(
                f"FFmpeg failed while processing {Path(source).name}: {detail}"
            )

    logger.info(
        "视频转码完成 | source=%s | elapsed=%.1fs",
        Path(source).name,
        time.monotonic() - started_at,
    )
