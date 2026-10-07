"""Worker-side phase protocol. The parent alone owns database GPU leases."""

import json
import sys
import time
from contextlib import contextmanager

from logging_config import log_performance


class WorkerGpuPhases:
    def __init__(self, request):
        self.task_id = request["task_id"]
        self.sequence = 0

    def exchange(self, action, phase, batch_size, oom=False):
        self.sequence += 1
        identifier = self.sequence
        print(json.dumps({"type": "video_filter_gpu", "task_id": self.task_id,
            "sequence": identifier, "action": action, "phase": phase,
            "batch_size": batch_size, "oom": oom}), flush=True)
        line = sys.stdin.readline()
        if not line:
            raise ValueError("gpu_phase_parent_disconnected")
        response = json.loads(line)
        if response.get("sequence") != identifier or response.get("error"):
            raise ValueError("gpu_phase_protocol_failed")
        return response.get("granted") is True

    @contextmanager
    def __call__(self, phase, batch_size=1):
        waiting = time.monotonic()
        log_performance("gpu_phase_waiting", task_id=self.task_id, phase=phase, batch_size=batch_size)
        while not self.exchange("acquire", phase, batch_size):
            time.sleep(.5)  # Parent enforces the whole worker's hard timeout.
        started = time.monotonic()
        log_performance("gpu_phase_started", task_id=self.task_id, phase=phase,
            batch_size=batch_size, wait_seconds=round(started - waiting, 4))
        oom, failed = False, False
        try:
            yield
        except Exception as error:
            failed = True
            oom = "out of memory" in str(error).lower()
            raise
        finally:
            # Tracebacks can retain failed CUDA tensors. On failure the parent
            # holds ownership until the complete process tree has terminated.
            self.exchange("failed" if failed else "release", phase, batch_size, oom)
            log_performance("gpu_phase_finished", task_id=self.task_id, phase=phase,
                batch_size=batch_size, hold_seconds=round(time.monotonic() - started, 4), oom=oom)
