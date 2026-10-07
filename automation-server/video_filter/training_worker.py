"""Isolated local GPU MIL training. No Flask/database/scheduler imports."""

import argparse
import json
import logging
import os
from pathlib import Path
from contextlib import nullcontext


def main():
    from .worker_gate import await_gate
    await_gate()
    os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
    import numpy as np
    from media_lineage.resources import gpu_lock
    from .mil import fit
    from .observability import configure_worker_logs, redact_paths

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    configure_worker_logs(request)
    redact_paths([request["dataset_path"], args.output])
    with np.load(request["dataset_path"], allow_pickle=False) as archive:
        y, training, holdout = archive["labels"], archive["fit"], archive["holdout"]
        bags = [(archive["x_" + str(i)], archive["valid_" + str(i)]) for i in range(len(y))]
    if request.get("dataset_group_id") and not request.get("phase_managed") and (not request.get("resource_granted") or not request.get("resource_lease_id")):
        raise ValueError("training_resource_required")
    from .gpu_phase import WorkerGpuPhases
    phase = WorkerGpuPhases(request) if request.get("phase_managed") else None
    with (nullcontext(True) if phase or request.get("resource_granted") else gpu_lock(request["state_directory"])) as acquired:
        if not acquired:
            raise ValueError("gpu_busy")
        parameters, probabilities, measurements = fit(bags, y, training, holdout,
            **{key: request[key] for key in ("device", "epochs", "patience", "max_train_windows", "task_id")},
            **{key: request["hyperparameters"][key] for key in ("seed", "learning_rate", "weight_decay", "dropout")},
            cpu_threads=request.get("worker_cpu_threads", 1), gpu_phase=phase)
    args.output.mkdir()
    (args.output / "result.json").write_text(json.dumps({"parameters": parameters,
        "probabilities": probabilities, "measurements": measurements}, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from .observability import log_failure
        log_failure(logging.getLogger("video_filter.training_worker"), "MIL训练子进程失败", error)
        raise SystemExit(1)
