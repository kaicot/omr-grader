"""Fail-closed RC27 fixed-workload evidence harness (Windows x64)."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "rc27-workload-receipt-v1"
WORKLOAD_VERSION = "rc27-workload-v1"
SAMPLE_INTERVAL_SECONDS = 0.05
GIB = 1024**3
LIMITS = {
    "heartbeat_gap_ms": 250,
    "cancel_submit_ms": 250,
    "cancel_settle_ms": 10_000,
    "shutdown_ms": 15_000,
    "orphan_count": 0,
    "aggregate_rss_bytes": 2 * GIB,
    "in_flight": 4,
    "workload_elapsed_ms": 900_000,
    "index_warm_ms": 100,
    "index_cold_ms": 500,
    "index_bytes": 5 * 1024**2,
    "detail_ms": 100,
    "transform_rms_px": 1,
    "transform_max_px": 2,
}


def canonical_json(value: Any) -> bytes:
    """Return the only permitted receipt encoding (UTF-8, sorted, one LF)."""
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_mapping(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} must contain exactly: {sorted(keys)}")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def validate_workload(workload: dict[str, Any]) -> None:
    """Validate the deliberately closed workload-v1 input contract."""
    required = {"schema_version", "workload_id", "executable", "argv", "inputs", "fixed_workload"}
    _require_mapping(workload, "workload", required)
    if (
        workload["schema_version"] != WORKLOAD_VERSION
        or not isinstance(workload["workload_id"], str)
        or not workload["workload_id"]
    ):
        raise ValueError("unsupported or invalid workload identity")
    executable = _require_mapping(workload["executable"], "executable", {"path", "sha256"})
    if not isinstance(executable["path"], str) or not executable["path"]:
        raise ValueError("invalid executable path")
    _sha256(executable["sha256"], "executable.sha256")
    if not isinstance(workload["argv"], list) or not all(
        isinstance(arg, str) for arg in workload["argv"]
    ):
        raise ValueError("argv must be a string array")
    if not isinstance(workload["inputs"], list) or not workload["inputs"]:
        raise ValueError("inputs must be a non-empty array")
    for item in workload["inputs"]:
        _require_mapping(item, "input", {"path", "sha256", "bytes", "source_sha256"})
        if not isinstance(item["path"], str) or not item["path"]:
            raise ValueError("input path must be a nonempty string")
        _sha256(item["sha256"], "input.sha256")
        _sha256(item["source_sha256"], "input.source_sha256")
        if type(item["bytes"]) is not int or item["bytes"] < 1:
            raise ValueError("input.bytes must be a positive integer")
    fixed = _require_mapping(
        workload["fixed_workload"],
        "fixed_workload",
        {"pages", "dpi", "max_in_flight", "index_sessions", "detail_students"},
    )
    if fixed != {
        "pages": 50,
        "dpi": 300,
        "max_in_flight": 4,
        "index_sessions": 2000,
        "detail_students": 500,
    }:
        raise ValueError("workload is not the approved RC27 fixed workload")


def validate_inputs(workload: dict[str, Any]) -> None:
    """Verify executable and fixture identity before a run; never silently rehash."""
    executable = workload["executable"]
    if sha256_file(executable["path"]) != executable["sha256"]:
        raise ValueError("executable hash mismatch")
    for item in workload["inputs"]:
        path = Path(item["path"])
        if (
            not path.is_file()
            or path.stat().st_size != item["bytes"]
            or sha256_file(path) != item["sha256"]
        ):
            raise ValueError(f"input identity mismatch: {path}")


def _required_evidence_keys() -> set[str]:
    return {
        "heartbeat_gaps_ms",
        "cancel_submit_ms",
        "cancel_settle_ms",
        "shutdown_ms",
        "orphan_pids",
        "workload_elapsed_ms",
        "index_warm_ms",
        "index_cold_ms",
        "index_bytes",
        "detail_ms",
        "transform_rms_px",
        "transform_max_px",
        "io",
        "samples",
        "descendant_pids",
        "runner",
    }


def evaluate_evidence(evidence: dict[str, Any]) -> dict[str, bool]:
    """Validate all channels and return per-bound verdicts; absent evidence is an error."""
    _require_mapping(evidence, "evidence", _required_evidence_keys())
    for key in ("heartbeat_gaps_ms", "orphan_pids", "samples", "descendant_pids"):
        if not isinstance(evidence[key], list):
            raise ValueError(f"{key} must be an array")
    if (
        not evidence["heartbeat_gaps_ms"]
        or not evidence["samples"]
        or not evidence["descendant_pids"]
    ):
        raise ValueError("required evidence channel is empty")
    for value in evidence["heartbeat_gaps_ms"]:
        _nonnegative_int(value, "heartbeat_gaps_ms value")
    for pid in evidence["orphan_pids"] + evidence["descendant_pids"]:
        if type(pid) is not int or pid <= 0:
            raise ValueError("PID evidence must contain positive integer PIDs")
    for key in (
        "cancel_submit_ms",
        "cancel_settle_ms",
        "shutdown_ms",
        "workload_elapsed_ms",
        "index_warm_ms",
        "index_cold_ms",
        "index_bytes",
        "detail_ms",
        "transform_rms_px",
        "transform_max_px",
    ):
        _nonnegative_int(evidence[key], key)
    io = _require_mapping(evidence["io"], "io", {"read_bytes", "write_bytes"})
    _nonnegative_int(io["read_bytes"], "io.read_bytes")
    _nonnegative_int(io["write_bytes"], "io.write_bytes")
    runner = _require_mapping(
        evidence["runner"], "runner", {"cpu", "ram_bytes", "os", "power", "defender"}
    )
    if (
        not isinstance(runner["cpu"], str)
        or not runner["cpu"]
        or not isinstance(runner["os"], str)
        or not runner["os"]
        or _nonnegative_int(runner["ram_bytes"], "runner.ram_bytes") < 1
        or runner["power"] not in {"offline", "ac", "unknown"}
        or runner["defender"] not in {"true", "false"}
    ):
        raise ValueError("invalid runner metadata")
    peak = 0
    peak_in_flight = 0
    prior_elapsed: int | None = None
    sampled_pids: set[int] = set()
    for sample in evidence["samples"]:
        _require_mapping(
            sample, "sample", {"elapsed_ms", "pids", "aggregate_rss_bytes", "in_flight"}
        )
        elapsed = _nonnegative_int(sample["elapsed_ms"], "sample.elapsed_ms")
        if prior_elapsed is not None:
            if elapsed <= prior_elapsed:
                raise ValueError("sample elapsed_ms values must be strictly monotonic")
            if elapsed - prior_elapsed > int(SAMPLE_INTERVAL_SECONDS * 2000):
                raise ValueError("sample cadence does not cover the measured interval")
        elif elapsed != 0:
            raise ValueError("first sample must begin at elapsed_ms 0")
        prior_elapsed = elapsed
        if not isinstance(sample["pids"], dict) or not sample["pids"]:
            raise ValueError("per-PID RSS channel missing")
        rss = 0
        for pid, value in sample["pids"].items():
            if not isinstance(pid, str) or not pid.isdigit() or int(pid) <= 0:
                raise ValueError("sample PID must be a positive decimal string")
            if int(pid) not in evidence["descendant_pids"]:
                raise ValueError("sample PID is absent from the recorded descendant set")
            sampled_pids.add(int(pid))
            rss += _nonnegative_int(value, "sample.pids RSS")
        aggregate_rss = _nonnegative_int(
            sample["aggregate_rss_bytes"], "sample.aggregate_rss_bytes"
        )
        if rss != aggregate_rss:
            raise ValueError("aggregate RSS must equal the per-PID sum")
        peak = max(peak, rss)
        peak_in_flight = max(
            peak_in_flight, _nonnegative_int(sample["in_flight"], "sample.in_flight")
        )
    if prior_elapsed is None:
        raise ValueError("samples do not cover the measured workload interval")
    sample_tolerance_ms = int(SAMPLE_INTERVAL_SECONDS * 2000)
    if prior_elapsed > evidence["workload_elapsed_ms"]:
        raise ValueError("samples extend beyond the measured workload interval")
    if prior_elapsed + sample_tolerance_ms < evidence["workload_elapsed_ms"]:
        raise ValueError("samples do not cover the measured workload interval")
    if len(evidence["descendant_pids"]) != len(set(evidence["descendant_pids"])):
        raise ValueError("descendant_pids must not contain duplicates")
    if evidence["descendant_pids"] != sorted(evidence["descendant_pids"]):
        raise ValueError("descendant_pids must be sorted")
    if sampled_pids != set(evidence["descendant_pids"]):
        raise ValueError("descendant_pids must be the complete sampled PID set")
    verdicts = {
        "heartbeat_gap_ms": max(evidence["heartbeat_gaps_ms"]) <= LIMITS["heartbeat_gap_ms"],
        "cancel_submit_ms": evidence["cancel_submit_ms"] <= LIMITS["cancel_submit_ms"],
        "cancel_settle_ms": evidence["cancel_settle_ms"] <= LIMITS["cancel_settle_ms"],
        "shutdown_ms": evidence["shutdown_ms"] <= LIMITS["shutdown_ms"],
        "orphan_count": len(evidence["orphan_pids"]) == LIMITS["orphan_count"],
        "aggregate_rss_bytes": peak <= LIMITS["aggregate_rss_bytes"],
        "in_flight": peak_in_flight <= LIMITS["in_flight"],
        "workload_elapsed_ms": evidence["workload_elapsed_ms"] <= LIMITS["workload_elapsed_ms"],
        "index_warm_ms": evidence["index_warm_ms"] <= LIMITS["index_warm_ms"],
        "index_cold_ms": evidence["index_cold_ms"] <= LIMITS["index_cold_ms"],
        "index_bytes": evidence["index_bytes"] <= LIMITS["index_bytes"],
        "detail_ms": evidence["detail_ms"] <= LIMITS["detail_ms"],
        "transform_rms_px": evidence["transform_rms_px"] <= LIMITS["transform_rms_px"],
        "transform_max_px": evidence["transform_max_px"] <= LIMITS["transform_max_px"],
    }
    return verdicts


def make_receipt(workload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    validate_workload(workload)
    verdicts = evaluate_evidence(evidence)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "workload": workload,
        "evidence": evidence,
        "limits": dict(LIMITS),
        "verdicts": verdicts,
        "passed": all(verdicts.values()),
    }
    receipt["receipt_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    return receipt


def validate_receipt(receipt: dict[str, Any]) -> None:
    """Revalidate every receipt semantic before it can be persisted."""
    required = {
        "schema_version",
        "workload",
        "evidence",
        "limits",
        "verdicts",
        "passed",
        "receipt_sha256",
    }
    _require_mapping(receipt, "receipt", required)
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported receipt schema")
    validate_workload(receipt["workload"])
    expected_verdicts = evaluate_evidence(receipt["evidence"])
    limits = _require_mapping(receipt["limits"], "receipt limits", set(LIMITS))
    if any(type(limits[key]) is not int or limits[key] != LIMITS[key] for key in LIMITS):
        raise ValueError("receipt limits do not match the approved limits")
    _require_mapping(receipt["verdicts"], "receipt verdicts", set(expected_verdicts))
    if any(type(value) is not bool for value in receipt["verdicts"].values()):
        raise ValueError("receipt verdicts must be booleans")
    if receipt["verdicts"] != expected_verdicts:
        raise ValueError("receipt verdicts do not match the evidence")
    if type(receipt["passed"]) is not bool or receipt["passed"] != all(expected_verdicts.values()):
        raise ValueError("receipt passed value does not match the verdicts")
    digest = _sha256(receipt["receipt_sha256"], "receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    if hashlib.sha256(canonical_json(unsigned)).hexdigest() != digest:
        raise ValueError("receipt hash mismatch")


def write_receipt(path: str | Path, receipt: dict[str, Any]) -> None:
    validate_receipt(receipt)
    Path(path).write_bytes(canonical_json(receipt))


def runner_metadata() -> dict[str, Any]:
    """Collect mandatory host metadata; unavailable channels fail closed."""
    if os.name != "nt":
        raise RuntimeError("RC27 runner metadata requires Windows")

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(POWER_STATUS)]
    kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
    memory = MEMORYSTATUSEX()
    memory.dwLength = ctypes.sizeof(memory)
    power = POWER_STATUS()
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    if not kernel32.GetSystemPowerStatus(ctypes.byref(power)):
        raise OSError(ctypes.get_last_error(), "GetSystemPowerStatus failed")
    power_states = {0: "offline", 1: "ac", 255: "unknown"}
    if power.ACLineStatus not in power_states:
        raise RuntimeError(f"invalid ACLineStatus: {power.ACLineStatus}")
    defender = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-MpComputerStatus).AMServiceEnabled",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    status = defender.stdout.strip().lower()
    if defender.returncode or status not in {"true", "false"}:
        raise RuntimeError("Defender metadata channel unavailable")
    result = {
        "cpu": platform.processor(),
        "ram_bytes": int(memory.ullTotalPhys),
        "os": platform.platform(),
        "power": power_states[power.ACLineStatus],
        "defender": status,
    }
    if not result["cpu"] or not result["os"] or result["ram_bytes"] < 1:
        raise RuntimeError("runner metadata channel unavailable")
    return result


# Windows collection. ctypes is kept here so importing and schema tests work on every OS.
def _windows_process_rss(pid: int) -> int:
    if os.name != "nt":
        raise RuntimeError("RSS collection requires Windows")
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_VM_READ = 0x1000, 0x0010
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"OpenProcess failed for PID {pid}")

    class COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(COUNTERS),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    try:
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error(), f"GetProcessMemoryInfo failed for PID {pid}")
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def descendant_pids(root_pid: int) -> set[int]:
    """Return the root and every currently discoverable Windows descendant."""
    if os.name != "nt":
        return {root_pid}
    TH32CS_SNAPPROCESS, ERROR_NO_MORE_FILES = 0x00000002, 18

    class ENTRY(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRY)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRY)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snap == invalid_handle:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    parents: dict[int, int] = {}
    entry = ENTRY()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Process32FirstW failed")
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            entry.dwSize = ctypes.sizeof(entry)
            if kernel32.Process32NextW(snap, ctypes.byref(entry)):
                continue
            error = int(kernel32.GetLastError())
            if error != ERROR_NO_MORE_FILES:
                raise OSError(error, "Process32NextW failed")
            break
    finally:
        kernel32.CloseHandle(snap)
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def _sample_pids(pids: set[int], elapsed_ms: int, in_flight: int = 0) -> dict[str, Any]:
    rss: dict[str, int] = {}
    for pid in pids:
        try:
            value = _windows_process_rss(pid)
            if value is None:
                raise RuntimeError(f"RSS evidence unavailable for PID {pid}: no RSS value")
            rss[str(pid)] = value
        except Exception as error:
            raise RuntimeError(f"RSS evidence unavailable for PID {pid}: {error}") from error
    return {
        "elapsed_ms": elapsed_ms,
        "pids": rss,
        "aggregate_rss_bytes": sum(rss.values()),
        "in_flight": in_flight,
    }


def sample_process_tree(root_pid: int, elapsed_ms: int, in_flight: int = 0) -> dict[str, Any]:
    return _sample_pids(descendant_pids(root_pid), elapsed_ms, in_flight)


class _JobObject:
    """Minimal Windows Job Object that terminates all assigned processes on close."""

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32 = kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._process_id_list_header_size = ctypes.sizeof(wintypes.DWORD) * 2
        self._process_id_size = ctypes.sizeof(ctypes.c_size_t)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise OSError(
                ctypes.get_last_error(), "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed"
            )

    def assign(self, process_handle: int) -> None:
        if not self.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def process_ids(self) -> set[int]:
        capacity = 1
        while True:
            buffer = (
                ctypes.c_ubyte
                * (self._process_id_list_header_size + capacity * self._process_id_size)
            )()
            returned = wintypes.DWORD()
            if self.kernel32.QueryInformationJobObject(
                self.handle,
                3,
                ctypes.byref(buffer),
                ctypes.sizeof(buffer),
                ctypes.byref(returned),
            ):
                count = int.from_bytes(bytes(buffer[4:8]), "little")
                if count > capacity:
                    capacity = count
                    continue
                offset = self._process_id_list_header_size
                return {
                    int.from_bytes(
                        bytes(
                            buffer[
                                offset + index * self._process_id_size : offset
                                + (index + 1) * self._process_id_size
                            ]
                        ),
                        "little",
                    )
                    for index in range(count)
                }
            error = ctypes.get_last_error()
            if error != 234:
                raise OSError(error, "QueryInformationJobObject(ProcessIdList) failed")
            capacity *= 2

    def wait_until_empty(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while members := self.process_ids():
            if time.monotonic() >= deadline:
                error = TimeoutError(
                    f"Job Object retained members after {timeout_seconds}s: {sorted(members)}"
                )
                error.member_pids = sorted(members)
                raise error
            time.sleep(SAMPLE_INTERVAL_SECONDS)

    def terminate_and_wait_empty(self, timeout_seconds: float = 5.0) -> None:
        if not self.kernel32.TerminateJobObject(self.handle, 1):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")
        self.wait_until_empty(timeout_seconds)

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _resume_process_threads(process_id: int) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot in {0, ctypes.c_void_p(-1).value}:
        raise OSError(ctypes.get_last_error(), "thread snapshot failed")
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    resumed = 0
    try:
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "thread enumeration failed")
        while True:
            if int(entry.th32OwnerProcessID) == process_id:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    raise OSError(
                        ctypes.get_last_error(),
                        f"OpenThread failed for {entry.th32ThreadID}",
                    )
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise OSError(
                            ctypes.get_last_error(),
                            f"ResumeThread failed for {entry.th32ThreadID}",
                        )
                    resumed += 1
                finally:
                    if not kernel32.CloseHandle(thread):
                        raise OSError(ctypes.get_last_error(), "thread handle close failed")
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                error = int(kernel32.GetLastError())
                if error != 18:
                    raise OSError(error, "thread enumeration failed")
                break
    finally:
        if not kernel32.CloseHandle(snapshot):
            raise OSError(ctypes.get_last_error(), "thread snapshot close failed")
    if resumed == 0:
        raise OSError("suspended process has no resumable thread")


def run_process(
    executable: str,
    argv: list[str],
    on_sample: Callable[[], int] | None = None,
    timeout_seconds: float = 900.0,
) -> tuple[int, list[dict[str, Any]], list[int]]:
    """Atomically contain and observe every Job Object member until it exits."""
    if os.name != "nt":
        raise RuntimeError("live RC27 Job Object collection requires Windows")
    job = _JobObject()
    try:
        process = subprocess.Popen(
            [executable, *argv], creationflags=getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
        job.assign(process._handle)
        _resume_process_threads(process.pid)
    except Exception as error:
        try:
            members = job.process_ids()
            if members:
                error.member_pids = sorted(members)
                job.terminate_and_wait_empty()
        except BaseException as cleanup_error:
            error.cleanup_error = cleanup_error
        finally:
            job.close()
        if "process" in locals() and process.poll() is None:
            process.kill()
            process.wait()
        raise
    start = time.monotonic()
    samples: list[dict[str, Any]] = []
    seen: set[int] = set()
    run_error: BaseException | None = None
    try:
        first_sample = True
        while True:
            elapsed = 0 if first_sample else int((time.monotonic() - start) * 1000)
            first_sample = False
            members = job.process_ids()
            if not members:
                if elapsed > timeout_seconds * 1000:
                    raise TimeoutError("workload exceeded timeout")
                break
            sample = _sample_pids(members, elapsed, on_sample() if on_sample else 0)
            samples.append(sample)
            seen.update(members)
            if elapsed > timeout_seconds * 1000:
                raise TimeoutError("workload exceeded timeout")
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        return process.wait(), samples, sorted(seen)
    except BaseException as error:
        run_error = error
        raise
    finally:
        try:
            if run_error is not None:
                try:
                    members = job.process_ids()
                    if members:
                        run_error.member_pids = sorted(members)
                finally:
                    job.terminate_and_wait_empty()
            else:
                job.wait_until_empty(timeout_seconds)
        except BaseException as cleanup_error:
            if run_error is None:
                raise
            run_error.cleanup_error = cleanup_error
        finally:
            job.close()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    workload = json.loads(Path(args.workload).read_text(encoding="utf-8"))
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not isinstance(workload, dict) or not isinstance(evidence, dict):
        raise ValueError("workload and evidence inputs must be JSON objects")
    validate_workload(workload)
    validate_inputs(workload)
    receipt = make_receipt(workload, evidence)
    write_receipt(args.receipt, receipt)
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
