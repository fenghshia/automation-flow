"""Owned worker trees: Windows kill-on-close Job Objects; POSIX process groups."""

import os
import signal


class ProcessTree:
    def __init__(self, process):
        self.process, self.handle = process, None
        self.closed = False
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64), ("Flags", w.DWORD),
                        ("Min", ctypes.c_size_t), ("Max", ctypes.c_size_t), ("Limit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.Flags = 0x2000
        if not handle or not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.kernel.AssignProcessToJobObject(handle, w.HANDLE(int(process._handle))):
            if handle:
                self.kernel.CloseHandle(handle)
            process.kill()
            process.wait()
            raise OSError("worker_job_object_unavailable")
        self.handle = handle

    def terminate(self):
        if self.closed:
            return
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self.closed:
            return
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        elif os.name != "nt":
            self.terminate()
        self.closed = True
