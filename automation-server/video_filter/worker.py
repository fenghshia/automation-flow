"""Bounded isolated extraction process: no Flask, database or scheduler imports."""

import argparse
import json
import logging
import os
import time
from logging_config import log_performance
from pathlib import Path
from contextlib import nullcontext


def main():
    from .worker_gate import await_gate
    await_gate()
    # Supported MKL configuration avoids loading a second OpenMP runtime with torch.
    os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    execute(request, args.output)


def execute(request, output, model_cache=None):
    threads = request.get("worker_cpu_threads", 1)
    scope_guard = None
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(threads)
    if request.get("dataset_group_id"):
        from env import EnvConfig
        from .group_config import require_scope
        settings = EnvConfig.video_filter_settings()
        group = next((g for g in settings.get("groups", []) if g["name"] == request.get("group_name") and g["enabled"]), None)
        if group is None or (not request.get("phase_managed") and (not request.get("resource_granted") or not request.get("resource_lease_id"))):
            raise ValueError("current_group_and_resource_required")
        require_scope(group, request["path"], sample=True)
        def scope_guard(path):
            from .group_config import require_current_group
            require_current_group(group)
            require_scope(group, path, sample=True)
    from .extraction import extract
    from .feature_store import FeatureStore
    from .features.manifest import load_model_manifest
    from .media import MediaDecoder
    import torch
    torch.set_num_threads(threads)
    from .observability import configure_worker_logs
    from media_lineage.resources import gpu_lock

    configure_worker_logs(request)
    logger = logging.getLogger("video_filter.worker")
    logger.info("提取子进程启动 | task_id=%s | variant_id=%s | device=%s", request["task_id"], request["variant_id"], request["device"])
    signature, artifacts = load_model_manifest(request["model_manifest"])
    if signature.digest != request["feature_signature"]:
        raise ValueError("model_signature_changed")
    from .gpu_phase import WorkerGpuPhases
    phase = WorkerGpuPhases(request) if request.get("phase_managed") else None
    with (nullcontext(True) if phase or request.get("resource_granted") else gpu_lock(request["state_directory"])) as acquired:
        if not acquired:
            logger.warning("提取等待 GPU：资源由其他任务持有 | task_id=%s", request["task_id"])
            raise ValueError("gpu_busy")
        logger.info("视频解码使用 NVIDIA NVDEC | task_id=%s | device=%s", request["task_id"], request["device"])
        result = extract(request["path"], MediaDecoder(request["ffmpeg_directory"], threads=threads, scope_guard=scope_guard, device=request["device"]), signature, artifacts,
                         request["device"], batch_size=request.get("batch_size", 1), task_id=request["task_id"], audio_cache_mib=request.get("audio_cache_mib", 32),
                         batch_sizes=request.get("batch_sizes"), gpu_phase=phase,
                         adapter_factories=model_cache.factories(request["task_id"]) if model_cache else None)
        logger.info("四路特征完成，开始序列化摘要 | task_id=%s | windows=%s", request["task_id"], len(result["windows"]))
        serialized_at = time.monotonic()
        prepared = FeatureStore().prepare(asset_id=request["asset_id"], variant_id=request["variant_id"],
            task_id=request["task_id"], signature=signature,
            **{key: request[key] for key in ("dataset_group_id", "reset_epoch") if key in request},
            **{name: result[name] for name in ("source_sha256", "source_snapshot", "duration_seconds", "windows", "vectors", "validity", "audio_status", "audio_missing_windows")})
        output.mkdir()
        (output / "arrays.npz").write_bytes(prepared.arrays_blob)
        (output / "metadata.json").write_text(json.dumps({"manifest": prepared.manifest,
            "manifest_sha256": prepared.manifest_sha256, "measurements": result["measurements"],
            "media_metadata": result["media_metadata"], "source_verified": True}), encoding="utf-8")
        log_performance("summary_serialized", task_id=request["task_id"], payload_bytes=len(prepared.arrays_blob),
            elapsed_seconds=round(time.monotonic() - serialized_at, 4))
        logger.info("提取子进程完成，等待主进程入库 | task_id=%s | payload_bytes=%s", request["task_id"], len(prepared.arrays_blob))


if __name__ == "__main__":
    import sys

    try:
        main()
    except Exception as error:
        from .observability import log_failure as log_exception

        log_exception(logging.getLogger("video_filter.worker"), "提取子进程失败", error)
        raise SystemExit(1)
