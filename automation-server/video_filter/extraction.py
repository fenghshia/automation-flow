"""Four sequential frozen adapters, using dense video clips and aligned audio."""

import gc
import logging
import time
from collections import OrderedDict

import numpy as np
from logging_config import log_performance

from .features.contract import DIMENSIONS, aligned_windows
from .identity import hash_stable
from .progress import extra


logger = logging.getLogger(__name__)


def extract(path, decoder, signature, artifacts, device="cuda:0", adapter_factories=None, batch_size=1, task_id=None, audio_cache_mib=32,
            batch_sizes=None, gpu_phase=None):
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
    verified_at = time.monotonic()
    source_sha256, source = hash_stable(path)
    log_performance("extraction_phase", task_id=task_id, phase="source_hash_before",
        elapsed_seconds=round(time.monotonic() - verified_at, 4))
    info = decoder.probe(path)
    window_size = signature.windows["duration_seconds"]
    if window_size != 10:
        raise ValueError("The initial adapters require aligned 10-second windows.")
    windows = aligned_windows(info["duration_seconds"], window_size)
    logger.info("媒体读取完成 | task_id=%s | duration=%.3fs | windows=%s | audio=%s", task_id or "-", info["duration_seconds"], len(windows), info["audio_status"])
    vectors = {name: np.zeros((len(windows), size), dtype=np.float32) for name, size in DIMENSIONS.items()}
    validity = {name: np.zeros(value.shape, dtype=np.bool_) for name, value in vectors.items()}
    measurements = {}
    audio_cache, cache_bytes, cache_hits = OrderedDict(), 0, 0
    missing_audio_windows = set()
    cache_limit = max(0, int(audio_cache_mib)) * 1024**2
    streaming_audio = callable(getattr(decoder, "audio_windows", None))
    batched = streaming_audio and all(callable(getattr(adapter_factories[name], "extract_batch", None))
                                     for name in ("dino", "videomae", "beats"))
    if batched:
        from .batched_extraction import modalities
        sizes = {name: (batch_sizes or {}).get(name, batch_size) for name in ("dino", "videomae", "beats")}
        if any(type(size) is not int or not 1 <= size <= 64 for size in sizes.values()):
            raise ValueError("Invalid modality batch size.")
        measurements, missing_audio_windows = modalities(path, decoder, signature, artifacts, adapter_factories,
            device, windows, info, vectors, validity, sizes, gpu_phase, task_id)
        cache_hits = len(windows) - len(missing_audio_windows) if info["audio_status"] != "no_audio" else 0
    for name in (() if batched else DIMENSIONS):
        if name == "egemaps" and streaming_audio:
            continue  # Computed beside BEATs on the same PCM window.
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
        log_performance("model_loaded", task_id=task_id, modality=name,
            elapsed_seconds=round(time.monotonic() - started, 4))
        audio_iterator, acoustic = None, None
        acoustic_seconds = 0.
        if name == "beats" and streaming_audio:
            acoustic_started = time.monotonic()
            try:
                acoustic = adapter_factories["egemaps"](artifacts["egemaps"], signature.models["egemaps"], "cpu")
                audio_iterator = decoder.audio_windows(path, windows, info["audio_stream"], task_id=task_id)
            except Exception:
                del adapter
                raise
            acoustic_seconds += time.monotonic() - acoustic_started
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
                    cached = audio_cache.pop(index, None) if name == "egemaps" else None
                    if audio_iterator is not None:
                        position, audio = next(audio_iterator)
                        if position != index:
                            raise ValueError("Continuous audio window alignment changed.")
                    elif cached is not None:
                        audio = cached
                        cache_bytes -= audio.nbytes
                        cache_hits += 1
                    else:
                        audio = decoder.audio(path, start, duration, info["audio_stream"])
                    if name == "beats" and audio_iterator is None and audio.nbytes <= cache_limit:
                        while audio_cache and cache_bytes + audio.nbytes > cache_limit:
                            cache_bytes -= audio_cache.popitem(last=False)[1].nbytes
                        audio_cache[index] = audio
                        cache_bytes += audio.nbytes
                    if not len(audio):
                        missing_audio_windows.add(index)
                        logger.warning("音频窗口无样本，保存明确缺失掩码 | task_id=%s | modality=%s | window=%s/%s | start=%.3f | end=%.3f",
                            task_id or "-", name, index + 1, len(windows), start, end,
                            extra=extra(task_id, "特征提取", modality=name, completed=index + 1, total=len(windows)))
                        continue
                    if name == "beats":
                        chunks = [audio[offset:offset + 80000] for offset in range(0, len(audio), 80000)]
                        value = np.average(np.stack([adapter.extract(chunk) for chunk in chunks]),
                                           axis=0, weights=[len(chunk) for chunk in chunks]).astype(np.float32)
                        if acoustic is not None:
                            acoustic_started = time.monotonic()
                            acoustic_value, mask = acoustic.extract(audio)
                            acoustic_seconds += time.monotonic() - acoustic_started
                            if acoustic_value.shape != (DIMENSIONS["egemaps"],) or not np.isfinite(acoustic_value).all():
                                raise ValueError("Adapter returned an invalid feature vector.")
                            vectors["egemaps"][index], validity["egemaps"][index] = acoustic_value, mask
                            cache_hits += 1
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
                    log_performance("window_progress", task_id=task_id, modality=name,
                        completed=index + 1, total=len(windows), elapsed_seconds=round(last_progress - started, 4),
                        allocated_mib=round(gpu.cuda.memory_allocated(device) / 1024**2, 2) if gpu else None,
                        reserved_mib=round(gpu.cuda.memory_reserved(device) / 1024**2, 2) if gpu else None,
                        audio_shared=audio_iterator is not None)
            if audio_iterator is not None:
                # Consume the decoder's terminal status before accepting features.
                try:
                    next(audio_iterator)
                except StopIteration:
                    pass
                else:
                    raise ValueError("Unexpected continuous audio window.")
        finally:
            if audio_iterator is not None:
                audio_iterator.close()
                measurements["egemaps"] = {"seconds": acoustic_seconds, "shared_audio": True}
                del acoustic
            del adapter
            gc.collect()
            measurements[name] = {"seconds": time.monotonic() - started}
            if gpu:
                measurements[name]["peak_memory_bytes"] = gpu.cuda.max_memory_allocated(device)
                gpu.cuda.empty_cache()
            logger.info("模型资源已释放 | task_id=%s | modality=%s | elapsed=%.1fs | peak_gpu_mib=%.1f", task_id or "-", name,
                measurements[name]["seconds"], measurements[name].get("peak_memory_bytes", 0) / 1024**2)
            log_performance("modality_finished", task_id=task_id, modality=name,
                elapsed_seconds=round(measurements[name]["seconds"], 4),
                peak_gpu_mib=round(measurements[name].get("peak_memory_bytes", 0) / 1024**2, 2),
                acoustic_seconds=round(acoustic_seconds, 4) if name == "beats" and streaming_audio else None)
    if getattr(decoder, "scope_guard", None):
        decoder.scope_guard(path)
    verified_at = time.monotonic()
    if hash_stable(path, source)[0] != source_sha256:
        raise ValueError("Video changed during feature extraction.")
    log_performance("extraction_phase", task_id=task_id, phase="source_hash_after",
        elapsed_seconds=round(time.monotonic() - verified_at, 4))
    measurements["audio_cache"] = {"hits": cache_hits, "budget_mib": audio_cache_mib, "continuous": streaming_audio}
    info["audio_missing_windows"] = sorted(missing_audio_windows)
    logger.info("音频内存复用完成 | task_id=%s | hits=%s | windows=%s | budget_mib=%s", task_id or "-", cache_hits, len(windows), audio_cache_mib)
    return {"source_sha256": source_sha256, "source_snapshot": {key: source[key] for key in ("size_bytes", "modified_ns")},
            "duration_seconds": info["duration_seconds"], "audio_status": info["audio_status"],
            "audio_missing_windows": sorted(missing_audio_windows),
            "windows": windows, "vectors": vectors, "validity": validity,
            "media_metadata": info, "measurements": measurements}
