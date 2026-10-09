"""Observable Windows smoke tests for a verified extracted onedir release.

The application supplies an opt-in readiness file only when
``OMR_GRADER_SMOKE_READY_FILE`` is set.  A title alone is intentionally not a
readiness signal: the splash window has the same title as the main UI.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READY_SCHEMA = 1
WRITE_AFFORDANCES = {"config_persistence", "session_persistence"}
SHA256_LENGTH = 64


class SmokeError(RuntimeError):
    pass


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]  # noqa: RUF012  ctypes _fields_ layout, never shared


class _Job:
    """Kill-on-close job: ownership is the Windows handle, never a recycled PID."""
    def __init__(self, process: subprocess.Popen[str]) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise SmokeError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        class Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_longlong), ("b", ctypes.c_longlong), ("flags", wintypes.DWORD), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]  # noqa: RUF012  ctypes _fields_ layout, never shared
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in "abcdef"]  # noqa: RUF012  ctypes _fields_ layout, never shared
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Io), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t)]  # noqa: RUF012  ctypes _fields_ layout, never shared
        limits = Extended()
        limits.basic.flags = 0x2000
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(self.handle, process._handle):
            error = ctypes.get_last_error()
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise SmokeError(f"could not configure/assign process job: {error}")
    def terminate(self) -> None:
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise SmokeError(f"TerminateJobObject failed: {ctypes.get_last_error()}")

    def process_ids(self) -> set[int]:
        if not self.handle:
            return set()
        capacity = 16
        while True:
            size = ctypes.sizeof(wintypes.DWORD) * 2 + capacity * ctypes.sizeof(ctypes.c_size_t)
            buffer = ctypes.create_string_buffer(size)
            if self.kernel.QueryInformationJobObject(self.handle, 3, buffer, size, None):
                # JOBOBJECT_BASIC_PROCESS_ID_LIST: assigned count first, then
                # the number of IDs actually returned in this buffer.
                count = int.from_bytes(buffer.raw[4:8], "little")
                offset = ctypes.sizeof(wintypes.DWORD) * 2
                return {int.from_bytes(buffer.raw[offset + index * ctypes.sizeof(ctypes.c_size_t):offset + (index + 1) * ctypes.sizeof(ctypes.c_size_t)], "little") for index in range(count)}
            if ctypes.get_last_error() != 234:
                raise SmokeError(f"QueryInformationJobObject failed: {ctypes.get_last_error()}")
            capacity *= 2

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.process_ids():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True
    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise SmokeError(f"CloseHandle(job) failed: {ctypes.get_last_error()}")


def _resume_suspended(process_id: int) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise SmokeError(f"thread snapshot failed: {ctypes.get_last_error()}")
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == process_id:
                thread = kernel.OpenThread(2, False, entry.th32ThreadID)
                if not thread or kernel.ResumeThread(thread) == 0xFFFFFFFF:
                    raise SmokeError(f"could not resume owned process: {ctypes.get_last_error()}")
                kernel.CloseHandle(thread)
                return
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise SmokeError("owned suspended process had no resumable thread")
    finally:
        kernel.CloseHandle(snapshot)


@dataclass
class SmokeReport:
    mode: str
    checks: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def passed(self, name: str, value: Any = None) -> None:
        self.checks[name] = "PASS"
        if value is not None:
            self.details[name] = value

    def failed(self, name: str, value: Any) -> None:
        self.checks[name] = "FAIL"
        self.details[name] = str(value)

    def output(self) -> dict[str, Any]:
        return {
            "result": "PASS" if all(value == "PASS" for value in self.checks.values()) else "FAIL",
            "mode": self.mode,
            "checks": self.checks,
            "details": self.details,
        }


def tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in sorted(root.rglob("*"), key=lambda value: value.as_posix().casefold()):
        if item.is_symlink():
            raise SmokeError(f"release copy contains a symlink: {item}")
        if item.is_file():
            digest = hashlib.sha256()
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[item.relative_to(root).as_posix()] = digest.hexdigest()
    return result


def current_user_sid() -> str:
    completed = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise SmokeError(f"could not resolve current SID: {completed.stderr.strip()}")
    # CSV output has a display name and a SID. Avoid using a login name, which
    # can be ambiguous for domain/local accounts and breaks ACL restoration.
    import csv

    row = next(csv.reader(completed.stdout.splitlines()), None)
    if row is None or len(row) != 2 or not row[1].startswith("S-"):
        raise SmokeError("could not parse current SID")
    return row[1]


def apply_write_deny(root: Path, sid: str) -> None:
    # icacls treats an unqualified numeric SID as an account name; the leading
    # asterisk explicitly selects SID syntax for both add and removal.
    rule = f"*{sid}:(OI)(CI)(WD,AD,DC,DE)"
    completed = subprocess.run(
        ["icacls", str(root), "/deny", rule, "/T", "/C"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise SmokeError(f"could not apply narrow write deny: {completed.stderr.strip()}")


def remove_write_deny(root: Path, sid: str) -> None:
    completed = subprocess.run(
        ["icacls", str(root), "/remove:d", f"*{sid}", "/T", "/C"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise SmokeError(f"could not restore ACL deny rule: {completed.stderr.strip()}")


def _close_windows(process_id: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_size_t),
    ]

    callback_errors: list[BaseException] = []
    closed = 0
    def close(window: int, _: int) -> bool:
        nonlocal closed
        try:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == process_id:
                result = ctypes.c_size_t()
                if not user32.SendMessageTimeoutW(window, 0x0112, 0xF060, 0, 0x0002, 5000, ctypes.byref(result)):
                    raise SmokeError(f"could not close main window: {ctypes.get_last_error()}")
                closed += 1
        except BaseException as error:  # ctypes callbacks otherwise swallow errors.  # noqa: BLE001  smoke harness records every failure instead of crashing
            callback_errors.append(error)
            return False
        return True
    enumerated = user32.EnumWindows(callback_type(close), 0)
    if callback_errors:
        raise SmokeError(f"window-close callback failed: {callback_errors[0]}") from callback_errors[0]
    if not enumerated:
        raise SmokeError(f"could not enumerate windows: {ctypes.get_last_error()}")
    if not closed:
        raise SmokeError("no launched main window accepted a close request")


def _force_tree_cleanup(process: subprocess.Popen[str], job: _Job) -> None:
    # A GUI parent can exit while a child remains.  Every descendant observed
    # during this launch is therefore cleaned individually, not only via /T on
    # a still-running parent PID.
    job.terminate()
    if process.poll() is None:
        process.wait(timeout=10)
    if not job.wait_empty(10):
        raise SmokeError("owned process job did not drain after forced cleanup")


def _wait_for_ready(process: subprocess.Popen[str], ready_file: Path, mode: str, owned_pids: set[int] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeError(f"startup exit {process.returncode}")
        if ready_file.is_file():
            try:
                value = json.loads(ready_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SmokeError(f"readiness capture is invalid: {error}") from error
            if not isinstance(value, dict):
                raise SmokeError("readiness capture is not an object")
            expected_read_only = mode == "readonly"
            if (
                value.get("schema") != READY_SCHEMA
                or value.get("pid") != process.pid
                or value.get("state") != "main-ready"
                or value.get("read_only") is not expected_read_only
                or value.get("write_enabled") is not (not expected_read_only)
            ):
                raise SmokeError(f"main UI readiness contract failed: {value!r}")
            if type(value.get("persistence_roundtrip")) is not bool:
                raise SmokeError("readiness capture lacks persistence_roundtrip")
            affordances = value.get("affordances")
            if not isinstance(affordances, dict):
                raise SmokeError("readiness capture lacks affordances")
            if expected_read_only:
                if any(affordances.get(name) is not False for name in WRITE_AFFORDANCES):
                    raise SmokeError("read-only UI has an enabled write affordance")
                if value.get("persistence") is not None:
                    raise SmokeError("read-only UI claimed persistence")
                if value["persistence_roundtrip"]:
                    raise SmokeError("read-only UI claimed a persistence roundtrip")
            elif any(affordances.get(name) is not True for name in WRITE_AFFORDANCES):
                raise SmokeError("writable UI did not expose persistence affordances")
            return value
        time.sleep(0.2)
    raise SmokeError("main UI readiness capture timed out (splash title is not accepted)")


def _start(root: Path, ready_file: Path, mode: str, phase: str, nonce: str, marker: Path) -> tuple[subprocess.Popen[str], _Job]:
    executable = root / "OMR Grader.exe"
    if not executable.is_file():
        raise SmokeError("OMR Grader.exe is missing")
    environment = os.environ.copy()
    environment["OMR_GRADER_SMOKE_READY_FILE"] = str(ready_file)
    environment["OMR_GRADER_SMOKE_MODE"] = mode
    environment["OMR_GRADER_SMOKE_PHASE"] = phase
    environment["OMR_GRADER_SMOKE_NONCE"] = nonce
    environment["OMR_GRADER_SMOKE_SCOPE_MARKER"] = str(marker)
    process: subprocess.Popen[str] | None = None
    job: _Job | None = None
    try:
        process = subprocess.Popen([str(executable)], cwd=root, env=environment, creationflags=getattr(subprocess, "CREATE_SUSPENDED", 4))
        job = _Job(process)
        _resume_suspended(process.pid)
        return process, job
    except BaseException as error:
        if job is not None:
            try:
                job.terminate()
            finally:
                job.close()
        elif process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        raise SmokeError(f"process creation/containment failed: {error}") from error


def _graceful_close(process: subprocess.Popen[str], *, required: bool, job: _Job) -> bool:
    _close_windows(process.pid)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired as timeout:
        _force_tree_cleanup(process, job)
        job.close()
        if required:
            raise SmokeError("graceful close timed out") from timeout
        return False
    if process.returncode != 0:
        raise SmokeError(f"graceful close exit {process.returncode}")
    if job.wait_empty(5):
        job.close()
        return True
    _force_tree_cleanup(process, job)
    job.close()
    if required:
        raise SmokeError("graceful close left owned descendant processes")
    return False


def _written_persistence(payload: dict[str, Any]) -> dict[str, Any]:
    persistence = payload.get("persistence")
    if not isinstance(persistence, dict) or persistence.get("phase") != "written":
        raise SmokeError("first writable readiness lacks persisted state")
    if payload.get("persistence_roundtrip") is not False or not isinstance(persistence, dict):
        raise SmokeError("first writable readiness has an invalid roundtrip flag")
    if type(persistence.get("default_sensitivity")) is not int or persistence["default_sensitivity"] == 5:
        raise SmokeError("writable readiness did not save a nondefault sensitivity")
    digest = persistence.get("config_sha256")
    if not isinstance(digest, str) or len(digest) != SHA256_LENGTH or any(c not in "0123456789abcdef" for c in digest):
        raise SmokeError("writable readiness has invalid authoritative config hash")
    return {"default_sensitivity": persistence["default_sensitivity"], "config_sha256": digest}


def _reopened_persistence(payload: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    reopened = payload.get("persistence")
    if not isinstance(reopened, dict) or reopened.get("phase") != "reopened":
        raise SmokeError("second writable launch did not reopen persisted state")
    stable = {"default_sensitivity": reopened.get("default_sensitivity"), "config_sha256": reopened.get("config_sha256")}
    if payload.get("persistence_roundtrip") is not True or stable != expected:
        raise SmokeError("second writable launch failed authoritative config roundtrip")
    return reopened


def run_smoke(source: Path, *, mode: str, require_graceful_close: bool) -> SmokeReport:
    if os.name != "nt":
        raise SmokeError("onedir smoke testing requires Windows")
    report = SmokeReport(mode=mode)
    if not source.is_dir():
        raise SmokeError(f"release application folder is missing: {source}")
    temporary = tempfile.TemporaryDirectory(prefix="omr-grader-onedir-smoke-")
    root = Path(temporary.name) / "OMR Grader"
    process: subprocess.Popen[str] | None = None
    job: _Job | None = None
    deny_applied = False
    sid: str | None = None
    before: dict[str, str] | None = None
    nonce = uuid.uuid4().hex
    try:
        shutil.copytree(source, root)
        report.passed("isolated_copy")
        marker = root.parent / f"omr-smoke-scope-{nonce}.json"
        ready = root.parent / f"omr-smoke-ready-{nonce}.json"
        marker_payload = {
            "schema": 1, "nonce": nonce, "app_root": str(root.resolve()), "ready_file": str(ready.resolve())
        }
        with marker.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(marker_payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        report.passed("runner_scope_marker", {"marker": str(marker), "nonce": nonce})
        if mode == "readonly":
            before = tree_hashes(root)
            sid = current_user_sid()
            apply_write_deny(root, sid)
            deny_applied = True
            report.passed("acl_write_deny", {"sid": sid, "rights": "WD,AD,DC,DE"})
        # The observation file is outside the portable root and is bound to the
        # sibling marker, so it cannot be the change the read-only test detects.
        phase = "readonly" if mode == "readonly" else "write"
        process, job = _start(root, ready, mode, phase, nonce, marker)
        report.passed("process_started", {"pid": process.pid})
        payload = _wait_for_ready(process, ready, mode, set())
        report.passed("main_ui_ready", payload)
        graceful = _graceful_close(process, required=require_graceful_close, job=job)
        report.passed("graceful_close" if graceful else "forced_cleanup", graceful)
        process = None
        if mode == "writable":
            persisted = _written_persistence(payload)
            # A second launch must load the persisted portable data, not merely
            # create it during the first initialization.
            ready.unlink(missing_ok=True)
            process, job = _start(root, ready, mode, "read", nonce, marker)
            payload = _wait_for_ready(process, ready, mode, set())
            reopened = _reopened_persistence(payload, persisted)
            report.passed("persistence_roundtrip", reopened)
            graceful = _graceful_close(process, required=require_graceful_close, job=job)
            report.passed("second_close" if graceful else "second_forced_cleanup", graceful)
            process = None
        else:
            after = tree_hashes(root)
            if before != after:
                raise SmokeError("read-only portable tree bytes changed")
            report.passed("read_only_tree_unchanged")
    except Exception as error:  # noqa: BLE001  smoke harness records every failure instead of crashing
        report.failed("execution", error)
    finally:
        try:
            if job is not None:
                try:
                    if process is not None:
                        _force_tree_cleanup(process, job)
                        report.passed("process_tree_cleanup")
                finally:
                    job.close()
        except Exception as error:  # noqa: BLE001  smoke harness records every failure instead of crashing
            report.failed("process_tree_cleanup", error)
        try:
            if deny_applied and sid is not None:
                remove_write_deny(root, sid)
                report.passed("acl_restored", {"sid": sid})
        except Exception as error:  # noqa: BLE001  smoke harness records every failure instead of crashing
            report.failed("acl_restored", error)
        temporary.cleanup()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--mode", choices=("writable", "readonly"), required=True)
    parser.add_argument("--require-graceful-close", action="store_true")
    arguments = parser.parse_args()
    try:
        report = run_smoke(
            arguments.release.resolve(),
            mode=arguments.mode,
            require_graceful_close=arguments.require_graceful_close,
        )
    except Exception as error:  # noqa: BLE001  smoke harness records every failure instead of crashing
        report = SmokeReport(arguments.mode)
        report.failed("preflight", error)
    print(json.dumps(report.output(), sort_keys=True))
    return 0 if report.output()["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
