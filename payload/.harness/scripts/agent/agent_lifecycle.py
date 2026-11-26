from __future__ import annotations
import os
import signal
import subprocess
import time
import sys
from contextlib import nullcontext
from pathlib import Path
from ci_common import msg
from agent_models import Session, SessionError, Stopped

GRACE_SECONDS = 10

WINDOWS_BOOTSTRAP = (
    "import os,subprocess,sys\n"
    "if os.read(0,1):\n"
    " code=subprocess.call(sys.argv[1:],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr)\n"
    " sys.exit(code if code<0x80000000 else code-0x100000000)\n"
)


class WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Limits), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                        (name, wintypes.DWORD) for name in
                        ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

        self.ctypes, self.accounting = ctypes, Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, args in (
            ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
            ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            ("QueryInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]),
            ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
        ):
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, args
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.api.CloseHandle(self.handle)
            raise error

    def __enter__(self):
        return self

    def start(self, process: subprocess.Popen) -> None:
        try:
            handle = self.api.OpenProcess(0x0101, False, process.pid)
            if not handle:
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            try:
                if not self.api.AssignProcessToJobObject(self.handle, handle):
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
            finally:
                self.api.CloseHandle(handle)
        except BaseException:
            process.kill()
            process.wait()
            raise

    def __exit__(self, *_exc):
        try:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            state = self.accounting()
            while True:
                if not self.api.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(state), self.ctypes.sizeof(state), None):
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
                if not state.ActiveProcesses:
                    return
                time.sleep(0.01)
        finally:
            self.api.CloseHandle(self.handle)


def kill(process: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except AttributeError:
        process.kill()


def stop(process: subprocess.Popen) -> None:
    kill(process, signal.SIGTERM)
    try:
        process.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    kill(process, getattr(signal, "SIGKILL", signal.SIGTERM))
    process.wait()


def execute(session: Session, command: list[str], log: Path) -> int:
    minutes = session.args.timeout_minutes
    with log.open("w", encoding="utf-8") as output, (WindowsJob() if os.name == "nt" else nullcontext()) as job:
        if job:
            command = [sys.executable, "-I", "-S", "-c", WINDOWS_BOOTSTRAP, *command]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, env=session.env,
                                   cwd=session.workdir, text=True, encoding="utf-8", start_new_session=True)
        if job:
            job.start(process)
        try:
            process.communicate(input=("\0" if job else "") + session.prompt, timeout=minutes * 60 if minutes else None)
        except subprocess.TimeoutExpired:
            stop(process)
            raise SessionError(msg("agent_run_timeout", session.args.provider, format(minutes, "g"))) from None
        except Stopped:
            stop(process)
            raise
        kill(process, getattr(signal, "SIGKILL", signal.SIGTERM))
    return process.returncode


def on_signal(signum: int, _frame: object) -> None:
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    raise Stopped(signum)
