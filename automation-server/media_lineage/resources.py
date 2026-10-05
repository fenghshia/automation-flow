"""Cross-process GPU exclusion shared by feature inference and compression."""

import os
import hashlib
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from contextvars import ContextVar

_mutex = threading.RLock()
_lease_context = ContextVar("media_resource_context", default=None)


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
    result = subprocess.run(["nvidia-smi", "-i", ordinal, "--query-gpu=uuid,memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True)
    fields = result.stdout.strip().split(",")
    if len(fields) != 2 or not fields[0].strip().startswith("GPU-"):
        raise ValueError("gpu_driver_identity_unavailable")
    return fields[0].strip()


def free_memory(device):
    result = subprocess.run(["nvidia-smi", "-i", device, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True)
    return int(result.stdout.strip())


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


def request_lease(session, device, mode, owner, capacity=6, check_memory=True):
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
        admitted = row.status == "active"
        if row.status == "waiting":
            if mode == "extract_shared":
                admitted = len(active) < capacity and all(r.mode == "extract_shared" and r.status == "active" for r in active)
                admitted = admitted and not any(r.mode != "extract_shared" for r in waiting[:waiting.index(row)])
                # Conservative driver admission; configured slots remain unchanged.
                if admitted and check_memory and device != "cpu":
                    admitted = free_memory(device) >= 2048 * (len(active) + 1)
            else:
                admitted = not active and waiting and waiting[0].id == row.id
        row.heartbeat_at = utc_now()
        if admitted:
            row.status = "active"
        session.commit()
        return row.id if admitted else None


def heartbeat(session, lease_id):
    from .models import ResourceLease, utc_now
    row = session.get(ResourceLease, lease_id)
    if row is None or row.status != "active":
        raise ValueError("resource_lease_lost")
    row.heartbeat_at = utc_now()
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
