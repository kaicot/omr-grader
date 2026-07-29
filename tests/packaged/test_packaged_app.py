from __future__ import annotations

import ctypes
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
sys.path.insert(0, str(PACKAGING))
import build_release  # noqa: E402
import verify_release as verifier  # noqa: E402
from build_release import install_wheels, load_manifest, manifest_inventory  # noqa: E402
from verify_release import verify_release  # noqa: E402


def _wheel(
    wheelhouse: Path,
    name: str = "example",
    requires: str | list[str] | None = None,
    version: str = "1.0",
) -> Path:
    wheel = wheelhouse / f"{name}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    for requirement in [requires] if isinstance(requires, str) else requires or []:
        metadata += f"Requires-Dist: {requirement}\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
    return wheel


def _manifest(wheel: Path) -> dict[str, object]:
    return {
        "version": 1,
        "wheels": [
            {
                "filename": wheel.name,
                "size": wheel.stat().st_size,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "license": "MIT",
                "provenance": "Vendor release archive",
                "acquisition_record_id": "ACQ-2026-001",
            }
        ],
    }


def test_manifest_rejects_tampered_missing_and_noncanonical_schema(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = _wheel(wheelhouse)
    manifest_path = tmp_path / "wheels.json"
    manifest = _manifest(wheel)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert load_manifest(manifest_path, wheelhouse)["version"] == 1
    manifest["wheels"][0]["sha256"] = manifest["wheels"][0]["sha256"].upper()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="normalized SHA256"):
        load_manifest(manifest_path, wheelhouse)
    manifest["wheels"][0]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest["extra"] = "not allowed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly"):
        load_manifest(manifest_path, wheelhouse)
    manifest.pop("extra")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    wheel.unlink()
    with pytest.raises(ValueError, match="missing"):
        load_manifest(manifest_path, wheelhouse)


def test_manifest_rejects_unresolved_transitive_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = _wheel(wheelhouse, requires="not-in-wheelhouse (>=1)")
    manifest_path = tmp_path / "wheels.json"
    manifest_path.write_text(json.dumps(_manifest(wheel)), encoding="utf-8")
    manifest = load_manifest(manifest_path, wheelhouse)
    with pytest.raises(ValueError, match="unresolved transitive"):
        manifest_inventory(manifest, wheelhouse)


def test_install_uses_only_isolated_interpreter_and_offline_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = _wheel(wheelhouse)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        build_release, "run", lambda command, *, env: commands.append(command) or ""
    )

    install_wheels(_manifest(wheel), wheelhouse, tmp_path / "fresh-venv" / "python", {})

    assert len(commands) == 2
    assert all(command[0] == str(tmp_path / "fresh-venv" / "python") for command in commands)
    assert all("--no-index" in command and "--no-deps" in command for command in commands)
    assert "--no-build-isolation" in commands[1]
    assert sys.executable not in {command[0] for command in commands}


def test_verifier_rejects_missing_or_tampered_build_output_and_inventory(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = _wheel(wheelhouse)
    manifest_path = tmp_path / "wheels.json"
    manifest = _manifest(wheel)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()
    executable = release / "OMR Grader.exe"
    executable.write_bytes(b"release")
    receipt = {
        "format": 1,
        "timestamp_utc": "2026-01-01T00:00:00Z",
        "executable": {
            "filename": executable.name,
            "size": executable.stat().st_size,
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        },
        "source_hashes": verifier.source_hashes(),
        "config_hashes": {
            path: verifier.sha256_file(verifier.PROJECT_ROOT / path)
            for path in verifier.CONFIG_HASH_PATHS
        },
        "builder_sources": {
            path: verifier.sha256_file(verifier.PROJECT_ROOT / path)
            for path in verifier.BUILDER_SOURCE_PATHS
        },
        "wheel_manifest": {
            "filename": manifest_path.name,
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "wheels": manifest["wheels"],
        },
        "environment_inventory": {
            "prefix": "C:/isolated/build-venv",
            "distributions": [
                {"name": "example", "version": "1.0"},
                {"name": "omr-grader", "version": "0.1.0"},
                {"name": "pip", "version": "25.0"},
            ],
        },
        "tool_versions": {},
        "warnings": [],
    }
    receipt_path = release / "release-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    artifact_manifest = tmp_path / "artifact-manifest.json"
    artifact_manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "artifacts": [
                    {
                        "filename": item.name,
                        "size": item.stat().st_size,
                        "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                    }
                    for item in (executable, receipt_path)
                ],
            }
        ),
        encoding="utf-8",
    )
    verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)
    original_artifact_manifest = artifact_manifest.read_text(encoding="utf-8")
    artifact_binding = json.loads(original_artifact_manifest)
    artifact_binding["artifacts"][0]["sha256"] = "0" * 64
    artifact_manifest.write_text(json.dumps(artifact_binding), encoding="utf-8")
    with pytest.raises(ValueError, match="External artifact binding does not match"):
        verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)
    artifact_manifest.write_text(original_artifact_manifest, encoding="utf-8")

    source_hashes = receipt["source_hashes"].copy()
    config_hashes = receipt["config_hashes"].copy()
    builder_sources = receipt["builder_sources"].copy()
    receipt["source_hashes"].pop("main.py")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="source binding"):
        verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)
    receipt["source_hashes"] = source_hashes
    receipt["config_hashes"].pop("pyproject.toml")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="configuration binding"):
        verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)
    receipt["config_hashes"] = config_hashes
    receipt["builder_sources"].pop("packaging/build_release.py")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="builder source binding"):
        verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)
    receipt["builder_sources"] = builder_sources
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt["environment_inventory"]["distributions"].append({"name": "pytest", "version": "8.0"})
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)
    receipt["environment_inventory"]["distributions"].pop()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    executable.write_bytes(b"modified")
    with pytest.raises(ValueError, match="Release executable size does not match the receipt."):
        verify_release(release, manifest_path, wheelhouse, artifact_manifest=artifact_manifest)


def test_manifest_closure_binds_runtime_markers_and_propagates_extras(tmp_path: Path) -> None:

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    application = _wheel(
        wheelhouse,
        "application",
        [
            'dependency[feature]>=2,<3; sys_platform == "win32" and extra == ""',
            'inactive>=1; python_full_version < "3.12.0"',
        ],
    )
    dependency = _wheel(
        wheelhouse,
        "dependency",
        'featuredep>=1; extra == "feature"',
        version="2.1",
    )
    featuredep = _wheel(wheelhouse, "featuredep")
    manifest = _manifest(application)
    manifest["wheels"].extend(_manifest(dependency)["wheels"])
    manifest["wheels"].extend(_manifest(featuredep)["wheels"])
    manifest_path = tmp_path / "wheels.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_manifest(manifest_path, wheelhouse)
    assert manifest_inventory(loaded, wheelhouse) == {
        "application": "1.0",
        "dependency": "2.1",
        "featuredep": "1.0",
    }
    assert verifier.manifest_inventory(loaded, wheelhouse) == {
        "application": "1.0",
        "dependency": "2.1",
        "featuredep": "1.0",
    }


def test_manifest_closure_rejects_unbound_direct_references(tmp_path: Path) -> None:

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    application = _wheel(wheelhouse, "application", "dependency @ https://example.invalid/pkg.whl")
    manifest_path = tmp_path / "wheels.json"
    manifest_path.write_text(json.dumps(_manifest(application)), encoding="utf-8")
    loaded = load_manifest(manifest_path, wheelhouse)

    with pytest.raises(ValueError, match="direct reference"):
        manifest_inventory(loaded, wheelhouse)
    with pytest.raises(ValueError, match="direct reference"):
        verifier.manifest_inventory(loaded, wheelhouse)


def test_verifier_rejects_extra_release_payload_before_receipt_loading(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "unexpected.txt").write_text("not declared", encoding="utf-8")
    with pytest.raises(
        ValueError, match="Release payload does not exactly match the declared contract"
    ):
        verify_release(release, tmp_path / "missing.json", tmp_path / "wheelhouse")


def test_windows_job_owns_descendants_until_close(monkeypatch: pytest.MonkeyPatch) -> None:

    calls: list[tuple[object, ...]] = []

    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)  # type: ignore[operator]

    class Kernel32:
        CreateJobObjectW = Function(lambda *_args: calls.append(("create",)) or 17)
        SetInformationJobObject = Function(
            lambda *args: calls.append(("configure", args[1], args[2])) or True
        )
        AssignProcessToJobObject = Function(lambda *args: calls.append(("assign", args[1])) or True)
        QueryInformationJobObject = Function(lambda *_args: True)
        CloseHandle = Function(lambda handle: calls.append(("close", handle)) or True)

    class Process:
        pid = 42
        _handle = 99

    monkeypatch.setattr(verifier, "_windows_dll", lambda _name: Kernel32())
    job = verifier._WindowsJob(Process())  # type: ignore[arg-type]
    job.close()
    job.close()

    assert [call[:2] for call in calls] == [
        ("create",),
        ("configure", 9),
        ("assign", 99),
        ("close", 17),
    ]
    limits = ctypes.cast(
        calls[1][2], ctypes.POINTER(verifier._JobObjectExtendedLimitInformation)
    ).contents
    assert limits.BasicLimitInformation.LimitFlags == 0x00002000


def test_marker_runtime_rejects_non_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_release.platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(verifier.platform, "machine", lambda: "ARM64")

    with pytest.raises(ValueError, match="AMD64"):
        build_release._windows_marker_environment()
    with pytest.raises(ValueError, match="AMD64"):
        verifier._windows_marker_environment()


def test_smoke_assigns_suspended_process_before_resume_and_closes_job_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "OMR Grader.exe").write_bytes(b"portable")

    events: list[object] = []

    class Process:
        pid = 41
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            events.append(("wait", timeout))
            self.returncode = 0
            return 0

    class Job:
        def __init__(self, process: Process) -> None:
            events.append(("assign", process.pid))

        def process_ids(self) -> set[int]:
            return {41, 42}

        def close(self) -> None:
            events.append("job-close")

    monkeypatch.setattr(verifier.os, "name", "nt")
    monkeypatch.setattr(verifier.subprocess, "CREATE_SUSPENDED", 4, raising=False)
    monkeypatch.setattr(
        verifier.subprocess,
        "Popen",
        lambda *_args, **kwargs: events.append(("launch", kwargs["creationflags"])) or Process(),
    )
    monkeypatch.setattr(verifier, "_WindowsJob", Job)
    monkeypatch.setattr(
        verifier, "_resume_suspended_process", lambda pid: events.append(("resume", pid))
    )
    monkeypatch.setattr(verifier, "_window_titles", lambda pids: ["OMR Grader"])
    monkeypatch.setattr(verifier, "_close_windows", lambda pids: events.append(("close", pids)))

    verifier.smoke_portable(release, read_only=False)

    assert events == [
        ("launch", 4),
        ("assign", 41),
        ("resume", 41),
        ("close", {41, 42}),
        ("wait", 5),
        "job-close",
    ]


def test_spec_retains_fitz_hidden_import_for_packaged_runtime() -> None:
    assert '"fitz"' in (PACKAGING / "OMR_Grader.spec").read_text(encoding="utf-8")


def test_close_windows_reports_child_window_pid_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)  # type: ignore[operator]

    class User32:
        EnumWindows = Function(lambda callback, _parameter: callback(101, 0))
        GetWindowThreadProcessId = Function(
            lambda _window, process_id: setattr(
                ctypes.cast(process_id, ctypes.POINTER(ctypes.c_ulong)).contents, "value", 42
            )
            or 1
        )
        PostMessageW = Function(lambda *_args: False)

    monkeypatch.setattr(verifier, "_windows_dll", lambda _name: User32())
    monkeypatch.setattr(
        verifier.ctypes, "WINFUNCTYPE", lambda *_args: lambda callback: callback, raising=False
    )
    monkeypatch.setattr(verifier.ctypes, "get_last_error", lambda: 5, raising=False)

    with pytest.raises(RuntimeError, match="process 42: Windows error 5"):
        verifier._close_windows({41, 42})


def test_windows_job_reports_pid_enumeration_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)  # type: ignore[operator]

    class Kernel32:
        CreateJobObjectW = Function(lambda *_args: 17)
        SetInformationJobObject = Function(lambda *_args: True)
        AssignProcessToJobObject = Function(lambda *_args: True)
        QueryInformationJobObject = Function(lambda *_args: False)
        CloseHandle = Function(lambda *_args: True)

    class Process:
        pid = 41
        _handle = 99

    monkeypatch.setattr(verifier, "_windows_dll", lambda _name: Kernel32())
    monkeypatch.setattr(verifier.ctypes, "set_last_error", lambda _error: None, raising=False)
    monkeypatch.setattr(verifier.ctypes, "get_last_error", lambda: 5, raising=False)
    job = verifier._WindowsJob(Process())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Could not enumerate process job: Windows error 5"):
        job.process_ids()
    job.close()


def test_smoke_combines_nonzero_wm_close_and_job_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "OMR Grader.exe").write_bytes(b"portable")
    closed_process_ids: list[set[int]] = []

    class Process:
        pid = 41
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            self.returncode = 9
            return 9

    class Job:
        def __init__(self, _process: Process) -> None:
            pass

        def process_ids(self) -> set[int]:
            return {41, 42}

        def close(self) -> None:
            raise RuntimeError("job close failed")

    monkeypatch.setattr(verifier.os, "name", "nt")
    monkeypatch.setattr(verifier.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(verifier, "_WindowsJob", Job)
    monkeypatch.setattr(verifier, "_resume_suspended_process", lambda _pid: None)
    monkeypatch.setattr(verifier, "_window_titles", lambda _pids: ["OMR Grader"])
    monkeypatch.setattr(
        verifier, "_close_windows", lambda process_ids: closed_process_ids.append(process_ids)
    )

    with pytest.raises(
        RuntimeError,
        match="exited unsuccessfully after WM_CLOSE \\(9\\).*cleanup failed",
    ):
        verifier.smoke_portable(release, read_only=False)
    assert closed_process_ids == [{41, 42}]
