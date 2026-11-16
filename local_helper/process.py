"""A Windows job owns the launcher and every browser child, including relaunches."""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes",
    )]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io", IoCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        *[(name, wintypes.DWORD) for name in (
            "x", "y", "xsize", "ysize", "xchars", "ychars", "fill", "flags",
        )],
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
        ("reserved_data", ctypes.c_void_p),
        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
    ]


class ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
        ("pid", wintypes.DWORD), ("tid", wintypes.DWORD),
    ]


class OwnedProcess:
    def __init__(self, arguments: list[str]):
        self.arguments = arguments
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.kernel
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
            wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo),
        ]
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        self.job = api.CreateJobObjectW(None, None)
        self.handle = None
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        startup, process = StartupInfo(), ProcessInfo()
        startup.cb = ctypes.sizeof(startup)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
        # Suspend until assignment so Edge's compatibility relaunch cannot
        # escape ownership before Python receives the launcher handle.
        if not api.CreateProcessW(
            arguments[0], command, None, None, False, 0x4 | 0x08000000,
            None, None, ctypes.byref(startup), ctypes.byref(process),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle, self.pid = process.process, process.pid
        try:
            if not api.AssignProcessToJobObject(self.job, self.handle):
                api.TerminateProcess(self.handle, 1)
                raise ctypes.WinError(ctypes.get_last_error())
            if api.ResumeThread(process.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            api.CloseHandle(process.thread)

    def poll(self):
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if code.value == 259 else code.value

    def wait(self, timeout=5):
        value = self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000))
        if value == 258:
            raise subprocess.TimeoutExpired(self.arguments, timeout)
        if value != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        self.kernel.TerminateJobObject(self.job, 1)

    kill = terminate

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
