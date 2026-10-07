"""Isolated PyTorch MIL inference. No application, database or media access."""

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path


def main():
    from .worker_gate import await_gate
    await_gate()
    import numpy as np
    from .mil import deserialize, torch_probability
    from .observability import configure_worker_logs, redact_paths
    from .progress import extra

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    execute(request, args.output)


def execute(request, output, model_cache=None):
    import numpy as np
    from .mil import deserialize, torch_probability, prepare_inference
    from .observability import configure_worker_logs, redact_paths
    from .progress import extra

    configure_worker_logs(request)
    redact_paths([request["bag_path"], request["model_path"], output])
    if request.get("dataset_group_id"):
        if not request.get("phase_managed") and (not request.get("resource_granted") or not request.get("resource_lease_id")):
            raise ValueError("prediction_resource_required")
        from env import EnvConfig
        group = next((g for g in EnvConfig.video_filter_settings(ignore_scope=True).get("groups", [])
                      if g["name"] == request.get("group_name") and g["enabled"]), None)
        if group is None:
            raise ValueError("configuration_changed")
    blob = Path(request["model_path"]).read_bytes()
    if hashlib.sha256(blob).hexdigest() != request["model_sha256"]:
        raise ValueError("model_checksum_mismatch")
    with np.load(request["bag_path"], allow_pickle=False) as archive:
        bag = (archive["x"], archive["valid"])
    logger = logging.getLogger("video_filter.inference_worker")
    started = time.monotonic()
    logger.info("MIL PyTorch推理开始 | task_id=%s | device=%s | windows=%s",
                request["task_id"], request["device"], len(bag[0]),
                extra=extra(request["task_id"], "MIL PyTorch推理", modality="mil", completed=0, total=1))
    from .gpu_phase import WorkerGpuPhases
    cached = None
    if model_cache is not None:
        key = ("mil", request.get("dataset_group_id"), request.get("reset_epoch"), request["model_sha256"])
        cached = model_cache.get(key, lambda: prepare_inference(deserialize(blob)),
            modality="mil", task_id=request["task_id"])
    probability = torch_probability(None if cached else deserialize(blob), bag, device=request["device"],
                                    cpu_threads=request.get("worker_cpu_threads", 1),
                                    gpu_phase=WorkerGpuPhases(request) if request.get("phase_managed") else None,
                                    prepared_model=cached)
    output.mkdir()
    (output / "result.json").write_text(json.dumps({"probability": probability,
        "backend": "pytorch", "device": request["device"], "model_sha256": request["model_sha256"]},
        allow_nan=False), encoding="utf-8")
    logger.info("MIL PyTorch推理完成 | task_id=%s | device=%s | probability=%.6f",
                request["task_id"], request["device"], probability,
                extra=extra(request["task_id"], "MIL PyTorch推理完成", modality="mil", completed=1, total=1,
                            elapsed_seconds=time.monotonic() - started))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from .observability import log_failure
        log_failure(logging.getLogger("video_filter.inference_worker"), "MIL推理子进程失败", error)
        raise SystemExit(1)
