"""Progress and worker log transport, using the shared redacting formatters."""

import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from logging_config import SafeFormatter, register_redaction_values

_status_lock = threading.Lock()
_statuses = {}


def status_log(logger, key, message, *args, interval=60, state=None):
    """Log a state change immediately, and stable states at most once a minute."""
    now = time.monotonic()
    with _status_lock:
        previous = _statuses.get(key)
        current = args if state is None else state
        if previous and previous[0] == current and now - previous[1] < interval:
            return
        _statuses[key] = (current, now)
        if len(_statuses) > 2048:
            oldest = min(_statuses, key=lambda item: _statuses[item][1])
            _statuses.pop(oldest, None)
    logger.info(message, *args)


def log_failure(logger, message, error, *args):
    """Keep every traceback frame, while SQLAlchemy suppresses bound payloads."""
    from sqlalchemy.exc import StatementError

    changed, pending, seen = [], [error], set()
    try:
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, StatementError):
                changed.append((current, current.hide_parameters))
                current.hide_parameters = True
            pending.extend((current.__cause__, current.__context__))
        logger.error(message, *args, exc_info=(type(error), error, error.__traceback__), stacklevel=2)
    finally:
        for current, original in changed:
            current.hide_parameters = original


def redact_paths(values):
    paths = []
    for value in values:
        if value is None:
            continue
        path = Path(value)
        paths.extend((str(path), str(path.resolve()), path.as_posix(), path.resolve().as_posix()))
    register_redaction_values(paths)


class WorkerLogHandler(logging.Handler):
    """Flush JSON lines to the parent; only the parent owns rotating log files."""

    def __init__(self, stream, task_id, context=None):
        super().__init__()
        self.stream, self.task_id = stream, task_id
        self.context = context or {}
        self.setFormatter(SafeFormatter("%(message)s"))

    def emit(self, record):
        payload = {"type": "video_filter_log", "level": record.levelname,
                   "message": self.format(record), "task_id": self.task_id}
        progress = getattr(record, "video_filter_progress", None)
        if isinstance(progress, dict):
            payload["progress"] = progress
        performance = getattr(record, "video_filter_performance", None)
        if isinstance(performance, dict):
            payload["performance"] = {**performance, **self.context}
        self.stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
        self.stream.flush()


def configure_worker_logs(request, stream=None):
    redact_paths([request.get(name) for name in ("path", "state_directory", "model_manifest", "ffmpeg_directory")])
    if request.get("path"):
        register_redaction_values([Path(request["path"]).name])
    if request.get("model_manifest"):
        redact_paths([Path(request["model_manifest"]).parent])
    logger = logging.getLogger("video_filter")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        if isinstance(handler, WorkerLogHandler):
            logger.removeHandler(handler)
            handler.close()
    logger.addHandler(WorkerLogHandler(stream or sys.stdout, request.get("task_id", "-"),
        {key: request[key] for key in ("dataset_group_id", "reset_epoch") if key in request}))


@contextmanager
def progress_phase(logger, phase, *, task_id=None, interval=15):
    """Elapsed-time heartbeats during opaque solver work; no invented percentage."""
    started = time.monotonic()
    from .progress import extra
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(interval):
            logger.info("阶段仍在运行 | task_id=%s | phase=%s | elapsed=%.1fs", task_id or "-", phase, time.monotonic() - started,
                extra=extra(task_id, phase, elapsed_seconds=time.monotonic() - started))
    thread = threading.Thread(target=heartbeat, name="video-filter-progress", daemon=True)
    logger.info("阶段开始 | task_id=%s | phase=%s", task_id or "-", phase, extra=extra(task_id, phase))
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
        logger.info("阶段结束 | task_id=%s | phase=%s | elapsed=%.1fs", task_id or "-", phase, time.monotonic() - started)
