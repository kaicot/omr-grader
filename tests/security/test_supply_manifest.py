from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).parents[2] / "tools" / "supply_manifest.py"
spec = importlib.util.spec_from_file_location("supply_manifest", MODULE_PATH)
assert spec and spec.loader
supply_manifest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = supply_manifest
spec.loader.exec_module(supply_manifest)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(root: Path, relative: str, *, tags: list[str] | None = None) -> dict[str, Any]:
    path = root / relative
    return {
        "path": relative,
        "size": path.stat().st_size,
        "sha256": _digest(path),
        "role": "runtime",
        "distribution": "example",
        "version": "1.0",
        "wheel_tags": tags or [],
        "upstream_url": "https://example.invalid/example",
        "license": "MIT",
        "signing_evidence": {
            "scheme": "sha256",
            "identity": "fixture",
            "signed_at": "2026-01-01T00:00:00Z",
            "digest": _digest(path),
        },
    }


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "supply" / "windows-py312" / "release-1"
    files = {
        "cpython-3.12.10-amd64.exe": b"cpython",
        "pip-25.1.1.pyz": b"pip",
        "bootstrap.lock": b"--require-hashes\n",
        "constraints/windows-py312.lock": b"--require-hashes\n",
        "application.lock": b"--require-hashes\n",
        "wheelhouse/example-1.0-cp312-cp312-win_amd64.whl": b"wheel",
        "licenses/example.txt": b"MIT\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    wheel = root / "wheelhouse/example-1.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "example-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: example\nVersion: 1.0\n",
        )
    artifacts = [
        _artifact(
            root, relative, tags=["cp312-cp312-win_amd64"] if relative.endswith(".whl") else None
        )
        for relative in sorted(files, key=lambda item: item.encode("utf-8"))
    ]
    manifest = {
        "schema_version": 1,
        "release_id": "release-1",
        "created_at": "2026-07-28T00:00:00Z",
        "target": {"platform": "windows", "architecture": "x64", "python": "3.12.10"},
        "artifacts": artifacts,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    )
    return root, manifest_path


def _verify(root: Path, manifest: Path) -> None:
    supply_manifest.verify_bundle(root, _digest(manifest))


def _read_manifest(manifest: Path) -> dict[str, Any]:
    return json.loads(manifest.read_text(encoding="utf-8"))


def _write_manifest(manifest: Path, value: dict[str, Any]) -> None:
    manifest.write_bytes(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    )


def test_valid_fixture_is_verified(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    _verify(root, manifest)


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_manifest_schema_is_exact(tmp_path: Path, mutation: str) -> None:
    root, manifest = _bundle(tmp_path)
    value = _read_manifest(manifest)
    if mutation == "extra":
        value["unexpected"] = True
    else:
        del value["created_at"]
    _write_manifest(manifest, value)
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code == supply_manifest.ExitCode.MANIFEST_FORMAT


def test_artifacts_must_be_utf8_sorted_and_cannot_list_manifest(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    value = _read_manifest(manifest)
    value["artifacts"] = list(reversed(value["artifacts"]))
    _write_manifest(manifest, value)
    with pytest.raises(supply_manifest.SupplyManifestError, match="sorted"):
        _verify(root, manifest)
    value["artifacts"] = _read_manifest(_bundle(tmp_path / "other")[1])["artifacts"]
    value["artifacts"].append(_artifact(root, "manifest.json"))
    _write_manifest(manifest, value)
    with pytest.raises(supply_manifest.SupplyManifestError, match="manifest must not list itself"):
        _verify(root, manifest)


@pytest.mark.parametrize(
    "path", ["../escape", "wheelhouse/../escape", "C:/drive", "wheelhouse/name:ads.whl", "a\\b"]
)
def test_traversal_and_alias_paths_are_rejected(tmp_path: Path, path: str) -> None:
    root, manifest = _bundle(tmp_path)
    value = _read_manifest(manifest)
    value["artifacts"][0]["path"] = path
    _write_manifest(manifest, value)
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code in {
        supply_manifest.ExitCode.UNSAFE_PATH,
        supply_manifest.ExitCode.MANIFEST_FORMAT,
    }


def test_symlink_is_rejected(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    link = root / "licenses" / "alias.txt"
    try:
        os.symlink(root / "licenses" / "example.txt", link)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code == supply_manifest.ExitCode.UNSAFE_PATH


def test_hash_and_size_mismatches_are_rejected(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    (root / "pip-25.1.1.pyz").write_bytes(b"changed")
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code == supply_manifest.ExitCode.FILE_INTEGRITY


def test_missing_and_extra_files_are_rejected(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    (root / "application.lock").unlink()
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code == supply_manifest.ExitCode.BUNDLE_LAYOUT
    root, manifest = _bundle(tmp_path / "extra")
    (root / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code == supply_manifest.ExitCode.BUNDLE_LAYOUT


def test_sdist_and_wrong_external_digest_are_rejected_before_use(tmp_path: Path) -> None:
    root, manifest = _bundle(tmp_path)
    sdist = root / "wheelhouse" / "example-1.0.tar.gz"
    sdist.write_bytes(b"sdist")
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        _verify(root, manifest)
    assert raised.value.code == supply_manifest.ExitCode.BUNDLE_LAYOUT
    with pytest.raises(supply_manifest.SupplyManifestError) as raised:
        supply_manifest.verify_bundle(root, "0" * 64)
    assert raised.value.code == supply_manifest.ExitCode.MANIFEST_DIGEST
