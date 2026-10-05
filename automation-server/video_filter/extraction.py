"""Four sequential frozen adapters, using dense video clips and aligned audio."""

import gc
import logging
import time

import numpy as np

from .features.contract import DIMENSIONS, aligned_windows
from .identity import hash_stable
from .progress import extra


logger = logging.getLogger(__name__)


def extract(path, decoder, signature, artifacts, device="cuda:0", adapter_factories=None, batch_size=1, task_id=None):
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("Invalid extraction batch size.")
    if adapter_factories is None:
        from .features.dino import DinoAdapter
        from .features.videomae import VideoMAEAdapter
        from .features.beats import BeatsAdapter
        from .features.acoustic import AcousticAdapter

        adapter_factories = {"dino": DinoAdapter, "videomae": VideoMAEAdapter,
                             "beats": BeatsAdapter, "egemaps": AcousticAdapter}
    if getattr(decoder, "scope_guard", None):
        decoder.scope_guard(path)
    source_sha256, source = hash_stable(path)
    info = decoder.probe(path)
    window_size = signature.windows["duration_seconds"]
    if window_size != 10:
        raise ValueError("The initial adapters require aligned 10-second windows.")
    windows = aligned_windows(info["duration_seconds"], window_size)
    logger.info("媒体读取完成 | task_id=%s | duration=%.3fs | windows=%s | audio=%s", task_id or "-", info["duration_seconds"], len(windows), info["audio_status"])
    vectors = {name: np.zeros((len(windows), size), dtype=np.float32) for name, size in DIMENSIONS.items()}
    validity = {name: np.zeros(value.shape, dtype=np.bool_) for name, value in vectors.items()}
    measurements = {}
    for name in DIMENSIONS:
        if info["audio_status"] == "no_audio" and name in ("beats", "egemaps"):
            logger.info("无音轨，保存明确缺失掩码 | task_id=%s | modality=%s", task_id or "-", name)
            continue
        started = time.monotonic()
        logger.info("模型加载开始 | task_id=%s | modality=%s", task_id or "-", name,
            extra=extra(task_id, "模型加载", modality=name))
        gpu = None
        if device.startswith("cuda") and adapter_factories[name].__module__.startswith("video_filter.features") and name != "egemaps":
            import torch

            gpu = torch
            torch.cuda.init()
            torch.cuda.reset_peak_memory_stats(device)
        adapter = adapter_factories[name](artifacts[name], signature.models[name], "cpu" if name == "egemaps" else device)
        logger.info("模型已加载，开始分析 | task_id=%s | modality=%s | windows=%s", task_id or "-", name, len(windows))
        last_progress = time.monotonic()
        try:
            for index, (start, end) in enumerate(windows):
                duration = end - start
                if name == "dino":
                    positions = np.linspace(start, max(start, end - 0.5), 3)
                    frames = np.concatenate([decoder.frames(path, float(position), min(0.5, end - position),
                        info["video_stream"], fps=8, count=1) for position in positions])
                    parts = [frames[offset:offset + batch_size] for offset in range(0, len(frames), batch_size)]
                    value = np.average(np.stack([adapter.extract(part) for part in parts]), axis=0,
                                       weights=[len(part) for part in parts]).astype(np.float32)
                elif name == "videomae":
                    # One dense consecutive 16-frame clip centered inside the window.
                    clip_duration = min(duration, 2.0)
                    clip_start = start + (duration - clip_duration) / 2
                    value = adapter.extract(decoder.frames(path, clip_start, clip_duration, info["video_stream"], fps=8, count=16))
                else:
                    audio = decoder.audio(path, start, duration, info["audio_stream"])
                    if name == "beats":
                        chunks = [audio[offset:offset + 80000] for offset in range(0, len(audio), 80000)]
                        value = np.average(np.stack([adapter.extract(chunk) for chunk in chunks]),
                                           axis=0, weights=[len(chunk) for chunk in chunks]).astype(np.float32)
                    else:
                        value, mask = adapter.extract(audio)
                        validity[name][index] = mask
                if value.shape != (DIMENSIONS[name],) or not np.isfinite(value).all():
                    raise ValueError("Adapter returned an invalid feature vector.")
                vectors[name][index] = value
                if name != "egemaps":
                    validity[name][index] = True
                if index == 0 or index + 1 == len(windows) or time.monotonic() - last_progress >= 5:
                    logger.info("特征提取进度 | task_id=%s | modality=%s | windows=%s/%s | progress=%.1f%% | elapsed=%.1fs",
                        task_id or "-", name, index + 1, len(windows), 100 * (index + 1) / len(windows), time.monotonic() - started,
                        extra=extra(task_id, "特征提取", modality=name, completed=index + 1, total=len(windows), elapsed_seconds=time.monotonic() - started))
                    last_progress = time.monotonic()
        finally:
            del adapter
            gc.collect()
            measurements[name] = {"seconds": time.monotonic() - started}
            if gpu:
                measurements[name]["peak_memory_bytes"] = gpu.cuda.max_memory_allocated(device)
                gpu.cuda.empty_cache()
            logger.info("模型资源已释放 | task_id=%s | modality=%s | elapsed=%.1fs | peak_gpu_mib=%.1f", task_id or "-", name,
                measurements[name]["seconds"], measurements[name].get("peak_memory_bytes", 0) / 1024**2)
    if getattr(decoder, "scope_guard", None):
        decoder.scope_guard(path)
    if hash_stable(path, source)[0] != source_sha256:
        raise ValueError("Video changed during feature extraction.")
    return {"source_sha256": source_sha256, "source_snapshot": {key: source[key] for key in ("size_bytes", "modified_ns")},
            "duration_seconds": info["duration_seconds"], "audio_status": info["audio_status"],
            "windows": windows, "vectors": vectors, "validity": validity,
            "media_metadata": info, "measurements": measurements}
