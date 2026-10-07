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
from video_filter.models import Asset, ScanRun, Task
from video_filter.tasks import enqueue_task
from video_filter.group_config import ROLES
from video_filter.process_tree import ProcessTree
from env import EnvConfig


class GroupRuntimeTests(DatabaseTestCase):
    def test_completion_wakeup_uses_existing_job_and_coalesces_during_tick(self):
        from video_filter import supervisor
        supervisor._tick_active.clear()
        supervisor._wake_requested.clear()
        with patch("app.scheduler") as scheduler:
            scheduler.running = True
            supervisor.wake_control(app)
            self.assertTrue(supervisor._wake_requested.is_set())
            scheduler.modify_job.assert_called_once()
            self.assertEqual("video_filter_process_one", scheduler.modify_job.call_args.args[0])
            for _ in range(5):
                supervisor.wake_control(app)
            self.assertEqual(1, scheduler.modify_job.call_count)
            supervisor._tick_active.set()
            for _ in range(5):
                supervisor.wake_control(app)
            self.assertEqual(1, scheduler.modify_job.call_count)
            supervisor._tick_active.clear()
        supervisor._wake_requested.clear()

    def test_completion_during_control_round_requests_one_followup(self):
        from video_filter import supervisor
        def complete(*args):
            supervisor.wake_control(app)
            supervisor.wake_control(app)
        with patch("app.scheduler") as scheduler, patch.object(supervisor, "_tick", side_effect=complete):
            scheduler.running = True
            supervisor.tick(app, self.settings)
            scheduler.modify_job.assert_called_once()
        supervisor._wake_requested.clear()

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
        supervisor._stopping.clear()
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
        self.exercise_six_slots(self.groups, 4)

    def test_single_group_fills_six_slots_in_one_tick(self):
        self.exercise_six_slots(self.groups[:1], 7)

    def test_oom_degrades_and_successes_restore_effective_concurrency(self):
        from concurrent.futures import Future
        from video_filter import supervisor
        group = self.groups[0]
        identifiers, futures = [], []
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            config.status, config.active_slot = "active", "active"
            db.session.add(ScanRun(config_revision_id=config.id, complete=True))
            for index in range(3):
                task = enqueue_task(db.session, "extract", config.id, {"fixture": "oom", "index": index})
                task.status, task.attempts = "failed" if index == 0 else "running", 3
                task.error_code = "gpu_out_of_memory" if index == 0 else None
                future = Future()
                if index == 0:
                    future.set_result(None)
                identifiers.append(task.id)
                futures.append(future)
                supervisor._running[task.id] = {"group": group, "kind": "extract", "future": future,
                    "lease": None, "resource_mode": None, "gpu_concurrency_at_oom": 3 if index == 0 else None}
            db.session.commit()
        supervisor._scans[group["name"]] = time.monotonic()
        supervisor._effective_limit = 3
        with patch("video_filter.runtime.enqueue_automatic"), patch("video_filter.supervisor._dispatch", return_value=False), \
                patch("video_filter.supervisor.DirectoryWatch") as watch:
            watch.return_value.poll.return_value = (False, False)
            supervisor.tick(app, {**self.settings, "groups": [group]})
            self.assertEqual(2, supervisor._effective_limit)
            self.assertNotIn(identifiers[0], supervisor._running)
            with group_scope(db.session, group):
                db.session.get(Task, identifiers[1]).status = "succeeded"
                db.session.commit()
            futures[1].set_result(None)
            supervisor._recovery_successes = 3
            supervisor._last_oom = time.monotonic() - 61
            supervisor.tick(app, {**self.settings, "groups": [group]})
            self.assertEqual(3, supervisor._effective_limit)

    def exercise_six_slots(self, groups, count):
        from video_filter import supervisor
        from media_lineage.resources import gpu_identity
        for group in groups:
            with group_scope(db.session, group) as scoped:
                config = record_configuration(db.session, scoped)
                config.status, config.active_slot = "active", "active"
                db.session.commit()
                for index in range(count):
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
                supervisor.tick(app, {**self.settings, "groups": groups})
                deadline = time.monotonic() + 5
                while len(started) < 6 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(6, len(started))
                self.assertEqual({group["name"] for group in groups}, {name for identifier, name in started})
                self.assertEqual(3 if len(groups) == 2 else 6, sum(name == "alpha" for _, name in started))
                self.assertEqual(6, len(supervisor._running))
            finally:
                release.set()
                for item in list(supervisor._running.values()):
                    item["future"].result(timeout=10)

    def test_new_lineage_schedules_only_affected_group_before_scan_interval(self):
        from concurrent.futures import Future
        from uuid import uuid4
        from media_lineage.models import LineageEvent
        from video_filter import supervisor
        for group in self.groups:
            with group_scope(db.session, group) as scoped:
                config = record_configuration(db.session, scoped)
                config.status, config.active_slot = "active", "active"
                db.session.add(ScanRun(config_revision_id=config.id, complete=True))
                db.session.commit()
                if group["name"] == "alpha":
                    db.session.add(LineageEvent(operation_id=str(uuid4()), sequence=0, producer="video_filter",
                        generation=str(uuid4()), phase="planned", source_sha256="a" * 64,
                        source_path=str(group["directories"]["unclassified"] / "fixture.mp4"),
                        destination_path=str(group["directories"]["predicted_like"] / "fixture.mp4"),
                        evidence={"dataset_group_id": scoped["dataset_group_id"], "reset_epoch": scoped["reset_epoch"]}))
                    db.session.commit()
            supervisor._scans[group["name"]] = time.monotonic()
        future = Future()
        future.set_result(None)
        with patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.supervisor.DirectoryWatch") as watch, \
                patch.object(supervisor._control_pool, "submit", return_value=future) as submit:
            watch.return_value.poll.return_value = (False, False)
            supervisor.tick(app, self.settings)
        submit.assert_called_once()
        self.assertEqual("alpha", submit.call_args.args[2]["name"])

    def test_shutdown_submission_returns_unstarted_task_and_releases_gpu_lease(self):
        from media_lineage.models import ResourceLease
        from sqlalchemy import select
        from video_filter import supervisor
        for group in self.groups:
            with group_scope(db.session, group) as scoped:
                config = record_configuration(db.session, scoped)
                config.status, config.active_slot = "active", "active"
                db.session.add(ScanRun(config_revision_id=config.id, complete=True))
                db.session.commit()
                if group["name"] == "alpha":
                    task = enqueue_task(db.session, "extract", config.id, {"fixture": "shutdown"})
                    db.session.commit()
                    identifier = task.id
            supervisor._scans[group["name"]] = time.monotonic()
        with patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.supervisor.DirectoryWatch") as watch, \
                patch("media_lineage.resources.gpu_identity", return_value="cpu"), \
                patch.object(supervisor._pool, "submit", side_effect=RuntimeError("cannot schedule new futures after shutdown")), \
                patch("video_filter.supervisor.log_failure") as failures:
            watch.return_value.poll.return_value = (False, False)
            supervisor.tick(app, self.settings)
        failures.assert_not_called()
        self.assertTrue(supervisor._stopping.is_set())
        with group_scope(db.session, self.groups[0]):
            task = db.session.get(Task, identifier)
            self.assertEqual(("queued", 0, None, None), (task.status, task.attempts, task.claim_token, task.execution_owner))
        self.assertEqual([], list(db.session.scalars(select(ResourceLease.status))))
        self.assertNotIn(identifier, supervisor._running)
        with patch.object(db.session, "execute", side_effect=AssertionError("shutdown tick accessed DB")):
            supervisor.tick(app, self.settings)

    def test_control_executor_shutdown_is_deferred_but_other_runtime_errors_propagate(self):
        from video_filter import supervisor
        with patch.object(supervisor._control_pool, "submit", side_effect=RuntimeError("unexpected executor fault")):
            with self.assertRaisesRegex(RuntimeError, "unexpected executor fault"):
                supervisor._submit(supervisor._control_pool, lambda: None)
        self.assertFalse(supervisor._stopping.is_set())
        with patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.supervisor.DirectoryWatch") as watch, \
                patch.object(supervisor._control_pool, "submit", side_effect=RuntimeError("cannot schedule new futures after interpreter shutdown")), \
                patch("video_filter.supervisor.log_failure") as failures:
            watch.return_value.poll.return_value = (False, False)
            supervisor.tick(app, self.settings)
        failures.assert_not_called()
        self.assertTrue(supervisor._stopping.is_set())
        self.assertEqual({}, supervisor._controls)

    def test_expected_input_changes_cancel_but_real_failures_keep_error_trace(self):
        from video_filter import supervisor
        from video_filter.identity import SourceMissing
        from video_filter.tasks import claim_task
        group = self.groups[0]
        for index, (kind, error, expected) in enumerate((
                ("train", ValueError("training_dataset_changed"), True),
                ("predict", ValueError("active_compatible_model_required"), True),
                ("extract", SourceMissing("source_missing"), True),
                ("extract", FileNotFoundError("missing model artifact"), False),
                ("predict", ValueError("model_checksum_mismatch"), False))):
            with self.subTest(kind=kind, error=type(error).__name__, expected=expected):
                with group_scope(db.session, group) as scoped:
                    config = record_configuration(db.session, scoped)
                    db.session.commit()
                    task = enqueue_task(db.session, kind, config.id, {"fixture": index})
                    db.session.commit()
                    task = claim_task(db.session, task.id)
                    identifier, token = task.id, task.claim_token
                with patch("video_filter.runtime.execute", side_effect=error), \
                        patch("video_filter.supervisor.log_failure") as failures:
                    supervisor._run(app, group, identifier, token, None)
                with group_scope(db.session, group):
                    self.assertEqual("cancelled" if expected else "failed", db.session.get(Task, identifier).status)
                if expected:
                    failures.assert_not_called()
                else:
                    failures.assert_called_once()

    def test_transaction_deadlock_rolls_back_partial_work_and_retries_at_most_three_times(self):
        from sqlalchemy import select, func
        from sqlalchemy.exc import OperationalError
        from video_filter import supervisor
        from video_filter.tasks import claim_task
        class Deadlock(Exception):
            pgcode = "40P01"
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            db.session.commit()
            task = enqueue_task(db.session, "predict", config.id, {"fixture": "deadlock"})
            db.session.commit()
            identifier = task.id
        def fail(session, settings, task):
            session.add(Asset(label=0))
            session.flush()
            raise OperationalError("INSERT fixture", {}, Deadlock("synthetic transaction conflict"), hide_parameters=True)
        for attempt in range(1, 4):
            with group_scope(db.session, group):
                task = claim_task(db.session, identifier)
                token = task.claim_token
            with patch("video_filter.runtime.execute", side_effect=fail), \
                    patch("video_filter.supervisor.log_failure") as failures:
                supervisor._run(app, group, identifier, token, None)
            failures.assert_called_once()  # Each failed transaction retains a full error trace.
            with group_scope(db.session, group):
                task = db.session.get(Task, identifier)
                self.assertEqual(attempt, task.attempts)
                self.assertEqual("queued" if attempt < 3 else "failed", task.status)
                self.assertEqual("database_deadlock", task.error_code)
                self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Asset)))
                if attempt < 3:
                    self.assertIsNone(task.claim_token)
                    self.assertIsNone(task.finished_at)

    def test_pending_feedback_requeues_classification_without_consuming_attempt(self):
        from video_filter import supervisor
        from video_filter.tasks import claim_task
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            db.session.commit()
            task = enqueue_task(db.session, "classify", config.id, {"fixture": "feedback-wait"})
            db.session.commit()
            task = claim_task(db.session, task.id)
            identifier, token = task.id, task.claim_token
        with patch("video_filter.runtime.execute", side_effect=ValueError("unresolved_feedback_journal")), \
                patch("video_filter.supervisor.log_failure") as failures:
            supervisor._run(app, group, identifier, token, None)
        failures.assert_not_called()
        with group_scope(db.session, group):
            task = db.session.get(Task, identifier)
            self.assertEqual(("queued", 0, "unresolved_feedback_journal"), (task.status, task.attempts, task.error_code))
            self.assertIsNone(task.claim_token)
            self.assertIsNone(task.finished_at)
            self.assertIsNone(task.execution_owner)

    def test_classify_with_pending_feedback_is_not_dispatched_or_claimed(self):
        from video_filter import supervisor
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            db.session.commit()
            task = enqueue_task(db.session, "classify", config.id, {"fixture": "pending-journal"})
            db.session.commit()
            root = scoped["state_directory"] / "feedback"
            root.mkdir(parents=True)
            (root / "fixture.json").write_text("{}", encoding="utf-8")
            with patch("video_filter.supervisor._submit", side_effect=AssertionError("must not start worker")):
                self.assertFalse(supervisor._dispatch(app, db.session, group, scoped, self.settings, {}))
            db.session.refresh(task)
            self.assertEqual(("queued", 0, None), (task.status, task.attempts, task.claim_token))

    def test_owned_process_tree_terminates_child(self):
        # Real synthetic processes exercise the native Job Object. No media/DB.
        script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                                   start_new_session=os.name != "nt")
        tree = ProcessTree(process)
        try:
            child = int(process.stdout.readline())
            identities = tree.identities()
            self.assertTrue({process.pid, child}.issubset({item["pid"] for item in identities}))
            self.assertTrue(all(item["identity"] for item in identities))
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

    def test_shadow_mil_prediction_waits_for_exclusive_gpu_then_receives_shared_lease(self):
        from video_filter import supervisor
        from media_lineage.resources import request_lease, release
        from media_lineage.models import ResourceLease
        from sqlalchemy import select
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            config.status, config.active_slot = "active", "active"
            db.session.commit()
            task = enqueue_task(db.session, "predict", config.id, {
                "model_ids": {"logistic_regression": "fixture-lr", "mil": "fixture-mil"},
                "selected_classifier": "logistic_regression"})
            db.session.commit()
            identifier = task.id
        exclusive = request_lease(db.session, "cpu", "exclusive_train", "fixture-exclusive")
        started, finish = threading.Event(), threading.Event()
        seen = []
        def fake_execute(session, settings, task):
            self.assertFalse(settings["resource_granted"])
            started.set()
            command = {"action": "acquire", "phase": "mil_predict", "batch_size": 1}
            while not settings["gpu_control"](command):
                if finish.wait(.01):
                    return
            seen.append(supervisor._running[task.id]["lease"])
            if not finish.wait(10):
                raise RuntimeError("prediction_test_did_not_release")
            settings["gpu_control"]({**command, "action": "release"})
        with patch("video_filter.runtime.execute", side_effect=fake_execute), patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.tracking.reconcile"), patch("video_filter.supervisor.DirectoryWatch") as watch, \
                patch("media_lineage.resources.gpu_identity", return_value="cpu"):
            watch.return_value.poll.return_value = (False, False)
            try:
                supervisor.tick(app, self.settings)
                self.assertTrue(started.wait(5))
                deadline = time.monotonic() + 5
                waiting = None
                while waiting is None and time.monotonic() < deadline:
                    waiting = db.session.scalar(select(ResourceLease).where(ResourceLease.status == "waiting"))
                    time.sleep(.01)
                self.assertEqual("extract_shared", waiting.mode)
                self.assertEqual([], seen)
                release(db.session, exclusive)
                supervisor.tick(app, self.settings)
                deadline = time.monotonic() + 5
                while not seen and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(seen)
                lease = db.session.get(ResourceLease, seen[0], populate_existing=True)
                self.assertEqual(("extract_shared", "active"), (lease.mode, lease.status))
            finally:
                finish.set()
                if identifier in supervisor._running:
                    supervisor._running[identifier]["future"].result(timeout=10)

    def test_classify_releases_gpu_while_future_keeps_cpu_stage_running(self):
        from video_filter import supervisor
        from media_lineage.models import ResourceLease
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            config.status, config.active_slot = "active", "active"
            db.session.add(ScanRun(config_revision_id=config.id, complete=True))
            task = enqueue_task(db.session, "classify", config.id, {"model_ids": {"mil": "fixture-mil"}})
            db.session.commit()
            identifier = task.id
        supervisor._scans[group["name"]] = time.monotonic()
        released, finish, seen = threading.Event(), threading.Event(), []
        def execute(session, settings, task):
            self.assertIsNone(settings["resource_lease_id"])
            command = {"action": "acquire", "phase": "mil_predict", "batch_size": 1}
            self.assertTrue(settings["gpu_control"](command))
            seen.append(supervisor._running[task.id]["lease"])
            settings["gpu_control"]({**command, "action": "release"})
            settings["release_gpu"]()
            self.assertFalse(settings["resource_granted"])
            released.set()
            if not finish.wait(10):
                raise RuntimeError("test CPU stage did not finish")
        with patch("video_filter.runtime.execute", side_effect=execute), patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.supervisor.DirectoryWatch") as watch, patch("media_lineage.resources.gpu_identity", return_value="cpu"):
            watch.return_value.poll.return_value = (False, False)
            try:
                supervisor.tick(app, {**self.settings, "groups": [group]})
                self.assertTrue(released.wait(5))
                self.assertIsNone(supervisor._running[identifier]["lease"])
                self.assertEqual("released", db.session.get(ResourceLease, seen[0], populate_existing=True).status)
                supervisor.tick(app, {**self.settings, "groups": [group]})
                self.assertEqual(1, supervisor.snapshot()["other_running"])
                self.assertFalse(supervisor._running[identifier]["future"].done())
            finally:
                finish.set()
                if identifier in supervisor._running:
                    supervisor._running[identifier]["future"].result(timeout=10)

    def test_phase_oom_keeps_active_lease_until_process_cleanup_and_learns_budget(self):
        from video_filter import supervisor
        from media_lineage.models import ResourceLease
        group = self.groups[0]
        with group_scope(db.session, group) as scoped:
            config = record_configuration(db.session, scoped)
            config.status, config.active_slot = "active", "active"
            db.session.add(ScanRun(config_revision_id=config.id, complete=True))
            task = enqueue_task(db.session, "extract", config.id, {"fixture": "phase-oom"})
            db.session.commit()
            identifier = task.id
        supervisor._scans[group["name"]] = time.monotonic()
        seen = []
        def execute(session, settings, task):
            command = {"action": "acquire", "phase": "beats", "batch_size": 8}
            self.assertTrue(settings["gpu_control"](command))
            lease = supervisor._running[task.id]["lease"]
            settings["gpu_control"]({**command, "action": "failed", "oom": True})
            row = session.get(ResourceLease, lease, populate_existing=True)
            self.assertEqual(("active", 1536), (row.status, row.memory_budget_mib))
            self.assertEqual(lease, supervisor._running[task.id]["lease"])
            settings["gpu_control"]({"action": "cleanup"})
            self.assertIsNone(supervisor._running[task.id]["lease"])
            self.assertEqual("released", session.get(ResourceLease, lease, populate_existing=True).status)
            seen.append(lease)
            raise RuntimeError("fixture out of memory")
        with patch("video_filter.runtime.execute", side_effect=execute), patch("video_filter.runtime.enqueue_automatic"), \
                patch("video_filter.supervisor.log_failure"), patch("video_filter.supervisor.DirectoryWatch") as watch, \
                patch("media_lineage.resources.gpu_identity", return_value="cpu"):
            watch.return_value.poll.return_value = (False, False)
            supervisor.tick(app, {**self.settings, "groups": [group]})
            supervisor._running[identifier]["future"].result(timeout=10)
        self.assertEqual(1, len(seen))
        self.assertEqual(("released", 1536), (db.session.get(ResourceLease, seen[0], populate_existing=True).status,
                                              db.session.get(ResourceLease, seen[0]).memory_budget_mib))
        with group_scope(db.session, group):
            self.assertEqual("gpu_out_of_memory", db.session.get(Task, identifier).error_code)
