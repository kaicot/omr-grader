from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WRITER_PATH = ROOT / "tools" / "write_installed_distributions_receipt.py"
_MISSING = object()


def _load(path: Path, name: str):
    dependency_name = "supply_manifest"
    original = sys.modules.pop(dependency_name, _MISSING)
    try:
        dependency_spec = importlib.util.spec_from_file_location(
            dependency_name, ROOT / "tools" / "supply_manifest.py"
        )
        assert dependency_spec and dependency_spec.loader
        dependency = importlib.util.module_from_spec(dependency_spec)
        sys.modules[dependency_name] = dependency
        dependency_spec.loader.exec_module(dependency)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if original is _MISSING:
            sys.modules.pop(dependency_name, None)
        else:
            sys.modules[dependency_name] = original

def test_isolated_loader_restores_preexisting_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = object()
    monkeypatch.setitem(sys.modules, "supply_manifest", original)

    _load(WRITER_PATH, "receipt_writer_restoration_test")

    assert sys.modules["supply_manifest"] is original


def test_isolated_loader_restores_preexisting_dependency_when_dependency_execution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = object()
    monkeypatch.setitem(sys.modules, "supply_manifest", original)
    original_spec_from_file_location = importlib.util.spec_from_file_location

    class RaisingLoader:
        def create_module(self, spec):
            return None

        def exec_module(self, module) -> None:
            raise RuntimeError("dependency execution failed")

    def failing_dependency_spec(name, location, *args, **kwargs):
        if name == "supply_manifest":
            return importlib.machinery.ModuleSpec(name, RaisingLoader())
        return original_spec_from_file_location(name, location, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", failing_dependency_spec)
    with pytest.raises(RuntimeError, match="dependency execution failed"):
        _load(WRITER_PATH, "receipt_writer_failed_dependency_test")
    assert sys.modules["supply_manifest"] is original
def test_isolated_loader_restores_preexisting_dependency_when_dependency_setup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = object()
    monkeypatch.setitem(sys.modules, "supply_manifest", original)

    def failing_dependency_spec(name, location, *args, **kwargs):
        if name == "supply_manifest":
            raise RuntimeError("dependency setup failed")
        return importlib.util.spec_from_file_location(name, location, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", failing_dependency_spec)
    with pytest.raises(RuntimeError, match="dependency setup failed"):
        _load(WRITER_PATH, "receipt_writer_failed_dependency_setup_test")
    assert sys.modules["supply_manifest"] is original



def _digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    writer = _load(WRITER_PATH, "receipt_writer_test")
    site = tmp_path / "site"
    info = site / "foo-1.0.dist-info"
    info.mkdir(parents=True)
    files = {
        "foo.py": b"VALUE = 1\n",
        "foo-1.0.dist-info/METADATA": b"Name: Foo\nVersion: 1.0\n",
        "foo-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    }
    record = (
        "\n".join(f"{name},{_digest(data)},{len(data)}" for name, data in files.items())
        + "\nfoo-1.0.dist-info/RECORD,,\n"
    )
    files["foo-1.0.dist-info/RECORD"] = record.encode()
    for name, data in files.items():
        target = site / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    bundle = tmp_path / "supply" / "windows-py312" / "fixture"
    wheel_path = bundle / "wheelhouse" / "foo-1.0-py3-none-any.whl"
    wheel_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, data in files.items():
            wheel.writestr(name, data)

    support = {
        "application.lock": b"",
        "bootstrap.lock": b"foo==1.0\n",
        "constraints/windows-py312.lock": b"",
        "cpython-3.12.10-amd64.exe": b"installer",
        "licenses/foo.txt": b"MIT\n",
        "pip-25.1.1.pyz": b"pip",
    }
    for name, data in support.items():
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    artifacts = []
    for path in sorted(
        [wheel_path, *(bundle / name for name in support)],
        key=lambda item: item.relative_to(bundle).as_posix().encode("utf-8"),
    ):
        relative = path.relative_to(bundle).as_posix()
        file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        wheel_artifact = relative.startswith("wheelhouse/")
        artifacts.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": file_sha256,
                "role": "wheel" if wheel_artifact else "support",
                "distribution": "Foo" if wheel_artifact else "OMR Grader support",
                "version": "1.0",
                "wheel_tags": ["py3-none-any"] if wheel_artifact else [],
                "upstream_url": "https://example.invalid/foo",
                "license": "MIT",
                "signing_evidence": {
                    "scheme": "sha256",
                    "identity": "fixture",
                    "signed_at": "2026-01-01T00:00:00Z",
                    "digest": file_sha256,
                },
            }
        )
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
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    locks = [
        bundle / "bootstrap.lock",
        bundle / "constraints/windows-py312.lock",
        bundle / "application.lock",
    ]
    distribution = __import__("importlib.metadata", fromlist=["PathDistribution"]).PathDistribution(
        info
    )
    monkeypatch.setattr(writer, "_site_roots", lambda: [site])
    monkeypatch.setattr(writer.importlib.metadata, "distributions", lambda: [distribution])
    monkeypatch.setattr(writer, "_run_pip_check", lambda: "No broken requirements found.\n")
    return writer, bundle, digest, locks, site


def test_writes_canonical_manifest_bound_minimal_wheel_receipt(tmp_path, monkeypatch):
    writer, bundle, digest, locks, _ = _fixture(tmp_path, monkeypatch)
    pyinstaller_artifact = tmp_path / "PyInstaller.exe"
    pyinstaller_artifact.write_bytes(b"pinned-pyinstaller")
    receipt = writer.build_receipt(
        bundle, digest, *locks, pyinstaller_artifact, "f" * 64
    )
    assert list(receipt) == [
        "schema_version",
        "python",
        "bundle",
        "locks",
        "distributions",
        "pip_check",
        "pip_list",
        "expected_set",
        "unexpected_set",
        "missing_set",
        "pyinstaller_artifact",
    ]
    assert receipt["distributions"][0]["name"] == "foo"
    assert receipt["distributions"][0]["installed_files"][-1]["path"] == "foo.py"


def test_rejects_installed_file_tamper_with_wheel_digest_diagnostic(tmp_path, monkeypatch):
    writer, bundle, digest, locks, site = _fixture(tmp_path, monkeypatch)
    (site / "foo.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(writer.ReceiptError, match=r"installed file differs from wheel: foo\.py"):
        writer.build_receipt(bundle, digest, *locks, None, "f" * 64)


def test_rejects_manifest_digest_drift_with_external_approval_diagnostic(tmp_path, monkeypatch):
    writer, bundle, digest, locks, _ = _fixture(tmp_path, monkeypatch)

    with pytest.raises(writer.ReceiptError, match="external manifest SHA-256 does not match"):
        writer.build_receipt(bundle, "0" * len(digest), *locks, None, "f" * 64)
