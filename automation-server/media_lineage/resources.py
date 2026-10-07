"""Cross-process GPU budgets and training exclusion shared by media workloads."""

import os
import hashlib
import subprocess
import threading
import time
import logging
from contextlib import contextmanager
from pathlib import Path
from contextvars import ContextVar
from functools import lru_cache
from logging_config import log_performance

_mutex = threading.RLock()
_lease_context = ContextVar("media_resource_context", default=None)
logger = logging.getLogger("video_filter.resources")
_decisions = {}
_decision_logs = {}
_round_samples = ContextVar("media_admission_samples", default=None)
_sample_times = {}


@contextmanager
def admission_round():
    """One fresh driver sample per device per control round, never across rounds."""
    token = _round_samples.set({})
    try:
        yield
    finally:
        _round_samples.reset(token)


def memory_snapshot(device):
    samples = _round_samples.get()
    if samples is None or device not in samples or time.monotonic() - samples[device][0] >= 1:
        available = free_memory(device)
        try:
            memory = process_memory(device)
        except (OSError, ValueError, subprocess.SubprocessError):
            memory = {}
        value = (available, memory)
        if samples is not None:
            samples[device] = (time.monotonic(), value)
        return value
    return samples[device][1]


def sample_gpu(device):
    """Observe utilization even while exclusive work prevents new admission."""
    now = time.monotonic()
    if now - _sample_times.get(device, -10) >= 5:
        memory_snapshot(device)
        _sample_times[device] = now


def current_lease():
    return _lease_context.get()


@contextmanager
def lease_scope(lease_id):
    token = _lease_context.set(lease_id)
    try:
        yield
    finally:
        _lease_context.reset(token)


def gpu_identity(device):
    """Use driver UUIDs so CUDA ordinals and NVENC share one physical resource."""
    if device == "cpu":
        return "cpu"
    ordinal = str(device).removeprefix("cuda:").removeprefix("driver:")
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if str(device).startswith("cuda:") and visible is not None:
        choices = [value.strip() for value in visible.split(",") if value.strip()]
        if int(ordinal) >= len(choices):
            raise ValueError("gpu_driver_identity_unavailable")
        ordinal = choices[int(ordinal)]
    return _driver_identity(ordinal)


@lru_cache(maxsize=16)
def _driver_identity(ordinal):
    result = subprocess.run(["nvidia-smi", "-i", ordinal, "--query-gpu=uuid,memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True)
    fields = result.stdout.strip().split(",")
    if len(fields) != 2 or not fields[0].strip().startswith("GPU-"):
        raise ValueError("gpu_driver_identity_unavailable")
    return fields[0].strip()


def free_memory(device):
    started = time.monotonic()
    result = subprocess.run(["nvidia-smi", "-i", device,
        "--query-gpu=memory.free,memory.used,memory.total,utilization.gpu,utilization.memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True)
    fields = [value.strip() for value in result.stdout.strip().split(",")]
    available = int(fields[0])
    metrics = {key: int(value) if value.isdigit() else None for key, value in zip(
        ("free_mib", "used_mib", "total_mib", "gpu_utilization_percent", "memory_utilization_percent"), fields)}
    log_performance("gpu_sample", device=device, query_seconds=round(time.monotonic() - started, 4), **metrics)
    return available


def process_identity(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise OSError("process_identity_unavailable")
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
                raise OSError("process_identity_unavailable")
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def process_memory(device):
    result = subprocess.run(["nvidia-smi", "-i", device, "--query-compute-apps=pid,used_gpu_memory",
        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True)
    values = {}
    for line in result.stdout.splitlines():
        fields = [value.strip() for value in line.split(",")]
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            values[int(fields[0])] = int(fields[1])
    return values


def _observed(row, memory):
    total = 0
    for child in (row.memory_observation or {}).get("processes", []):
        try:
            if process_identity(child["pid"]) == child["identity"]:
                total += memory.get(child["pid"], 0)
        except OSError:
            pass
    return total


def admission_decisions():
    with _mutex:
        return {key: dict(value) for key, value in _decisions.items()}


def request_lease(session, device, mode, owner, capacity=6, check_memory=True, *,
                  memory_budget_mib=1024, reserve_mib=1024, workload_type="extract", profile_key=None):
    """Short locked admission transaction. Never reclaim an unproved live owner."""
    from sqlalchemy import select, text
    from .models import ResourceLease, utc_now
    with _mutex:
        if session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(hashlib.sha256(device.encode()).digest()[:8], "big", signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        rows = session.execute(select(ResourceLease).where(ResourceLease.device == device,
            ResourceLease.status.in_(("waiting", "active", "conflict"))).order_by(ResourceLease.created_at, ResourceLease.id).with_for_update()
            .execution_options(populate_existing=True)).scalars().all()
        for row in rows:
            if row.status == "waiting" and (utc_now() - row.heartbeat_at).total_seconds() > 300:
                # Waiting requests own no GPU work; abandoned requests may expire.
                row.status = "released"
                continue
            try:
                identity = process_identity(row.pid)
                if identity != row.process_identity:
                    row.status = "released"
            except OSError:
                row.status = "conflict"
        row = next((r for r in rows if r.owner == owner), None)
        if row is not None and row.status == "released":
            row.status, row.created_at = "waiting", utc_now()
            row.pid, row.process_identity = os.getpid(), process_identity(os.getpid())
        if row is None:
            row = session.scalar(select(ResourceLease).where(ResourceLease.owner == owner).execution_options(populate_existing=True))
            if row is None:
                row = ResourceLease(device=device, mode=mode, owner=owner, pid=os.getpid(), process_identity=process_identity(os.getpid()))
                session.add(row)
            else:
                if row.device != device:
                    raise ValueError("resource_device_changed")
                row.status, row.created_at = "waiting", utc_now()
                row.pid, row.process_identity = os.getpid(), process_identity(os.getpid())
            session.flush()
            rows.append(row)
        rows.sort(key=lambda item: (item.created_at, item.id))
        active = [r for r in rows if r.status in ("active", "conflict") and r.id != row.id]
        waiting = [r for r in rows if r.status == "waiting"]
        if row.mode != mode:
            raise ValueError("resource_request_changed")
        if mode == "extract_shared":
            if workload_type not in ("extract", "predict", "compression") or min(memory_budget_mib, reserve_mib) <= 0:
                raise ValueError("invalid_resource_memory_budget")
            observation = dict(row.memory_observation or {})
            if profile_key:
                history = session.scalars(select(ResourceLease).where(ResourceLease.device == device,
                    ResourceLease.status == "released").order_by(ResourceLease.created_at.desc()).limit(64)).all()
                memory_budget_mib = max([memory_budget_mib] + [r.memory_budget_mib or 0 for r in history
                    if (r.memory_observation or {}).get("profile_key") == profile_key and
                    ((r.memory_observation or {}).get("oom") or (r.memory_observation or {}).get("driver_peak_mib"))])
            observation.update(workload_type=workload_type, profile_key=profile_key)
            row.memory_observation = observation
            row.memory_budget_mib = max(row.memory_budget_mib or 0, memory_budget_mib)
        admitted = row.status == "active"
        diagnostic = {"reason": "admitted" if admitted else "capacity_full", "workload_type": workload_type,
            "estimated_increment_mib": row.memory_budget_mib, "reserve_mib": reserve_mib,
            "free_mib": None, "pending_reserved_mib": None, "usable_free_mib": None,
            "waiting_compression_reserved_mib": 0}
        if row.status == "waiting":
            if mode == "extract_shared":
                same_type = [r for r in active if (r.memory_observation or {}).get("workload_type", "extract") == workload_type]
                limit = {"extract": capacity, "predict": 2, "compression": 1}[workload_type]
                admitted = len(same_type) < limit
                if any(r.status == "conflict" for r in active):
                    admitted, diagnostic["reason"] = False, "lease_conflict"
                elif any(r.mode != "extract_shared" for r in active):
                    admitted, diagnostic["reason"] = False, "exclusive_active"
                elif any(r.mode != "extract_shared" for r in waiting[:waiting.index(row)]):
                    admitted, diagnostic["reason"] = False, "exclusive_waiting"
                if admitted and check_memory and device != "cpu":
                    try:
                        available, memory = memory_snapshot(device)
                        pending = 0
                        for running in active:
                            observed = _observed(running, memory)
                            measurement = dict(running.memory_observation or {})
                            measurement.update(observed_mib=observed,
                                driver_peak_mib=max(measurement.get("driver_peak_mib", 0), observed),
                                sampled_at=utc_now().isoformat(), attribution="known" if observed else "conservative")
                            running.memory_observation = measurement
                            if observed > (running.memory_budget_mib or 1024):
                                running.memory_budget_mib = int(observed * 1.1) + 1
                            pending += max(0, (running.memory_budget_mib or 1024) - observed)
                        # Earmark the oldest earlier encoder when no encoder is active.
                        # Extraction may still fill memory beyond this budget, but
                        # continual replacement cannot starve an awaiting encoder.
                        earlier_encoders = [r for r in waiting[:waiting.index(row)]
                            if r.mode == "extract_shared" and
                            (r.memory_observation or {}).get("workload_type") == "compression"]
                        if workload_type != "compression" and earlier_encoders and not any(
                                r.mode == "extract_shared" and
                                (r.memory_observation or {}).get("workload_type") == "compression" for r in active):
                            encoder_pending = earlier_encoders[0].memory_budget_mib or 1024
                            pending += encoder_pending
                            diagnostic["waiting_compression_reserved_mib"] = encoder_pending
                        diagnostic.update(free_mib=available, pending_reserved_mib=pending, usable_free_mib=available - pending)
                        admitted = available - pending >= row.memory_budget_mib + reserve_mib
                        if not admitted:
                            diagnostic["reason"] = "memory_insufficient"
                    except (OSError, ValueError, subprocess.SubprocessError):
                        admitted, diagnostic["reason"] = False, "memory_query_unavailable"
            else:
                admitted = not active and waiting and waiting[0].id == row.id
                diagnostic["reason"] = "exclusive_active" if active else "exclusive_waiting"
        row.heartbeat_at = utc_now()
        if admitted:
            row.status = "active"
            diagnostic["reason"] = "admitted"
        session.commit()
        diagnostic.update(admitted=bool(admitted), updated_at=utc_now().isoformat())
        _decisions[device] = diagnostic
        if len(_decisions) > 16:
            _decisions.pop(next(iter(_decisions)))
        key, state = (device, owner, mode), (bool(admitted), diagnostic["reason"])
        previous = _decision_logs.get(key)
        if not previous or previous[0] != state or time.monotonic() - previous[1] >= 60:
            log_performance("gpu_admission", device=device, lease_id=row.id, mode=mode,
                active_leases=[{"lease_id": r.id, "workload_type": (r.memory_observation or {}).get("workload_type", "extract"),
                    "budget_mib": r.memory_budget_mib, "observed_mib": (r.memory_observation or {}).get("observed_mib"),
                    "attribution": (r.memory_observation or {}).get("attribution", "unknown")} for r in active],
                **{key: value for key, value in diagnostic.items() if key != "updated_at"})
            logger.info("GPU 准入 | mode=%s | workload=%s | admitted=%s | reason=%s | free_mib=%s | pending_reserved_mib=%s | estimated_increment_mib=%s | reserve_mib=%s | waiting_compression_reserved_mib=%s",
                mode, workload_type, bool(admitted), diagnostic["reason"], diagnostic["free_mib"], diagnostic["pending_reserved_mib"], diagnostic["estimated_increment_mib"], reserve_mib, diagnostic["waiting_compression_reserved_mib"])
            _decision_logs[key] = (state, time.monotonic())
            if len(_decision_logs) > 2048:
                _decision_logs.pop(next(iter(_decision_logs)))
        return row.id if admitted else None


def heartbeat(session, lease_id, processes=None):
    from .models import ResourceLease, utc_now
    row = session.get(ResourceLease, lease_id)
    if row is not None and row.status == "released":
        return False
    if row is None or row.status != "active":
        raise ValueError("resource_lease_lost")
    row.heartbeat_at = utc_now()
    if processes is not None:
        row.memory_observation = {**(row.memory_observation or {}), "processes": processes,
            "sampled_at": utc_now().isoformat()}
    session.commit()
    return True


def record_oom(session, lease_id):
    from .models import ResourceLease
    row = session.get(ResourceLease, lease_id)
    if row:
        row.memory_budget_mib = max(1024, int((row.memory_budget_mib or 1024) * 1.5))
        row.memory_observation = {**(row.memory_observation or {}), "oom": True}
        session.commit()


def release(session, lease_id=None, owner=None):
    from sqlalchemy import update
    from .models import ResourceLease
    condition = ResourceLease.id == lease_id if lease_id else ResourceLease.owner == owner
    session.execute(update(ResourceLease).where(condition).values(status="released"))
    session.commit()


@contextmanager
def gpu_lock(state_directory, name="gpu.lock"):
    root = Path(state_directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if name not in ("gpu.lock", "control.lock"):
        raise ValueError("Unsupported resource lock.")
    path = root / name
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
