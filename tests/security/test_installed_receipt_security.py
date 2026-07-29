from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PACKAGED = ROOT / "tests" / "packaged" / "test_installed_receipt.py"
VERIFIER = ROOT / "tools" / "verify_installed_distributions_receipt.py"
SUPPLY_MANIFEST = ROOT / "tools" / "supply_manifest.py"


def _load(path: Path, name: str):
    if path == VERIFIER and "supply_manifest" not in sys.modules:
        dependency_spec = importlib.util.spec_from_file_location("supply_manifest", SUPPLY_MANIFEST)
        assert dependency_spec and dependency_spec.loader
        dependency = importlib.util.module_from_spec(dependency_spec)
        sys.modules["supply_manifest"] = dependency
        dependency_spec.loader.exec_module(dependency)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "cafe\u0301.py",
        "name.",
        "name ",
        "CON.txt",
        "name\x1fcontrol",
        *[f"name{character}forbidden" for character in '<>:"|?*'],
        r"nested\path",
        "nested//path",
        "/absolute",
        "//server/share",
        "C:/drive",
        "C:relative-drive",
        "./path",
        "nested/../path",
    ],
)
def test_verifier_rejects_nonportable_record_and_receipt_paths(tmp_path, unsafe_path):
    verifier = _load(VERIFIER, "installed_receipt_path_verifier_test")
    record_name = "foo-1.0.dist-info/RECORD"
    wheel = tmp_path / "foo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(unsafe_path, b"")
        archive.writestr(
            record_name,
            '"{}",,\n{},,\n'.format(unsafe_path.replace('"', '""'), record_name),
        )

    with pytest.raises(verifier.VerificationError, match="unsafe installed path"):
        verifier._wheel_record(wheel, "foo", "1.0")
    # Receipt paths are validated before resolving them under a site root.
    with pytest.raises(verifier.VerificationError, match="unsafe installed path"):
        verifier._safe_path(unsafe_path)


def test_rejects_editable_missing_record_and_path_aliases(tmp_path, monkeypatch):
    fixture = _load(PACKAGED, "installed_receipt_fixture")
    writer, bundle, digest, locks, site = fixture._fixture(tmp_path, monkeypatch)
    info = site / "foo-1.0.dist-info"
    (info / "direct_url.json").write_text('{"url":"file:///source"}', encoding="utf-8")
    with pytest.raises(writer.ReceiptError, match="direct URL"):
        writer.build_receipt(bundle, digest, *locks, None, "f" * 64)
    (info / "direct_url.json").unlink()
    (info / "RECORD").unlink()
    with pytest.raises(writer.ReceiptError, match="missing file"):
        writer.build_receipt(bundle, digest, *locks, None, "f" * 64)
    # A case-folded alias is rejected before it can make ownership ambiguous on Windows.
    (info / "RECORD").write_text("foo.py,,\nFOO.py,,\n", encoding="utf-8")
    with pytest.raises(writer.ReceiptError, match="alias"):
        writer.build_receipt(bundle, digest, *locks, None, "f" * 64)


def test_rejects_undeclared_distribution_and_unowned_file(tmp_path, monkeypatch):
    fixture = _load(PACKAGED, "installed_receipt_fixture_undeclared")
    writer, bundle, digest, locks, site = fixture._fixture(tmp_path, monkeypatch)
    (site / "injected.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(writer.ReceiptError, match="unowned"):
        writer.build_receipt(bundle, digest, *locks, None, "f" * 64)
    (site / "injected.py").unlink()
    external_constraints = tmp_path / "external-constraints.lock"
    external_constraints.write_text("evil==9\n", encoding="utf-8")
    mutated_locks = [locks[0], external_constraints, locks[2]]
    with pytest.raises(writer.ReceiptError, match="installed set mismatch"):
        writer.build_receipt(bundle, digest, *mutated_locks, None, "f" * 64)


def test_verifier_rejects_lock_and_writer_hash_mismatch(tmp_path):
    verifier = _load(VERIFIER, "installed_receipt_verifier_test")
    receipt = {
        "schema_version": "installed-distributions-v1",
        "python": {
            "path": str(Path(verifier.sys.executable).resolve()),
            "version": verifier.platform.python_version(),
            "architecture": verifier.platform.machine(),
            "exe_sha256": verifier._sha256(Path(verifier.sys.executable)),
        },
        "bundle": {
            "path": str(tmp_path),
            "manifest_sha256": "0" * 64,
            "writer_tool_sha256": "f" * 64,
            "verifier_tool_sha256": "f" * 64,
            "release_id": "x",
        },
        "locks": {
            "bootstrap": {"path": str(tmp_path / "missing"), "sha256": "0" * 64},
            "constraints": {"path": str(tmp_path / "missing"), "sha256": "0" * 64},
            "application": {"path": str(tmp_path / "missing"), "sha256": "0" * 64},
        },
        "distributions": [],
        "pip_check": "",
        "pip_list": [],
        "expected_set": [],
        "unexpected_set": [],
        "missing_set": [],
        "pyinstaller_artifact": None,
    }
    path = tmp_path / "receipt.json"
    path.write_bytes((json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode())
    with pytest.raises(
        verifier.VerificationError,
        match="caller-approved manifest and writer identities",
    ):
        verifier.verify(path, "0" * 64, "0" * 64)
