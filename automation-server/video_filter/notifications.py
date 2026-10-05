"""Nonrecursive Windows notifications are reconciliation hints, never labels."""

import ctypes
import os
import threading
from ctypes import wintypes as w


class DirectoryWatch:
    def __init__(self, root):
        self.changed = threading.Event()
        self.overflow = threading.Event()
        self.stopped = threading.Event()
        self.handle = None
        self.root = root
        self.thread = None
        if os.name == "nt":
            self.thread = threading.Thread(target=self._run, name="video-filter-directory-watch", daemon=True)
            self.thread.start()

    def _run(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        kernel.ReadDirectoryChangesW.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CancelIoEx.argtypes = [w.HANDLE, ctypes.c_void_p]
        kernel.OpenThread.restype = w.HANDLE
        kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.CancelSynchronousIo.argtypes = [w.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateFileW(str(self.root), 1, 7, None, 3, 0x02000000, None)
        if self.handle == w.HANDLE(-1).value:
            self.handle = None
            self.overflow.set()
            self.changed.set()
            return
        try:
            buffer = ctypes.create_string_buffer(65536)
            while not self.stopped.is_set():
                size = w.DWORD()
                success = kernel.ReadDirectoryChangesW(self.handle, buffer, len(buffer), False, 0x1 | 0x8 | 0x10,
                    ctypes.byref(size), None, None)
                if not success or size.value == 0:
                    self.overflow.set()
                self.changed.set()
                if not success:
                    break
        finally:
            kernel.CloseHandle(self.handle)
            self.handle = None

    def poll(self):
        changed, overflow = self.changed.is_set(), self.overflow.is_set()
        self.changed.clear()
        self.overflow.clear()
        return changed, overflow

    def close(self):
        self.stopped.set()
        if self.handle:
            self.kernel.CancelIoEx(self.handle, None)
            # ReadDirectoryChangesW uses synchronous I/O on the watcher thread.
            thread_handle = self.kernel.OpenThread(0x0001, False, self.thread.native_id)
            if thread_handle:
                try:
                    self.kernel.CancelSynchronousIo(thread_handle)
                finally:
                    self.kernel.CloseHandle(thread_handle)
        if self.thread:
            self.thread.join(timeout=1)
