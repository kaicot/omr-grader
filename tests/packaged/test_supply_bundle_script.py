from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "tools" / "verify-release-bundle.ps1"
SUPPLY_MANIFEST = SCRIPT.with_name("supply_manifest.py")


def _powershell() -> str:
    host = shutil.which("pwsh") or shutil.which("powershell")
    if host is None:
        pytest.skip("PowerShell host unavailable; release-bundle boundary cannot run")
    return host


def _write_bundle(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "supply" / "windows-py312" / "rc27-test"
    files = {
        "application.lock": b"foo==1.0\n",
        "bootstrap.lock": b"bootstrap==1.0\n",
        "constraints/windows-py312.lock": b"constraint==1.0\n",
        "cpython-3.12.10-amd64.exe": b"cpython",
        "licenses/foo.txt": b"MIT\n",
        "pip-25.1.1.pyz": b"pip",
        "wheelhouse/foo-1.0-py3-none-any.whl": b"wheel",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    artifacts = []
    for relative in sorted(files, key=lambda item: item.encode("utf-8")):
        content = files[relative]
        artifacts.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "role": "wheel" if relative.startswith("wheelhouse/") else "support",
                "distribution": "Foo" if relative.startswith("wheelhouse/") else "",
                "version": "1.0" if relative.startswith("wheelhouse/") else "",
                "wheel_tags": ["py3-none-any"] if relative.startswith("wheelhouse/") else [],
                "upstream_url": "https://example.invalid/foo",
                "license": "MIT",
                "signing_evidence": "fixture",
            }
        )
    manifest = {
        "schema_version": 1,
        "release_id": root.name,
        "created_at": "2026-01-01T00:00:00Z",
        "target": {"platform": "windows", "architecture": "x64", "python": "3.12.10"},
        "artifacts": artifacts,
    }
    manifest_bytes = (
        json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    )
    (root / "manifest.json").write_bytes(manifest_bytes)
    return root, hashlib.sha256(manifest_bytes).hexdigest()


def _run_verifier(
    root: Path, digest: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = [
        _powershell(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-BundleRoot",
        str(root),
        "-ExpectedManifestSha256",
        digest,
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False, env=environment)


def test_release_bundle_script_verifies_canonical_bundle_and_propagates_verifier_failures(
    tmp_path: Path,
) -> None:
    root, digest = _write_bundle(tmp_path)

    result = _run_verifier(root, digest)

    assert result.returncode == 0, result.stderr

    inventory_root, inventory_digest = _write_bundle(tmp_path / "bad-inventory")
    (inventory_root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    inventory = _run_verifier(inventory_root, inventory_digest)
    assert inventory.returncode == 70
    assert "SUPPLY_BUNDLE_LAYOUT: unexpected bundle path" in inventory.stderr

    hash_root, hash_digest = _write_bundle(tmp_path / "bad-hash")
    (hash_root / "wheelhouse" / "foo-1.0-py3-none-any.whl").write_bytes(b"other")
    hash_failure = _run_verifier(hash_root, hash_digest)
    assert hash_failure.returncode == 70
    assert (
        "SUPPLY_FILE_INTEGRITY: size or SHA-256 mismatch: "
        "wheelhouse/foo-1.0-py3-none-any.whl" in hash_failure.stderr
    )


def _ambient_python_double(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ambient-python.log"
    command = tmp_path / "python.cmd"
    command.write_text(
        f'@echo off\r\necho invoked> "{log}"\r\nexit /b 0\r\n',
        encoding="utf-8",
        newline="",
    )
    return command, log


def test_fake_ambient_python_cannot_intercept_the_release_bundle_verifier(tmp_path: Path) -> None:
    root, digest = _write_bundle(tmp_path)
    ambient, ambient_log = _ambient_python_double(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = str(ambient.parent) + os.pathsep + environment.get("PATH", "")

    command = [
        _powershell(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-BundleRoot",
        str(root),
        "-ExpectedManifestSha256",
        digest,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False, env=environment)

    assert result.returncode == 0, result.stderr
    assert not ambient_log.exists()
