"""Do not start decoding before the parent owns the worker process tree."""

import os
import time
from pathlib import Path


def await_gate():
    value = os.environ.get("VIDEO_FILTER_WORKER_GATE")
    if value:
        deadline = time.monotonic() + 30
        while not Path(value).is_file():
            if time.monotonic() > deadline:
                raise ValueError("worker_parent_gate_timeout")
            time.sleep(.05)
