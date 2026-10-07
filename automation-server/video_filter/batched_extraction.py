"""Sequential modalities with bounded batches and short parent-owned GPU phases."""

import gc
import logging
import time
from contextlib import nullcontext

import numpy as np
from logging_config import log_performance

from .features.batching import activate, deactivate, infer_batches
from .features.contract import DIMENSIONS
from .progress import extra

logger = logging.getLogger(__name__)


def modalities(path, decoder, signature, artifacts, factories, device, windows, info,
               vectors, validity, batch_sizes, phase, task_id):
    measurements, missing = {}, set()
    for name in ("dino", "videomae", "beats"):
        if name == "beats" and info["audio_status"] == "no_audio":
            continue
        started = time.monotonic()
        size = batch_sizes[name]
        # Checkpoints and model initialization run on CPU before GPU admission.
        logger.info("模型加载开始 | task_id=%s | modality=%s", task_id, name,
            extra=extra(task_id, "模型加载", modality=name))
        adapter = factories[name](artifacts[name], signature.models[name], "cpu")
        log_performance("model_loaded", task_id=task_id, modality=name,
            elapsed_seconds=round(time.monotonic() - started, 4))
        acoustic, iterator = None, None
        peak, acoustic_seconds = 0, 0.
        last_progress = started
        try:
            if name == "beats":
                acoustic = factories["egemaps"](artifacts["egemaps"], signature.models["egemaps"], "cpu")
                iterator = decoder.audio_windows(path, windows, info["audio_stream"], task_id=task_id)
            per_window = 3 if name == "dino" else 2 if name == "beats" else 1
            window_batch = max(1, size // per_window)
            for offset in range(0, len(windows), window_batch):
                indices = list(range(offset, min(len(windows), offset + window_batch)))
                inputs, owners, weights = [], [], []
                if name == "beats":
                    prepared_at = time.monotonic()
                    for index in indices:
                        position, audio = next(iterator)
                        if position != index:
                            raise ValueError("Continuous audio window alignment changed.")
                        if not len(audio):
                            missing.add(index)
                            continue
                        acoustic_at = time.monotonic()
                        value, mask = acoustic.extract(audio)
                        acoustic_seconds += time.monotonic() - acoustic_at
                        if value.shape != (DIMENSIONS["egemaps"],) or not np.isfinite(value).all():
                            raise ValueError("Adapter returned an invalid feature vector.")
                        vectors["egemaps"][index], validity["egemaps"][index] = value, mask
                        for start in range(0, len(audio), 80000):
                            chunk = audio[start:start + 80000]
                            inputs.append(chunk)
                            owners.append(index)
                            weights.append(len(chunk))
                    log_performance("audio_batch_prepared", task_id=task_id, windows=len(indices),
                        chunks=len(inputs), elapsed_seconds=round(time.monotonic() - prepared_at, 4))
                    if not inputs:
                        continue
                requested = min(size, getattr(adapter, "batch_limit", size),
                    len(inputs) if name == "beats" else len(indices) * per_window)
                with phase(name, requested) if phase else nullcontext():
                    try:
                        # NVDEC belongs inside the lease too; probe/hash remain outside.
                        if name != "beats":
                            for index in indices:
                                start, end = windows[index]
                                if name == "dino":
                                    for position in np.linspace(start, max(start, end - .5), 3):
                                        inputs.append(decoder.frames(path, float(position), min(.5, end - position),
                                            info["video_stream"], fps=8, count=1)[0])
                                        owners.append(index)
                                        weights.append(1)
                                else:
                                    duration = min(end - start, 2.)
                                    inputs.append(decoder.frames(path, start + (end - start - duration) / 2,
                                        duration, info["video_stream"], fps=8, count=16))
                                    owners.append(index)
                                    weights.append(1)
                        moved_at = time.monotonic()
                        activate(adapter, device)
                        log_performance("model_gpu_transfer", task_id=task_id, modality=name,
                            direction="to_gpu", elapsed_seconds=round(time.monotonic() - moved_at, 4))
                        if device.startswith("cuda"):
                            adapter.torch.cuda.reset_peak_memory_stats(device)
                        inferred_at = time.monotonic()
                        values = infer_batches(adapter, inputs, size, modality=name, task_id=task_id)
                        if values.shape != (len(inputs), DIMENSIONS[name]):
                            raise ValueError("Adapter returned an invalid feature vector.")
                        log_performance("batch_inferred", task_id=task_id, modality=name, items=len(inputs),
                            windows=len(indices), elapsed_seconds=round(time.monotonic() - inferred_at, 4),
                            allocated_mib=round(adapter.torch.cuda.memory_allocated(device) / 1024**2, 2) if device.startswith("cuda") else None,
                            reserved_mib=round(adapter.torch.cuda.memory_reserved(device) / 1024**2, 2) if device.startswith("cuda") else None)
                    finally:
                        if device.startswith("cuda") and adapter.torch.cuda.is_initialized():
                            peak = max(peak, adapter.torch.cuda.max_memory_allocated(device))
                        moved_at = time.monotonic()
                        deactivate(adapter)  # No model/input tensors survive successful lease release.
                        log_performance("model_gpu_transfer", task_id=task_id, modality=name,
                            direction="to_cpu", elapsed_seconds=round(time.monotonic() - moved_at, 4))
                for index in set(owners):
                    positions = [i for i, owner in enumerate(owners) if owner == index]
                    vectors[name][index] = np.average(values[positions], axis=0,
                        weights=[weights[i] for i in positions]).astype(np.float32)
                    validity[name][index] = True
                completed = indices[-1] + 1
                if offset == 0 or completed == len(windows) or time.monotonic() - last_progress >= 5:
                    last_progress = time.monotonic()
                    logger.info("批量特征进度 | task_id=%s | modality=%s | windows=%s/%s | batch_size=%s",
                        task_id, name, completed, len(windows), size,
                        extra=extra(task_id, "特征提取", modality=name, completed=completed, total=len(windows)))
                    log_performance("window_progress", task_id=task_id, modality=name,
                        completed=completed, total=len(windows), batch_size=size,
                        elapsed_seconds=round(last_progress - started, 4))
            if iterator is not None:
                try:
                    next(iterator)
                except StopIteration:
                    pass
                else:
                    raise ValueError("Unexpected continuous audio window.")
        finally:
            if iterator is not None:
                iterator.close()
            del acoustic, adapter
            gc.collect()
        measurements[name] = {"seconds": time.monotonic() - started, "peak_memory_bytes": peak,
                              "batch_size": size}
        log_performance("modality_finished", task_id=task_id, modality=name, batch_size=size,
            elapsed_seconds=round(measurements[name]["seconds"], 4), peak_gpu_mib=round(peak / 1024**2, 2))
        if name == "beats":
            measurements["egemaps"] = {"seconds": acoustic_seconds, "shared_audio": True}
    return measurements, missing
