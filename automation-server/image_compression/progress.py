"""Best-effort live metadata; DB state and manifests remain recovery authorities."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time

from .group_config import PENDING_ROOT, reject_links

logger = logging.getLogger(__name__)
STAGES = {"moving", "collecting", "unpacking", "inspecting", "processing",
          "staging", "publishing", "cleanup_pending", "failed"}
MAX_PROGRESS_BYTES = 16 * 1024
STALE_SECONDS = 90


def _path(root, mission_id):
    if type(mission_id) is not int or mission_id < 1:
        raise ValueError("positive_mission_id_required")
    return reject_links(Path(root) / str(mission_id) / "progress.json")


class ProgressReporter:
    def __init__(self, mission_id, group_name, attempt, pending_root=PENDING_ROOT):
        self.path = _path(pending_root, mission_id)
        self.values = {"mission_id": mission_id, "group_name": group_name,
                       "attempt": attempt, "completed_files": 0, "total_files": None}
        self.last_write = 0.0

    def __call__(self, stage, **fields):
        try:
            if stage not in STAGES:
                return
            changed = self.values.get("stage") != stage
            if changed:
                self.values["current_file"] = None
            self.values.update(stage=stage, **fields)
            current = self.values.get("current_file")
            if current is not None:
                self.values["current_file"] = str(current)[:2048]
            now = time.monotonic()
            finished = (self.values["total_files"] is not None
                        and self.values["completed_files"] == self.values["total_files"])
            if not changed and not finished and now - self.last_write < 0.5:
                return
            self.values["updated_at"] = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(self.values, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_PROGRESS_BYTES:
                return
            reject_links(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = reject_links(self.path.with_suffix(".json.part"))
            temporary.write_bytes(encoded)
            os.replace(temporary, self.path)
            self.last_write = now
        except (OSError, ValueError, TypeError):
            logger.warning("图片进度暂不可用 | event=IMAGE_PROGRESS_UNAVAILABLE")


def read_progress(mission, pending_root=PENDING_ROOT, now=None):
    try:
        path = _path(pending_root, mission.id)
        with path.open("rb") as source:
            encoded = source.read(MAX_PROGRESS_BYTES + 1)
        if len(encoded) > MAX_PROGRESS_BYTES:
            return None
        value = json.loads(encoded)
        if (not isinstance(value, dict) or value.get("mission_id") != mission.id
                or value.get("group_name") != mission.group_name
                or type(value.get("attempt")) is not int or value["attempt"] != mission.attempts
                or value.get("stage") not in STAGES):
            return None
        completed, total = value.get("completed_files"), value.get("total_files")
        current = value.get("current_file")
        if (type(completed) is not int or completed < 0
                or (total is not None and (type(total) is not int or total < 1 or completed > total))
                or (current is not None and (not isinstance(current, str) or len(current) > 2048
                    or Path(current).is_absolute() or Path(current).drive or ".." in Path(current).parts))):
            return None
        updated = datetime.fromisoformat(value["updated_at"])
        if updated.tzinfo is None:
            return None
        age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
        return {**value, "stale": age > STALE_SECONDS or age < -5,
                "percent": round(100 * completed / total, 1)
                if total and value["stage"] == "processing" else None}
    except (OSError, ValueError, TypeError, KeyError):
        return None
