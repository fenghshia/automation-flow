"""Temporary worker transport; committed feature storage remains the database."""

import json
import hashlib
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .feature_store import PreparedBundle, FeatureStore
from .observability import redact_paths
from .process_tree import ProcessTree
import time
from logging_config import log_performance

_trees = {}
_tree_lock = threading.Lock()


def cancel_worker(task_id):
    with _tree_lock:
        tree = _trees.get(task_id)
        if tree:
            tree.terminate()


logger = logging.getLogger(__name__)


def worker_processes(task_id):
    with _tree_lock:
        tree = _trees.get(task_id)
        try:
            return tree.identities() if tree else []
        except OSError:
            return []  # Unknown ownership keeps the conservative reservation.


class RemoteWorkerError(RuntimeError):
    """Carry the original worker traceback as the cause of a local task error."""


def _run_worker(request, timeout, *, module, prefix, prepare=None, load=None):
    started = time.monotonic()
    state = Path(request["state_directory"])
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix + "-", dir=state) as temporary:
        root = Path(temporary)
        source, output = root / "request.json", root / "output"
        request = dict(request)
        gpu_control = request.pop("gpu_control", None)
        request["phase_managed"] = callable(gpu_control)
        if prepare is not None:
            prepare(root, request)
        log_performance("worker_input_prepared", task_id=request.get("task_id"), kind=prefix,
            elapsed_seconds=round(time.monotonic() - started, 4))
        source.write_text(json.dumps(request), encoding="utf-8")
        environment = os.environ.copy()
        environment["MKL_THREADING_LAYER"] = "SEQUENTIAL"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["VIDEO_FILTER_WORKER_GATE"] = str(root / "gate")
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            environment[key] = str(request.get("worker_cpu_threads", 1))
        redact_paths([root, request.get("path"), request.get("state_directory"), request.get("model_manifest"), request.get("ffmpeg_directory")])
        if request.get("persistent_workers") and callable(gpu_control) and prefix in ("extraction", "prediction"):
            from .persistent_client import run
            return run(request, timeout, source=source, output=output, environment=environment,
                kind=prefix, gpu_control=gpu_control, load=load)
        remote_errors, transport_errors = [], []
        # Drain progress while the worker runs; stderr has its own temporary sink
        # so library warnings cannot fill a pipe or mix with protocol messages.
        with tempfile.TemporaryFile() as stderr:
            with subprocess.Popen([sys.executable, "-m", module, "--request", str(source),
                    "--output", str(output)], cwd=Path(__file__).parents[1], env=environment,
                    stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8", errors="replace",
                    start_new_session=os.name != "nt") as process:
                tree = ProcessTree(process)
                with _tree_lock:
                    _trees[request.get("task_id")] = tree
                log_performance("worker_started", task_id=request.get("task_id"), kind=prefix,
                    worker_pid=process.pid if type(process.pid) is int else None,
                    elapsed_seconds=round(time.monotonic() - started, 4))
                (root / "gate").touch()
                def forward():
                    try:
                        for line in process.stdout:
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(record, dict) and record.get("type") == "video_filter_gpu" and record.get("task_id") == request.get("task_id"):
                                if not gpu_control:
                                    raise ValueError("unexpected_gpu_phase_request")
                                response = {"sequence": record.get("sequence")}
                                try:
                                    response["granted"] = bool(gpu_control(record))
                                except Exception as error:
                                    transport_errors.append(error)
                                    response["error"] = "gpu_phase_control_failed"
                                process.stdin.write(json.dumps(response) + "\n")
                                process.stdin.flush()
                                continue
                            if not isinstance(record, dict) or record.get("type") != "video_filter_log" or record.get("task_id") != request.get("task_id"):
                                continue
                            if isinstance(record.get("performance"), dict):
                                metric = dict(record["performance"])
                                event = metric.pop("event", None)
                                if isinstance(event, str):
                                    metric["task_id"] = request.get("task_id")
                                    log_performance(event, **metric)
                                continue
                            level, message = record.get("level"), record.get("message")
                            if not isinstance(message, str):
                                continue
                            if level in ("ERROR", "CRITICAL"):
                                remote_errors.append(message)
                            elif level in ("DEBUG", "INFO", "WARNING"):
                                logger.log(getattr(logging, level), "worker | task_id=%s | %s", request.get("task_id"), message,
                                    extra={"video_filter_progress": record.get("progress")})
                    except Exception as error:
                        transport_errors.append(error)
                        tree.terminate()
                reader = threading.Thread(target=forward, name="video-filter-worker-logs", daemon=True)
                reader.start()
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    tree.terminate()
                    process.wait()
                    reader.join()
                    raise ValueError(prefix + "_timeout") from error
                finally:
                    # Close the job before draining EOF: a descendant may have
                    # inherited stdout even after the top-level worker exits.
                    with _tree_lock:
                        _trees.pop(request.get("task_id"), None)
                        tree.close()
                    if process.poll() is not None:
                        reader.join()
                    if gpu_control:
                        # Release waiting/active leases only after process-tree death.
                        gpu_control({"action": "cleanup"})
                if transport_errors:
                    raise RuntimeError("worker_log_transport_failed") from transport_errors[0]
                if returncode != 0:
                    stderr.seek(0)
                    original = "\n".join(remote_errors) or stderr.read().decode("utf-8", errors="replace") or "Worker exited without a Python traceback."
                    failure = ValueError(prefix + "_worker_failed")
                    failure.diagnostic = {"returncode": returncode}
                    raise failure from RemoteWorkerError(original)
                if remote_errors:
                    logger.error("子进程异常记录 | task_id=%s\n%s", request.get("task_id"), "\n".join(remote_errors))
        log_performance("worker_finished", task_id=request.get("task_id"), kind=prefix,
            returncode=returncode, elapsed_seconds=round(time.monotonic() - started, 4))
        loaded_at = time.monotonic()
        result = load(output)
        log_performance("worker_output_loaded", task_id=request.get("task_id"), kind=prefix,
            elapsed_seconds=round(time.monotonic() - loaded_at, 4))
        return result


def run_extraction(request, timeout):
    def load(output):
        metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
        prepared = PreparedBundle((output / "arrays.npz").read_bytes(), metadata["manifest"], metadata["manifest_sha256"])
        FeatureStore()._decode(prepared)
        return prepared, metadata
    return _run_worker(request, timeout, module="video_filter.worker", prefix="extraction", load=load)


def run_mil_training(bags, y, fit, holdout, settings, task_id=None):
    import numpy as np

    request = {"task_id": task_id or "-", "state_directory": str(settings["state_directory"]),
               "device": settings.get("device", "cuda:0"),
               "epochs": settings.get("mil_epochs", 60), "patience": settings.get("mil_patience", 8),
               "max_train_windows": settings.get("mil_max_train_windows", 512)}
    from .training_config import effective
    request.update(hyperparameters=effective(settings, "mil"), worker_cpu_threads=settings.get("worker_cpu_threads", 1),
        resource_granted=settings.get("resource_granted", False), resource_lease_id=settings.get("resource_lease_id"),
        gpu_control=settings.get("gpu_control"))
    if settings.get("grouped"):
        request.update(dataset_group_id=settings["dataset_group_id"], reset_epoch=settings["reset_epoch"], group_name=settings["name"])
    def prepare(root, values):
        arrays = {"labels": y, "fit": fit, "holdout": holdout}
        for index, (x, valid) in enumerate(bags):
            arrays["x_" + str(index)], arrays["valid_" + str(index)] = x, valid
        path = root / "dataset.npz"
        np.savez_compressed(path, **arrays)
        values["dataset_path"] = str(path)
    return _run_worker(request, settings["task_timeout_seconds"], module="video_filter.training_worker",
                       prefix="training", prepare=prepare,
                       load=lambda output: json.loads((output / "result.json").read_text(encoding="utf-8")))


def run_mil_prediction(blob, bag, settings, task_id=None):
    import numpy as np

    if settings.get("grouped") and not callable(settings.get("gpu_control")) and (not settings.get("resource_granted") or not settings.get("resource_lease_id")):
        raise ValueError("prediction_resource_required")
    request = {"task_id": task_id or "-", "state_directory": str(settings["state_directory"]),
        "device": settings.get("device", "cuda:0"), "model_sha256": hashlib.sha256(blob).hexdigest(),
        "worker_cpu_threads": settings.get("worker_cpu_threads", 1),
        "resource_granted": settings.get("resource_granted", False), "resource_lease_id": settings.get("resource_lease_id"),
        "gpu_control": settings.get("gpu_control")}
    request.update({key: settings[key] for key in ("persistent_workers", "worker_model_cache_mib", "worker_idle_seconds", "worker_max_tasks") if key in settings})
    request["persistent_worker_capacity"] = 2
    if settings.get("grouped"):
        request.update(dataset_group_id=settings["dataset_group_id"], reset_epoch=settings["reset_epoch"], group_name=settings["name"])
    def prepare(root, values):
        model_path, bag_path = root / "model.npz", root / "bag.npz"
        model_path.write_bytes(blob)
        np.savez_compressed(bag_path, x=bag[0], valid=bag[1])
        values.update(model_path=str(model_path), bag_path=str(bag_path))
    def load(output):
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        probability = result.get("probability")
        if (type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1 or
                result.get("backend") != "pytorch" or result.get("device") != request["device"] or
                result.get("model_sha256") != request["model_sha256"]):
            raise ValueError("invalid_prediction_worker_result")
        return float(probability)
    return _run_worker(request, settings["task_timeout_seconds"], module="video_filter.inference_worker",
                       prefix="prediction", prepare=prepare, load=load)
