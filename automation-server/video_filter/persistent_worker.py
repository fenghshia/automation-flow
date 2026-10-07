"""Sequential isolated worker: cache CPU models, never own DB or idle GPU leases."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def main():
    from .worker_gate import await_gate
    await_gate()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("extraction", "prediction"), required=True)
    parser.add_argument("--cache-mib", type=int, required=True)
    args = parser.parse_args()
    from .model_cache import ModelCache
    from .observability import configure_worker_logs, log_failure
    cache = ModelCache(args.cache_mib)
    for line in sys.stdin:
        request = None
        try:
            command = json.loads(line)
            request = json.loads(Path(command["request"]).read_text(encoding="utf-8"))
            configure_worker_logs(request)
            if not request.get("phase_managed"):
                raise ValueError("persistent_worker_requires_gpu_phase_control")
            if args.kind == "extraction":
                from .worker import execute
            else:
                from .inference_worker import execute
            execute(request, Path(command["output"]), model_cache=cache)
            # All task-local media/tensors leave scope before accepting a new job.
            import gc
            gc.collect()
            print(json.dumps({"type": "video_filter_done", "task_id": request["task_id"],
                              "success": True, "worker_pid": os.getpid()}), flush=True)
        except Exception as error:
            log_failure(logging.getLogger("video_filter.persistent_worker"), "常驻子进程失败，等待父进程回收", error)
            print(json.dumps({"type": "video_filter_done", "task_id": (request or {}).get("task_id"),
                              "success": False}), flush=True)
            return 1  # Never reuse native/GPU state after an exception.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
