from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TOOLS = ROOT / "tools"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell behavior tests require Windows PowerShell or pwsh")
    return executable


def _artifact(path: Path, root: Path, *, role: str, tags: list[str]) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "role": role,
        "distribution": "fixture",
        "version": "1.0",
        "wheel_tags": tags,
        "upstream_url": "https://example.invalid/fixture",
        "license": "MIT",
        "signing_evidence": "fixture",
    }


def _bundle(tmp_path: Path) -> tuple[Path, str]:
    bundle = tmp_path / "supply" / "windows-py312" / "fixture"
    contents = {
        "cpython-3.12.10-amd64.exe": b"installer",
        "pip-25.1.1.pyz": b"pip",
        "bootstrap.lock": b"",
        "constraints/windows-py312.lock": b"",
        "application.lock": b"",
        "wheelhouse/fixture-1.0-py3-none-any.whl": b"wheel",
        "licenses/fixture.txt": b"license",
    }
    for name, content in contents.items():
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    artifacts = [
        _artifact(
            bundle / name,
            bundle,
            role="wheel" if name.startswith("wheelhouse/") else "fixture",
            tags=["py3-none-any"] if name.startswith("wheelhouse/") else [],
        )
        for name in sorted(contents)
    ]
    manifest = {
        "schema_version": 1,
        "release_id": "fixture",
        "created_at": "2026-01-01T00:00:00Z",
        "target": {"platform": "windows", "architecture": "x64", "python": "3.12.10"},
        "artifacts": artifacts,
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    return bundle, hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    release.mkdir()
    executable = release / "OMR Grader.exe"
    executable.write_bytes(b"fixture executable")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    (release / "OMR Grader.exe.sha256").write_text(f"{digest}  OMR Grader.exe", encoding="utf-8")
    (release / "release-receipt.json").write_text("{}", encoding="utf-8")
    return release


def _fixture_python(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "fixture-python.log"
    command = tmp_path / "fixture-python.cmd"
    command.write_text(
        "@echo off\n"
        "echo ARGS=%*;PIP_NO_INDEX=%PIP_NO_INDEX%;NO_PROXY=%NO_PROXY%;"
        "no_proxy=%no_proxy%;HTTP_PROXY=%HTTP_PROXY%;HTTPS_PROXY=%HTTPS_PROXY%;"
        'ALL_PROXY=%ALL_PROXY%;RELEASE=%OMR_GRADER_RELEASE_DIR%>> "%FIXTURE_LOG%"\n'
        'if "%1"=="-m" if "%2"=="pytest" exit /b %FIXTURE_PYTEST_EXIT%\n'
        "exit /b 0\n",
        encoding="utf-8",
    )
    return command, log


def _run(script: str, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(TOOLS / script),
            *arguments,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _environment(log: Path, *, pytest_exit: int = 0) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "FIXTURE_LOG": str(log),
            "FIXTURE_PYTEST_EXIT": str(pytest_exit),
            "TEMP": str(log.parent),
            "TMP": str(log.parent),
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    return environment


def _run_packaged(
    tmp_path: Path, bundle: Path, digest: str, *, pytest_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], Path]:
    python, log = _fixture_python(tmp_path)
    result = _run(
        "run-packaged-tests.ps1",
        "-BundleRoot",
        str(bundle),
        "-ExpectedManifestSha256",
        digest,
        "-Python",
        str(python),
        "-ReleaseRoot",
        str(_release(tmp_path)),
        env=_environment(log, pytest_exit=pytest_exit),
    )
    return result, log


@pytest.mark.parametrize("bundle_state", ["wrong-digest", "missing-manifest"])
def test_invalid_manifest_aborts_before_any_packaged_action(
    tmp_path: Path, bundle_state: str
) -> None:
    bundle, digest = _bundle(tmp_path)
    if bundle_state == "wrong-digest":
        digest = "0" * 64
    else:
        (bundle / "manifest.json").unlink()

    result, log = _run_packaged(tmp_path, bundle, digest)

    assert result.returncode != 0
    assert not log.exists(), result.stdout + result.stderr


def test_verified_bundle_invokes_receipt_verifier_and_packaged_tests_offline(
    tmp_path: Path,
) -> None:
    bundle, digest = _bundle(tmp_path)
    result, log = _run_packaged(tmp_path, bundle, digest)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any(
        "verify_release.py" in call and f"--release {_release_path(tmp_path)}" in call
        for call in calls
    )
    pytest_call = next(call for call in calls if "ARGS=-m pytest" in call)
    assert f"RELEASE={_release_path(tmp_path)}" in pytest_call
    assert "PIP_NO_INDEX=1" in pytest_call
    assert "HTTP_PROXY=http://127.0.0.1:9" in pytest_call
    assert "HTTPS_PROXY=http://127.0.0.1:9" in pytest_call
    assert "ALL_PROXY=http://127.0.0.1:9" in pytest_call
    assert "NO_PROXY=*" not in pytest_call
    assert "no_proxy=*" not in pytest_call


def _release_path(tmp_path: Path) -> str:
    return str(tmp_path / "release")


def test_packaged_test_native_exit_code_propagates(tmp_path: Path) -> None:
    bundle, digest = _bundle(tmp_path)

    result, log = _run_packaged(tmp_path, bundle, digest, pytest_exit=47)

    assert result.returncode == 47, result.stdout + result.stderr
    assert any("ARGS=-m pytest" in call for call in log.read_text(encoding="utf-8").splitlines())


def test_smoke_delegates_writable_and_read_only_modes_to_receipt_verifier(
    tmp_path: Path,
) -> None:
    bundle, digest = _bundle(tmp_path)
    python, log = _fixture_python(tmp_path)
    release = _release(tmp_path)
    other_drive = ROOT / "artifacts"
    reparse_probe = subprocess.run(
        ["fsutil", "reparsepoint", "query", str(other_drive)],
        capture_output=True,
        text=True,
        check=False,
    )
    if other_drive.is_symlink() or reparse_probe.returncode == 0:
        pytest.skip("The workspace artifacts path is a reparse point, not a smoke volume")

    result = _run(
        "smoke-release.ps1",
        "-BundleRoot",
        str(bundle),
        "-ExpectedManifestSha256",
        digest,
        "-Python",
        str(python),
        "-ReleaseRoot",
        str(release),
        "-SmokeRoot",
        str(tmp_path / "smoke"),
        "-OtherDriveRoot",
        str(other_drive),
        env=_environment(log),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    smoke_calls = [
        call for call in log.read_text(encoding="utf-8").splitlines() if "--smoke" in call
    ]
    assert len(smoke_calls) == 1
    verifier = (ROOT / "packaging" / "verify_release.py").read_text(encoding="utf-8")
    assert "smoke_portable(args.release, read_only=False)" in verifier
    assert "smoke_portable(args.release, read_only=True)" in verifier
