from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[2] / "tools" / "performance_workload.py"
spec = importlib.util.spec_from_file_location("performance_workload", MODULE)
assert spec and spec.loader
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_descendant_aggregation_is_not_root_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "descendant_pids", lambda root: {root, 22})
    values = {10: 7, 22: 11}
    monkeypatch.setattr(harness, "_windows_process_rss", values.get)
    sample = harness.sample_process_tree(10, 50, 3)
    assert sample["pids"] == {"10": 7, "22": 11}
    assert sample["aggregate_rss_bytes"] == 18
    assert sample["in_flight"] == 3


def test_non_windows_live_job_collection_is_explicitly_unavailable() -> None:
    if os.name != "nt":
        with pytest.raises(RuntimeError, match="requires Windows"):
            harness.run_process(sys.executable, ["-c", "pass"])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_windows_lightweight_process_has_50ms_rss_samples() -> None:
    code, samples, descendants = harness.run_process(
        sys.executable, ["-c", "import time; time.sleep(.22)"], timeout_seconds=2
    )
    assert code == 0
    assert descendants
    assert len(samples) >= 2
    assert all(sample["aggregate_rss_bytes"] == sum(sample["pids"].values()) for sample in samples)
    assert samples[0]["elapsed_ms"] == 0
    assert all(sample["elapsed_ms"] >= 0 for sample in samples)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_timeout_terminates_parent_and_child_and_preserves_membership(tmp_path: Path) -> None:
    child_pid = tmp_path / "timed-out-child.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"open({str(child_pid)!r}, 'w').write(str(child.pid)); "
        "time.sleep(30)"
    )
    with pytest.raises(TimeoutError) as raised:
        harness.run_process(sys.executable, ["-c", parent_code], timeout_seconds=0.25)
    assert child_pid.is_file()
    assert len(raised.value.member_pids) >= 2
    child = int(child_pid.read_text(encoding="utf-8"))
    assert child in raised.value.member_pids
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    for pid in raised.value.member_pids:
        for _ in range(20):
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                break
            exit_code = ctypes.c_ulong()
            assert kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            assert kernel32.CloseHandle(handle)
            if exit_code.value != 259:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"Job Object left timed-out PID {pid} running")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_job_object_waits_for_child_that_outlives_root(tmp_path: Path) -> None:
    child_ready = tmp_path / "child-ready"
    child_pid = tmp_path / "child.pid"
    child_code = (
        "import pathlib, time; "
        f"pathlib.Path({str(child_ready)!r}).write_text('ready'); "
        "time.sleep(.5)"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        f"ready = pathlib.Path({str(child_ready)!r})\n"
        "deadline = time.monotonic() + 2\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(.01)\n"
        "assert ready.exists()"
    )
    started = time.monotonic()
    code, _, descendants = harness.run_process(
        sys.executable, ["-c", parent_code], timeout_seconds=2
    )
    elapsed = time.monotonic() - started
    assert code == 0
    assert child_ready.is_file()
    pid = int(child_pid.read_text(encoding="utf-8"))
    assert pid in descendants
    assert elapsed >= 0.35
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    for _ in range(20):
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            break
        assert kernel32.CloseHandle(handle)
        time.sleep(0.05)
    else:
        pytest.fail("Job Object left the child process running")
def test_run_error_terminates_job_without_waiting_for_the_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        _handle = 1
        pid = 2

        def poll(self):
            return None

        def wait(self):
            return 0

    class Job:
        instance: Job

        def __init__(self) -> None:
            Job.instance = self
            self.terminated = False
            self.wait_timeout: float | None = None

        def assign(self, handle: int) -> None:
            assert handle == 1

        def process_ids(self) -> set[int]:
            return {2} if not self.terminated else set()

        def terminate_and_wait_empty(self, timeout_seconds: float = 5.0) -> None:
            self.terminated = True

        def wait_until_empty(self, timeout_seconds: float) -> None:
            self.wait_timeout = timeout_seconds

        def close(self) -> None:
            pass

    monkeypatch.setattr(harness.os, "name", "nt")
    monkeypatch.setattr(harness, "_JobObject", Job)
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(harness, "_resume_process_threads", lambda pid: None)

    def failing_sample(*args):
        raise RuntimeError("sample failed")

    monkeypatch.setattr(harness, "_sample_pids", failing_sample)

    with pytest.raises(RuntimeError, match="sample failed") as raised:
        harness.run_process("app.exe", [], timeout_seconds=900)

    assert raised.value.member_pids == [2]
    assert Job.instance.terminated
    assert Job.instance.wait_timeout is None


def complete_evidence(**changes: object) -> dict:
    evidence = {
        "heartbeat_gaps_ms": [1],
        "cancel_submit_ms": 1,
        "cancel_settle_ms": 1,
        "shutdown_ms": 1,
        "orphan_pids": [],
        "workload_elapsed_ms": 50,
        "index_warm_ms": 1,
        "index_cold_ms": 1,
        "index_bytes": 1,
        "detail_ms": 1,
        "transform_rms_px": 1,
        "transform_max_px": 1,
        "io": {"read_bytes": 0, "write_bytes": 0},
        "samples": [
            {"elapsed_ms": 0, "pids": {"1": 10}, "aggregate_rss_bytes": 10, "in_flight": 0},
            {"elapsed_ms": 50, "pids": {"1": 11}, "aggregate_rss_bytes": 11, "in_flight": 0},
        ],
        "descendant_pids": [1],
        "runner": {
            "cpu": "cpu",
            "ram_bytes": 1,
            "os": "Windows",
            "power": "ac",
            "defender": "true",
        },
    }
    evidence.update(changes)
    return evidence


@pytest.mark.parametrize(
    "change",
    [
        {"index_bytes": 1.5},
        {"io": {"read_bytes": True, "write_bytes": 0}},
        {
            "samples": [
                {"elapsed_ms": 0, "pids": {"1": 1.5}, "aggregate_rss_bytes": 1, "in_flight": 0}
            ]
        },
    ],
)
def test_evidence_rejects_fractional_or_boolean_counts(change: dict) -> None:
    with pytest.raises(ValueError):
        harness.evaluate_evidence(complete_evidence(**change))


def test_evidence_requires_complete_rss_and_sample_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "descendant_pids", lambda root: {root, 22})
    monkeypatch.setattr(harness, "_windows_process_rss", lambda pid: 7 if pid == 10 else None)
    with pytest.raises(RuntimeError, match="PID 22"):
        harness.sample_process_tree(10, 0)
    with pytest.raises(ValueError, match="cadence"):
        harness.evaluate_evidence(
            complete_evidence(
                samples=[
                    {"elapsed_ms": 0, "pids": {"1": 1}, "aggregate_rss_bytes": 1, "in_flight": 0},
                    {"elapsed_ms": 101, "pids": {"1": 1}, "aggregate_rss_bytes": 1, "in_flight": 0},
                ]
            )
        )
    with pytest.raises(ValueError, match="complete sampled PID set"):
        harness.evaluate_evidence(complete_evidence(descendant_pids=[1, 2]))

    with pytest.raises(ValueError, match="beyond"):
        harness.evaluate_evidence(
            complete_evidence(
                workload_elapsed_ms=0,
                samples=[
                    {"elapsed_ms": 0, "pids": {"1": 1}, "aggregate_rss_bytes": 1, "in_flight": 0},
                    {"elapsed_ms": 1, "pids": {"1": 1}, "aggregate_rss_bytes": 1, "in_flight": 0},
                ],
            )
        )
    with pytest.raises(ValueError, match="duplicates"):
        harness.evaluate_evidence(complete_evidence(descendant_pids=[1, 1]))


@pytest.mark.parametrize("power", ["battery", "AC", "", None])
def test_evidence_rejects_invalid_runner_power(power: object) -> None:
    with pytest.raises(ValueError, match="runner"):
        harness.evaluate_evidence(
            complete_evidence(runner={**complete_evidence()["runner"], "power": power})
        )


def test_receipt_write_rejects_tampering(tmp_path: Path) -> None:
    workload = {
        "schema_version": harness.WORKLOAD_VERSION,
        "workload_id": "test",
        "executable": {"path": "app", "sha256": "a" * 64},
        "argv": [],
        "inputs": [{"path": "input", "sha256": "b" * 64, "source_sha256": "c" * 64, "bytes": 1}],
        "fixed_workload": {
            "pages": 50,
            "dpi": 300,
            "max_in_flight": 4,
            "index_sessions": 2000,
            "detail_students": 500,
        },
    }
    receipt = harness.make_receipt(workload, complete_evidence())
    receipt["passed"] = False
    receipt["receipt_sha256"] = hashlib.sha256(
        harness.canonical_json(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    ).hexdigest()
    with pytest.raises(ValueError, match="passed"):
        harness.write_receipt(tmp_path / "receipt.json", receipt)


def test_receipt_write_revalidates_limits_and_verdicts(tmp_path: Path) -> None:
    workload = {
        "schema_version": harness.WORKLOAD_VERSION,
        "workload_id": "test",
        "executable": {"path": "app", "sha256": "a" * 64},
        "argv": [],
        "inputs": [{"path": "input", "sha256": "b" * 64, "source_sha256": "c" * 64, "bytes": 1}],
        "fixed_workload": {
            "pages": 50,
            "dpi": 300,
            "max_in_flight": 4,
            "index_sessions": 2000,
            "detail_students": 500,
        },
    }
    for field, value, message in (
        ("limits", {**harness.LIMITS, "detail_ms": 99}, "limits"),
        ("limits", {**harness.LIMITS, "transform_rms_px": True}, "limits"),
        (
            "verdicts",
            {**harness.make_receipt(workload, complete_evidence())["verdicts"], "detail_ms": False},
            "verdicts",
        ),
    ):
        receipt = harness.make_receipt(workload, complete_evidence())
        receipt[field] = value
        receipt["receipt_sha256"] = hashlib.sha256(
            harness.canonical_json(
                {key: item for key, item in receipt.items() if key != "receipt_sha256"}
            )
        ).hexdigest()
        with pytest.raises(ValueError, match=message):
            harness.write_receipt(tmp_path / "receipt.json", receipt)


def test_cli_writes_receipt_and_returns_verdict_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = Path(sys.executable)
    input_path = tmp_path / "input.bin"
    input_path.write_bytes(b"fixture")
    workload = {
        "schema_version": harness.WORKLOAD_VERSION,
        "workload_id": "cli-test",
        "executable": {"path": str(executable), "sha256": harness.sha256_file(executable)},
        "argv": [],
        "inputs": [
            {
                "path": str(input_path),
                "sha256": harness.sha256_file(input_path),
                "source_sha256": "c" * 64,
                "bytes": input_path.stat().st_size,
            }
        ],
        "fixed_workload": {
            "pages": 50,
            "dpi": 300,
            "max_in_flight": 4,
            "index_sessions": 2000,
            "detail_students": 500,
        },
    }
    workload_path = tmp_path / "workload.json"
    evidence_path = tmp_path / "evidence.json"
    receipt_path = tmp_path / "receipt.json"
    workload_path.write_bytes(harness.canonical_json(workload))
    for expected_exit, evidence in (
        (0, complete_evidence()),
        (1, complete_evidence(index_bytes=harness.LIMITS["index_bytes"] + 1)),
    ):
        evidence_path.write_bytes(harness.canonical_json(evidence))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "performance_workload.py",
                "--workload",
                str(workload_path),
                "--evidence",
                str(evidence_path),
                "--receipt",
                str(receipt_path),
            ],
        )
        assert harness.main() == expected_exit
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["passed"] is (expected_exit == 0)
        harness.validate_receipt(receipt)
        raw = receipt_path.read_bytes()
        assert raw == harness.canonical_json(receipt)
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
