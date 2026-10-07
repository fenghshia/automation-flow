"""Parent-owned bounded worker pool with cancellation, retirement and task isolation."""

import json
import atexit
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from logging_config import log_performance
from .process_tree import ProcessTree

logger = logging.getLogger(__name__)
_condition = threading.Condition()
_workers = []
_closed = False


class PersistentWorker:
    def __init__(self, key, environment, gate):
        self.key, self.busy, self.completed = key, True, 0
        self.last_used, self.idle_seconds = time.monotonic(), 120
        self.messages = queue.Queue(maxsize=256)
        self.stopped = threading.Event()
        self.closed = False
        self.close_lock = threading.Lock()
        self.stderr = tempfile.TemporaryFile()
        kind, device, threads, cache_mib = key[:4]
        self.process = subprocess.Popen([sys.executable, "-m", "video_filter.persistent_worker",
            "--kind", kind, "--cache-mib", str(cache_mib)], cwd=Path(__file__).parents[1],
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
        try:
            self.tree = ProcessTree(self.process)
        except Exception:
            self.stderr.close()
            raise
        self.reader = threading.Thread(target=self._read, name="video-filter-persistent-logs", daemon=True)
        self.reader.start()
        gate.touch()  # Own the complete process tree before model/decoder execution.

    def _send(self, value):
        while not self.stopped.is_set():
            try:
                self.messages.put(value, timeout=.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    self._send(value)
        finally:
            self._send(None)

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.stopped.set()
            self.tree.terminate()
            self.tree.close()
            self.process.wait(timeout=10)
            self.reader.join(timeout=2)
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass  # Process-tree death has already been confirmed.
            self.stderr.close()
            self.closed = True
            log_performance("worker_retired", worker_pid=self.process.pid, kind=self.key[0],
                completed_tasks=self.completed)

    def run(self, request, source, output, deadline, gpu_control):
        from .worker_client import RemoteWorkerError
        task_id = request["task_id"]
        errors, active_phase = [], False
        self.process.stdin.write(json.dumps({"request": str(source), "output": str(output)}) + "\n")
        self.process.stdin.flush()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(self.key[0] + "_timeout")
            try:
                record = self.messages.get(timeout=min(.25, remaining))
            except queue.Empty:
                if self.process.poll() is None:
                    continue
                record = None
            if record is None:
                break
            if record.get("task_id") != task_id:
                raise ValueError("persistent_worker_task_mismatch")
            if record.get("type") == "video_filter_gpu":
                response = {"sequence": record.get("sequence")}
                try:
                    response["granted"] = bool(gpu_control(record))
                except Exception:
                    self.process.stdin.write(json.dumps({**response, "error": "gpu_phase_control_failed"}) + "\n")
                    self.process.stdin.flush()
                    raise
                if record["action"] == "acquire" and response["granted"]:
                    active_phase = True
                elif record["action"] == "release":
                    active_phase = False
                self.process.stdin.write(json.dumps(response) + "\n")
                self.process.stdin.flush()
            elif record.get("type") == "video_filter_done":
                if record.get("success") is not True:
                    break
                if active_phase or errors:
                    raise ValueError("persistent_worker_not_idle")
                if any(member["pid"] != self.process.pid for member in self.tree.identities()):
                    raise ValueError("persistent_worker_descendants_remaining")
                self.completed += 1
                return
            elif record.get("type") == "video_filter_log":
                if isinstance(record.get("performance"), dict):
                    metric = dict(record["performance"])
                    event = metric.pop("event", None)
                    if isinstance(event, str):
                        metric["task_id"] = task_id
                        log_performance(event, **metric)
                elif isinstance(record.get("message"), str):
                    level = record.get("level")
                    if level in ("ERROR", "CRITICAL"):
                        errors.append(record["message"])
                    elif level in ("DEBUG", "INFO", "WARNING"):
                        logger.log(getattr(logging, level), "worker | task_id=%s | %s", task_id, record["message"],
                            extra={"video_filter_progress": record.get("progress")})
        try:
            self.stderr.seek(0, 2)
            self.stderr.seek(max(0, self.stderr.tell() - 65536))
            diagnostic = self.stderr.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            diagnostic = "Worker stream closed."
        raise ValueError(self.key[0] + "_worker_failed") from RemoteWorkerError(
            "\n".join(errors) or diagnostic or "Worker exited without a Python traceback.")


def reap_idle():
    with _condition:
        for worker in list(_workers):
            if not worker.busy and (worker.process.poll() is not None or time.monotonic() - worker.last_used >= worker.idle_seconds):
                worker.close()
                _workers.remove(worker)
        _condition.notify_all()


def shutdown():
    global _closed
    with _condition:
        _closed = True
        for worker in list(_workers):
            worker.close()
        _workers.clear()
        _condition.notify_all()


def _acquire(key, capacity, environment, gate, deadline):
    with _condition:
        while True:
            if _closed:
                raise ValueError("persistent_worker_pool_closed")
            reap_idle()
            compatible = next((w for w in _workers if not w.busy and w.key == key), None)
            if compatible:
                compatible.busy = True
                return compatible
            same_kind = [w for w in _workers if w.key[0] == key[0]]
            if len(same_kind) >= capacity:
                obsolete = next((w for w in same_kind if not w.busy and w.key != key), None)
                if obsolete:
                    _workers.remove(obsolete)
                    obsolete.close()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(key[0] + "_timeout")
                _condition.wait(min(.25, remaining))
                continue
            worker = PersistentWorker(key, environment, gate)
            _workers.append(worker)
            return worker


def run(request, timeout, *, source, output, environment, kind, gpu_control, load):
    from .worker_client import _trees, _tree_lock
    started, worker, reusable = time.monotonic(), None, False
    key = (kind, request["device"], request.get("worker_cpu_threads", 1), request.get("worker_model_cache_mib", 768),
           request.get("feature_signature") if kind == "extraction" else None)
    try:
        worker = _acquire(key, request.get("persistent_worker_capacity", 6), environment,
            Path(environment["VIDEO_FILTER_WORKER_GATE"]), started + timeout)
        with _tree_lock:
            _trees[request["task_id"]] = worker.tree
        log_performance("worker_started", task_id=request["task_id"], kind=kind, worker_pid=worker.process.pid,
            reused=worker.completed > 0, elapsed_seconds=round(time.monotonic() - started, 4))
        worker.run(request, source, output, started + timeout, gpu_control)
        loaded_at = time.monotonic()
        result = load(output)
        log_performance("worker_output_loaded", task_id=request["task_id"], kind=kind,
            elapsed_seconds=round(time.monotonic() - loaded_at, 4))
        reusable = worker.completed < request.get("worker_max_tasks", 100)
    finally:
        # A failed task keeps its tree/lease until every descendant is dead.
        if worker is not None and not reusable:
            worker.close()
        with _tree_lock:
            _trees.pop(request["task_id"], None)
        try:
            gpu_control({"action": "cleanup"})
        except Exception:
            reusable = False
            if worker:
                worker.close()
            raise
        finally:
            if worker:
                with _condition:
                    if reusable and not _closed:
                        worker.busy, worker.last_used = False, time.monotonic()
                        worker.idle_seconds = request.get("worker_idle_seconds", 120)
                    elif worker in _workers:
                        _workers.remove(worker)
                    _condition.notify_all()
    log_performance("worker_finished", task_id=request["task_id"], kind=kind, worker_pid=worker.process.pid,
        persistent=True, elapsed_seconds=round(time.monotonic() - started, 4))
    return result


atexit.register(shutdown)
