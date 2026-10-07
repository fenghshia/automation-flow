"""Bounded local decoding; no screenshots, audio clips or transcripts persist."""

import json
import logging
import math
import os
import subprocess
import threading
import time
import queue
import re
from collections import deque
from pathlib import Path
from logging_config import log_performance

import numpy as np


logger = logging.getLogger(__name__)

# Explicit CUVID decoders prevent unsupported streams silently using software.
NVDEC_CODECS = {name: name + "_cuvid" for name in
                ("h264", "hevc", "av1", "vp8", "vp9", "mpeg4", "vc1", "mjpeg")}
NVDEC_CODECS.update(mpeg1video="mpeg1_cuvid", mpeg2video="mpeg2_cuvid")


def _reserved_color_error(error):
    detail = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
    return "Unsupported input" in detail and ("prim:reserved" in detail or "trc:reserved" in detail)


def run_media_tool(arguments, timeout, *, recover_reserved_color=False):
    try:
        return subprocess.run(arguments, capture_output=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
        log = logger.warning if recover_reserved_color and _reserved_color_error(error) else logger.error
        log("媒体工具执行失败 | returncode=%s | stderr=%s", error.returncode, detail.strip())
        raise


class MediaDecoder:
    def __init__(self, ffmpeg_directory, timeout=120, threads=1, scope_guard=None, device="cuda:0"):
        suffix = ".exe" if os.name == "nt" else ""
        self.ffmpeg = Path(ffmpeg_directory) / ("ffmpeg" + suffix)
        self.ffprobe = Path(ffmpeg_directory) / ("ffprobe" + suffix)
        self.timeout = timeout
        self.threads = threads
        self.scope_guard = scope_guard
        if not isinstance(device, str) or not device.startswith("cuda:") or not device[5:].isdigit():
            raise ValueError("nvdec_requires_cuda_device")
        # FFmpeg's CUDA context inherits CUDA_VISIBLE_DEVICES, just like torch.
        self.cuda_ordinal = device[5:]
        self._video_codecs = {}
        self._video_colors = {}
        self._video_color_recovery = set()

    def probe(self, path):
        if self.scope_guard:
            self.scope_guard(path)
        result = run_media_tool(
            [str(self.ffprobe), "-v", "error", "-show_entries",
             "format=duration:stream=index,codec_type,codec_name,width,height,duration,color_primaries,color_transfer:stream_disposition=default",
             "-of", "json", str(path)], timeout=self.timeout,
        )
        content = json.loads(result.stdout)
        video = [stream for stream in content["streams"] if stream["codec_type"] == "video"]
        if not video:
            raise ValueError("Media has no video stream.")
        self._video_codecs = {(str(Path(path).resolve()), stream["index"]): stream.get("codec_name") for stream in video}
        self._video_colors = {(str(Path(path).resolve()), stream["index"]):
            (stream.get("color_primaries"), stream.get("color_transfer")) for stream in video}
        self._video_color_recovery.clear()
        for stream in video:
            if "reserved" in self._video_colors[(str(Path(path).resolve()), stream["index"])]:
                logger.warning("视频色彩元数据含 reserved，提取帧时修正无效标记 | stream=%s", stream["index"])
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
                "audio_stream": selected, "audio_status": "present" if audio else "no_audio",
                "video_codec": video[0].get("codec_name"), "video_decode_backend": "nvidia_nvdec",
                "width": video[0].get("width"), "height": video[0].get("height"),
                "video_decode_device": "cuda:" + self.cuda_ordinal}

    def _decode(self, path, start, duration, options, input_options=(), *, allow_empty=False, recover_reserved_color=False):
        if self.scope_guard:
            self.scope_guard(path)
        result = run_media_tool(
            [str(self.ffmpeg), "-v", "error", "-xerror", "-nostdin", "-threads", str(self.threads), "-filter_threads", str(self.threads), "-ss", str(start), *input_options, "-i", str(path),
             "-t", str(duration), *options, "pipe:1"],
            timeout=self.timeout, **({"recover_reserved_color": True} if recover_reserved_color else {}),
        )
        if not result.stdout and not allow_empty:
            raise ValueError("Decoding produced no media samples.")
        return result.stdout

    def frames(self, path, start, duration, stream, fps=8, count=16):
        if not 0 < duration <= 10 or not 1 <= count <= 64 or fps <= 0:
            raise ValueError("Invalid bounded frame request.")
        # A still-frame request selects the first actual decoded frame. An fps
        # filter can drop every frame close to a video's final timestamp.
        temporal_filter = f"fps={fps}:round=up," if count > 1 else ""
        key = (str(Path(path).resolve()), stream)
        if key not in self._video_codecs:
            self.probe(path)
        codec = self._video_codecs.get(key)
        if codec not in NVDEC_CODECS:
            raise ValueError("nvdec_codec_unsupported")
        # Keep frames in host memory for the existing RGB filters/adapters.
        # CUVID retrieves the appropriate native pixel format, including 10-bit.
        hardware = [f"-hwaccel:{stream}", "cuda", f"-hwaccel_device:{stream}", self.cuda_ordinal,
                    f"-c:{stream}", NVDEC_CODECS[codec]]
        colors = self._video_colors.get(key, (None, None))
        correction = self._color_correction(colors, force=key in self._video_color_recovery)
        filters = temporal_filter + "scale=256:256:force_original_aspect_ratio=increase,crop=224:224"
        def decode(sample_start, sample_duration, sample_filter, limit):
            nonlocal correction
            options = ["-map", f"0:{stream}", "-vf", correction + sample_filter,
                "-frames:v", str(limit), "-pix_fmt", "rgb24", "-f", "rawvideo"]
            try:
                return self._decode(path, sample_start, sample_duration, options,
                    input_options=hardware, allow_empty=True, recover_reserved_color=not correction)
            except subprocess.CalledProcessError as error:
                if correction or not _reserved_color_error(error):
                    raise
                # Retain NVDEC and cache this correction for subsequent clips.
                logger.warning("视频帧颜色元数据为 reserved，修正后重试 NVDEC RGB 转换 | stream=%s", stream)
                correction = self._color_correction(colors, force=True)
                options[3] = correction + sample_filter
                result = self._decode(path, sample_start, sample_duration, options,
                    input_options=hardware, allow_empty=True)
                self._video_color_recovery.add(key)
                return result
        data = decode(start, duration, filters, count)
        spatial_filter = "scale=256:256:force_original_aspect_ratio=increase,crop=224:224"
        if not data and count > 1:
            # FPS buffering can drop the only frame in a short/VFR tail clip.
            logger.warning("视频片段重采样无帧，保留 NVDEC 并重试实际帧 | start=%.3f | duration=%.3f", start, duration)
            data = decode(start, duration, spatial_filter, count)
        if not data and start > 0:
            # A rounded duration may extend past the last frame. Recover one
            # real nearby frame, then use the existing short-tail repetition.
            earlier = max(0.0, start - 1.0)
            logger.warning("视频尾部/稀疏片段无帧，向前最多一秒寻找实际帧 | start=%.3f | retry_start=%.3f", start, earlier)
            data = decode(earlier, min(10.0, start + duration - earlier), spatial_filter, 1)
        if not data:
            raise ValueError("Decoding produced no media samples after bounded NVDEC recovery.")
        if len(data) % (224 * 224 * 3):
            raise ValueError("Incomplete decoded frame.")
        frames = np.frombuffer(data, dtype=np.uint8).reshape(-1, 224, 224, 3).copy()
        if len(frames) < count:
            # A short tail uses repeated last frames, preserving temporal order.
            frames = np.concatenate((frames, np.repeat(frames[-1:], count - len(frames), axis=0)))
        return frames[:count]

    @staticmethod
    def _color_correction(colors, force=False):
        # Preserve declared valid HDR/SDR tags. BT.709 is only a fallback for
        # invalid/unknown tags during recovery; this does not tone-map HDR.
        primaries, transfer = colors
        valid_primaries = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020", "smpte428", "smpte431", "smpte432"}
        valid_transfer = {"bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear", "log100", "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12", "smpte2084", "smpte428", "arib-std-b67"}
        fields = []
        if primaries == "reserved" or force:
            fields.append("color_primaries=" + (primaries if primaries in valid_primaries else "bt709"))
        if transfer == "reserved" or force:
            fields.append("color_trc=" + (transfer if transfer in valid_transfer else "bt709"))
        return "setparams=" + ":".join(fields) + "," if fields else ""

    def audio(self, path, start, duration, stream, sample_rate=16000):
        if stream is None:
            return None
        if not 0 < duration <= 10 or sample_rate != 16000:
            raise ValueError("Invalid bounded audio request.")
        data = self._decode(path, start, duration, ["-map", f"0:{stream}", "-vn",
            "-ac", "1", "-ar", str(sample_rate), "-f", "f32le"], allow_empty=True)
        audio = np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)
        if not np.isfinite(audio).all() or len(audio) > (duration + 0.1) * sample_rate:
            raise ValueError("Invalid decoded audio samples.")
        return audio

    def audio_windows(self, path, windows, stream, *, task_id=None):
        """One continuous decode, one bounded PCM window in memory, no media files.

        The read watchdog runs only while waiting for FFmpeg, not while the caller
        computes features. Closing the generator always terminates the child.
        """
        if stream is None or not windows:
            return
        if windows[0][0] != 0 or any(not 0 < end - start <= 10 for start, end in windows):
            raise ValueError("Invalid continuous audio windows.")
        if any(windows[i][1] != windows[i + 1][0] for i in range(len(windows) - 1)):
            raise ValueError("Continuous audio windows must be adjacent.")
        if self.scope_guard:
            self.scope_guard(path)
        command = [str(self.ffmpeg), "-v", "info", "-xerror", "-nostdin", "-threads", str(self.threads),
            "-copyts", "-start_at_zero", "-i", str(path), "-t", str(windows[-1][1]), "-map", f"0:{stream}", "-vn",
            "-af", "aresample=16000,aformat=sample_fmts=flt,asettb=1/16000,ashowinfo",
            "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"]
        started, read_seconds, emitted = time.monotonic(), 0., 0
        log_performance("audio_decode_started", task_id=task_id, windows=len(windows), decoder_processes=1)
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            stopped, timed_out = threading.Event(), threading.Event()
            deadline = [None]
            frames, errors = queue.Queue(maxsize=4), deque(maxlen=16)
            # pts_time uses rounded decimal text (increasingly coarse for long
            # videos). Integer PTS in a fixed sample timebase preserves exact
            # frame boundaries and cannot create false overlaps after rounding.
            frame_pattern = re.compile(r"\bpts:(-?\d+)\s+pts_time:[^\s]+.*nb_samples:(\d+)")
            def send(value):
                while not stopped.is_set():
                    try:
                        frames.put(value, timeout=.05)
                        return
                    except queue.Full:
                        pass
            def metadata():
                try:
                    for line in process.stderr:
                        text = line.decode("utf-8", errors="replace")
                        if "ashowinfo" in text and "nb_samples:" in text:
                            match = frame_pattern.search(text)
                            send((int(match[1]), int(match[2])) if match else
                                 ValueError("Invalid continuous audio frame metadata."))
                        else:
                            errors.append(line[-4096:])
                except Exception as error:
                    send(error)
                finally:
                    send(None)
            def watchdog():
                while not stopped.wait(.05):
                    limit = deadline[0]
                    if limit is not None and time.monotonic() >= limit:
                        timed_out.set()
                        process.kill()
                        return
            watcher = threading.Thread(target=watchdog, name="video-filter-audio-timeout", daemon=True)
            reader = threading.Thread(target=metadata, name="video-filter-audio-timestamps", daemon=True)
            watcher.start()
            reader.start()
            def read(count):
                nonlocal read_seconds
                before = time.monotonic()
                deadline[0] = before + self.timeout
                try:
                    data = process.stdout.read(count)
                    if timed_out.is_set():
                        raise subprocess.TimeoutExpired(command, self.timeout)
                    return data
                finally:
                    deadline[0] = None
                    read_seconds += time.monotonic() - before
            try:
                frame, frame_start, ended, previous_end = None, 0., False, None
                def next_frame():
                    nonlocal ended, previous_end, read_seconds
                    before = time.monotonic()
                    try:
                        item = frames.get(timeout=self.timeout)
                    except queue.Empty as error:
                        raise subprocess.TimeoutExpired(command, self.timeout) from error
                    finally:
                        read_seconds += time.monotonic() - before
                    if item is None:
                        ended = True
                        return None, 0.
                    if isinstance(item, Exception):
                        raise item
                    pts, count = item
                    if not 0 < count <= 160000:
                        raise ValueError("Invalid continuous audio frame metadata.")
                    if previous_end is not None and pts < previous_end - 1:
                        raise ValueError("Continuous audio timestamps overlap.")
                    data = read(count * 4)
                    if len(data) % 4:
                        raise ValueError("Incomplete decoded audio sample.")
                    samples = np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)
                    if not np.isfinite(samples).all():
                        raise ValueError("Invalid decoded audio samples.")
                    previous_end = pts + count
                    return samples, pts
                for index, (start, end) in enumerate(windows):
                    if self.scope_guard:
                        self.scope_guard(path)
                    parts = []
                    start_sample, end_sample = round(start * 16000), round(end * 16000)
                    while True:
                        if frame is None and not ended:
                            frame, frame_start = next_frame()
                        if ended or frame_start >= end_sample:
                            break
                        first = max(0, start_sample - frame_start)
                        last = min(len(frame), max(0, end_sample - frame_start))
                        if last > first:
                            parts.append(frame[first:last])
                        if frame_start + len(frame) > end_sample:
                            break  # Retain only the boundary-crossing frame.
                        frame = None
                    audio = np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
                    if len(audio) > round((end - start + .1) * 16000):
                        raise ValueError("Continuous audio exceeded its requested window.")
                    emitted += 1
                    yield index, audio
                while not ended:
                    next_frame()  # Drain trimmed end-frame metadata before checking exit.
                process.wait(timeout=self.timeout)
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, stderr=b"".join(errors))
            finally:
                stopped.set()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=self.timeout)
                watcher.join(timeout=1)
                reader.join(timeout=1)
                log_performance("audio_decode_finished", task_id=task_id, windows=emitted,
                    decoder_processes=1, elapsed_seconds=round(time.monotonic() - started, 4),
                    read_seconds=round(read_seconds, 4), returncode=process.returncode)
