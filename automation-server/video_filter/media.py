"""Bounded local decoding; no screenshots, audio clips or transcripts persist."""

import json
import logging
import math
import os
import subprocess
from pathlib import Path

import numpy as np


logger = logging.getLogger(__name__)


def run_media_tool(arguments, timeout):
    try:
        return subprocess.run(arguments, capture_output=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
        logger.error("媒体工具执行失败 | returncode=%s | stderr=%s", error.returncode, detail.strip())
        raise


class MediaDecoder:
    def __init__(self, ffmpeg_directory, timeout=120, threads=1, scope_guard=None):
        suffix = ".exe" if os.name == "nt" else ""
        self.ffmpeg = Path(ffmpeg_directory) / ("ffmpeg" + suffix)
        self.ffprobe = Path(ffmpeg_directory) / ("ffprobe" + suffix)
        self.timeout = timeout
        self.threads = threads
        self.scope_guard = scope_guard

    def probe(self, path):
        if self.scope_guard:
            self.scope_guard(path)
        result = run_media_tool(
            [str(self.ffprobe), "-v", "error", "-show_entries",
             "format=duration:stream=index,codec_type,width,height,duration:stream_disposition=default",
             "-of", "json", str(path)], timeout=self.timeout,
        )
        content = json.loads(result.stdout)
        video = [stream for stream in content["streams"] if stream["codec_type"] == "video"]
        if not video:
            raise ValueError("Media has no video stream.")
        durations = [video[0].get("duration"), content.get("format", {}).get("duration")]
        duration = next((float(value) for value in durations if value not in (None, "N/A") and math.isfinite(float(value)) and float(value) > 0), None)
        if duration is None:
            raise ValueError("Media duration is unavailable.")
        audio = [stream for stream in content["streams"] if stream["codec_type"] == "audio"]
        defaults = [stream for stream in audio if stream.get("disposition", {}).get("default") == 1]
        if len(audio) == 1:
            selected = audio[0]["index"]
        elif len(defaults) == 1:
            selected = defaults[0]["index"]
        elif audio:
            raise ValueError("Multiple audio tracks have no unique default.")
        else:
            selected = None
        return {"duration_seconds": duration, "video_stream": video[0]["index"],
                "audio_stream": selected, "audio_status": "present" if audio else "no_audio"}

    def _decode(self, path, start, duration, options):
        if self.scope_guard:
            self.scope_guard(path)
        result = run_media_tool(
            [str(self.ffmpeg), "-v", "error", "-nostdin", "-threads", str(self.threads), "-filter_threads", str(self.threads), "-ss", str(start), "-i", str(path),
             "-t", str(duration), *options, "pipe:1"],
            timeout=self.timeout,
        )
        if not result.stdout:
            raise ValueError("Decoding produced no media samples.")
        return result.stdout

    def frames(self, path, start, duration, stream, fps=8, count=16):
        if not 0 < duration <= 10 or not 1 <= count <= 64 or fps <= 0:
            raise ValueError("Invalid bounded frame request.")
        # A still-frame request selects the first actual decoded frame. An fps
        # filter can drop every frame close to a video's final timestamp.
        temporal_filter = f"fps={fps}:round=up," if count > 1 else ""
        data = self._decode(path, start, duration, ["-map", f"0:{stream}",
            "-vf", temporal_filter + "scale=256:256:force_original_aspect_ratio=increase,crop=224:224",
            "-frames:v", str(count), "-pix_fmt", "rgb24", "-f", "rawvideo"])
        if len(data) % (224 * 224 * 3):
            raise ValueError("Incomplete decoded frame.")
        frames = np.frombuffer(data, dtype=np.uint8).reshape(-1, 224, 224, 3).copy()
        if len(frames) < count:
            # A short tail uses repeated last frames, preserving temporal order.
            frames = np.concatenate((frames, np.repeat(frames[-1:], count - len(frames), axis=0)))
        return frames[:count]

    def audio(self, path, start, duration, stream, sample_rate=16000):
        if stream is None:
            return None
        if not 0 < duration <= 10 or sample_rate != 16000:
            raise ValueError("Invalid bounded audio request.")
        data = self._decode(path, start, duration, ["-map", f"0:{stream}", "-vn",
            "-ac", "1", "-ar", str(sample_rate), "-f", "f32le"])
        audio = np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)
        if not np.isfinite(audio).all() or len(audio) > (duration + 0.1) * sample_rate:
            raise ValueError("Invalid decoded audio samples.")
        return audio
