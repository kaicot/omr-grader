from __future__ import annotations

import importlib.util
import os
import subprocess
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("portable_smoke", ROOT / "tools" / "smoke-portable-onedir.py")
assert SPEC and SPEC.loader
portable_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = portable_smoke
SPEC.loader.exec_module(portable_smoke)


class _Running:
    pid = 77

    def poll(self) -> None:
        return None


class _Exited:
    pid = 77
    returncode = 17

    def poll(self) -> int:
        return 17


def _ready(path: Path, **extra: object) -> None:
    value: dict[str, object] = {
        "schema": 1,
        "pid": 77,
        "state": "main-ready",
        "read_only": False,
        "write_enabled": True,
        "affordances": {"config_persistence": True, "session_persistence": True},
        "persistence_roundtrip": False,
    }
    value.update(extra)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_main_ui_readiness_requires_the_explicit_post_splash_contract(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    _ready(ready)

    value = portable_smoke._wait_for_ready(_Running(), ready, "writable", set())

    assert value["state"] == "main-ready"


def test_nonzero_startup_exit_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(portable_smoke.SmokeError, match="startup exit 17"):
        portable_smoke._wait_for_ready(_Exited(), tmp_path / "absent.json", "writable", set())


def test_read_only_ready_signal_rejects_enabled_write_affordances(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    _ready(ready, read_only=True, write_enabled=False, persistence_roundtrip=False, persistence=None)

    with pytest.raises(portable_smoke.SmokeError, match="enabled write affordance"):
        portable_smoke._wait_for_ready(_Running(), ready, "readonly", set())


def test_process_creation_permission_error_is_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "OMR Grader.exe"
    executable.write_bytes(b"fixture")

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(portable_smoke.subprocess, "Popen", denied)
    with pytest.raises(portable_smoke.SmokeError, match="process creation/containment failed"):
        portable_smoke._start(tmp_path, tmp_path / "ready.json", "readonly", "readonly", "token", tmp_path / "scope.json")


def test_tree_hashes_detects_same_name_content_changes(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"before":true}', encoding="utf-8")
    before = portable_smoke.tree_hashes(tmp_path)
    config.write_text('{"before":false}', encoding="utf-8")

    assert before != portable_smoke.tree_hashes(tmp_path)


def test_missing_or_false_ready_persistence_flags_fail_closed() -> None:
    digest = "a" * 64
    written = {
        "persistence": {
            "phase": "written",
            "default_sensitivity": 7,
            "config_sha256": digest,
        },
        "persistence_roundtrip": False,
    }
    persisted = portable_smoke._written_persistence(written)
    false_roundtrip = {
        "persistence": {
            "phase": "reopened",
            "default_sensitivity": 7,
            "config_sha256": digest,
        },
        "persistence_roundtrip": False,
    }

    with pytest.raises(portable_smoke.SmokeError, match="config roundtrip"):
        portable_smoke._reopened_persistence(false_roundtrip, persisted)
    with pytest.raises(portable_smoke.SmokeError, match="invalid roundtrip flag"):
        portable_smoke._written_persistence({"persistence": {"phase": "written"}})


def test_positive_roundtrip_compares_stable_values_not_phase() -> None:
    expected = {"default_sensitivity": 7, "config_sha256": "b" * 64}
    payload = {"persistence_roundtrip": True, "persistence": {"phase": "reopened", **expected}}
    assert portable_smoke._reopened_persistence(payload, expected)["phase"] == "reopened"
    payload["persistence"]["config_sha256"] = "c" * 64
    with pytest.raises(portable_smoke.SmokeError, match="config roundtrip"):
        portable_smoke._reopened_persistence(payload, expected)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_job_object_owns_and_terminates_a_real_suspended_child() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_SUSPENDED", 4),
    )
    job = portable_smoke._Job(process)
    try:
        portable_smoke._resume_suspended(process.pid)
        portable_smoke._force_tree_cleanup(process, job)
        assert process.poll() is not None
    finally:
        job.close()
