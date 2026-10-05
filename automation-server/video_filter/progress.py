"""Bounded, metadata-only live progress transport; durable task state stays in DB."""

import json
import logging
import math
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

FIELDS = {"stage", "modality", "completed", "total", "elapsed_seconds", "epoch", "epochs",
          "training_loss", "validation_loss", "status"}
TEXT_FIELDS = {"stage", "modality", "status"}


def _valid_field(key, value):
    if key in TEXT_FIELDS:
        return isinstance(value, str) and len(value) <= 96
    return key in FIELDS and type(value) in (int, float) and math.isfinite(value)


def extra(task_id, stage, **fields):
    return {"video_filter_progress": {"task_id": task_id, "stage": stage, **fields}}


def _path(state, task_id):
    if not isinstance(task_id, str) or str(UUID(task_id)) != task_id:
        raise ValueError("Canonical progress task ID required.")
    return Path(state) / "progress" / (task_id + ".json")


def read_progress(state, task_id):
    try:
        path = _path(state, task_id)
        if path.stat().st_size > 16384:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("task_id") != task_id:
            return None
        if set(value) - (FIELDS | {"task_id", "updated_at"}):
            return None
        if not isinstance(value.get("updated_at"), str) or len(value["updated_at"]) > 64:
            return None
        if any(not _valid_field(key, field) for key, field in value.items() if key in FIELDS):
            return None
        return value
    except (OSError, ValueError):
        return None


class ProgressHandler(logging.Handler):
    def __init__(self, state, task_id):
        super().__init__()
        self.path, self.task_id = _path(state, task_id), task_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write({"stage": "准备执行", "status": "running"})

    def write(self, fields):
        # Replace atomically under the handler lock; HTTP readers never see a
        # partially written file. No filenames, raw logs or features are saved.
        self.acquire()
        try:
            payload = {"task_id": self.task_id, "updated_at": datetime.now(timezone.utc).isoformat()}
            for key, value in fields.items():
                if _valid_field(key, value):
                    payload[key] = value
            temporary = self.path.with_suffix(".pending")
            temporary.write_text(json.dumps(payload, ensure_ascii=True, allow_nan=False), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            self.release()

    def emit(self, record):
        value = getattr(record, "video_filter_progress", None)
        if isinstance(value, dict) and value.get("task_id") == self.task_id:
            try:
                self.write(value)
            except OSError as error:
                from .observability import log_failure
                log_failure(logging.getLogger(__name__), "实时进度写入失败，任务继续执行", error)


@contextmanager
def track_task(state, task_id):
    logger = logging.getLogger("video_filter")
    handler = None
    try:
        handler = ProgressHandler(state, task_id)
        logger.addHandler(handler)
    except OSError as error:
        from .observability import log_failure
        log_failure(logger, "实时进度初始化失败，任务继续执行", error)
    try:
        yield handler
    finally:
        if handler:
            logger.removeHandler(handler)
            handler.close()
