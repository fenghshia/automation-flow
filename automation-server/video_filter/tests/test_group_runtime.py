import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from .support import DatabaseTestCase, app, db
from video_filter import register_video_filter
from video_filter.scope import group_scope, current_scope
from video_filter.configuration import record_configuration
from video_filter.models import Asset, Task
from video_filter.tasks import enqueue_task
from video_filter.group_config import ROLES
from video_filter.process_tree import ProcessTree
from env import EnvConfig


class GroupRuntimeTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        register_video_filter()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.groups = []
        for name in ("alpha", "beta"):
            roots = {role: self.root / name / role for role in ROLES}
            for root in roots.values():
                root.mkdir(parents=True)
            group = {"enabled": True, "grouped": True, "name": name, "directories": roots,
                     "state_directory": self.root / "state", "model_manifest": None,
                     "classifier": "logistic_regression", "transfer_enabled": False,
                     "compression_enabled": False, "scan_interval_seconds": 60, "stable_seconds": 1,
                     "missing_seconds": 2, "task_timeout_seconds": 30, "device": "cpu"}
            self.groups.append(group)
            with group_scope(db.session, group):
                asset = Asset(label=1 if name == "alpha" else 0)
                db.session.add(asset)
                db.session.commit()
        self.settings = {"enabled": True, "grouped": True, "groups": self.groups,
                         "state_directory": self.root / "state", "extract_concurrency": 6}

    def tearDown(self):
        from video_filter import supervisor
        for future in supervisor._controls.values():
            future.result(timeout=10)
        supervisor._controls.clear()
        supervisor._uncertainty.clear()
        for watches in supervisor._watches.values():
            for watch in watches[1]:
                watch.close()
        supervisor._watches.clear()
        supervisor._scans.clear()
        supervisor._running.clear()
        supervisor._effective_limit = None
        self.temp.cleanup()
        super().tearDown()

    def test_group_routes_and_readonly_dashboard_do_not_merge_assets(self):
        from sqlalchemy import select, func
        from video_filter.models import DatasetGroup
        before = db.session.scalar(select(func.count()).select_from(DatasetGroup))
        def configuration(ignore_scope=False):
            scope = current_scope()
            return scope["settings"] if scope and not ignore_scope else self.settings
        with patch.object(EnvConfig, "video_filter_settings", side_effect=configuration):
            client = app.test_client()
            self.assertEqual(409, client.post("/video_filter/scan", json={}).status_code)
            self.assertEqual(1, client.get("/video_filter/groups/alpha/status").json["counts"]["positive_assets"])
            self.assertEqual(0, client.get("/video_filter/groups/beta/status").json["counts"]["positive_assets"])
            self.assertEqual(404, client.get("/video_filter/groups/unknown/status").status_code)
            self.assertEqual(2, len(client.get("/video_filter/groups").json["groups"]))
            self.assertEqual([], client.get("/video_filter/groups/alpha/tasks").json["tasks"])
            page = client.get("/video_filter/dashboard/?group=alpha")
            self.assertEqual(200, page.status_code)
            self.assertIn("group=alpha", page.get_data(as_text=True))
            self.assertEqual(200, client.get("/video_filter/dashboard/fragment?group=beta").status_code)
        self.assertEqual(before, db.session.scalar(select(func.count()).select_from(DatasetGroup)))
        self.assertEqual(0, db.session.query(Task).count())

    def test_six_subprocess_chains_overlap_and_seventh_waits(self):
        from video_filter import supervisor
        from media_lineage.resources import gpu_identity
        for group in self.groups:
            with group_scope(db.session, group) as scoped:
                config = record_configuration(db.session, scoped)
                config.status, config.active_slot = "active", "active"
                db.session.commit()
                for index in range(4):
                    enqueue_task(db.session, "extract", config.id, {"fixture": index})
                db.session.commit()
        started, release, guard = [], threading.Event(), threading.Lock()
        def fake_execute(session, settings, task):
            with guard:
                started.append((task.id, settings["name"]))
            # Simulated worker lifetime; the scheduler must not block here.
            if not release.wait(10):
                raise RuntimeError("test_supervisor_did_not_fill_slots")
        with patch("video_filter.runtime.execute", side_effect=fake_execute), patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.tracking.reconcile"), patch("video_filter.supervisor.DirectoryWatch") as watch, \
                patch("media_lineage.resources.gpu_identity", return_value="cpu"):
            watch.return_value.poll.return_value = (False, False)
            try:
                for _ in range(4):
                    supervisor.tick(app, self.settings)
                deadline = time.monotonic() + 5
                while len(started) < 6 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(6, len(started))
                self.assertEqual({"alpha", "beta"}, {name for identifier, name in started})
                self.assertEqual(6, len(supervisor._running))
            finally:
                release.set()
                for item in list(supervisor._running.values()):
                    item["future"].result(timeout=10)

    def test_owned_process_tree_terminates_child(self):
        # Real synthetic processes exercise the native Job Object. No media/DB.
        script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                                   start_new_session=os.name != "nt")
        tree = ProcessTree(process)
        try:
            child = int(process.stdout.readline())
            tree.terminate()
            process.wait(timeout=10)
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes as w
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.restype = w.HANDLE
                kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
                kernel.CloseHandle.argtypes = [w.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, child)
                if handle:
                    try:
                        self.assertEqual(0, kernel.WaitForSingleObject(handle, 5000))
                    finally:
                        kernel.CloseHandle(handle)
        finally:
            tree.terminate()
            tree.close()
            process.wait(timeout=10)
            process.stdout.close()
