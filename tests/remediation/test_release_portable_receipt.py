from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("portable_release", ROOT / "tools" / "portable_release.py")
assert SPEC and SPEC.loader
portable_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = portable_release
SPEC.loader.exec_module(portable_release)


def _record(path: str, content: bytes) -> dict[str, object]:
    return {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _write_bundle(tmp_path: Path, *, archive_overrides: dict[str, bytes] | None = None) -> tuple[Path, Path]:
    release = tmp_path / "OMR-Grader-fixed21-20260906"
    payload = release / "OMR Grader"
    files = {"OMR Grader.exe": b"fixture exe", "_internal/Qt6Core.dll": b"fixture dll"}
    for relative, content in files.items():
        target = payload / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    records = [_record(path, content) for path, content in files.items()]
    receipt = {
        "format": 2,
        "product": "OMR Grader",
        "version": "2.1.0",
        "git_head": "a" * 40,
        "payload_root": "OMR Grader",
        "executable": records[0],
        "payload_files": records,
        "build_inputs": [{"path": "main.py", "sha256": "b" * 64}],
        "tools": {"python": "Python 3.12.13", "pyinstaller": "6.14.1", "pyside6": "6.9.1", "pyinstaller_hooks_contrib": "2026.6"},
    }
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (release / "release-receipt.json").write_bytes(receipt_bytes)
    archive = tmp_path / f"{release.name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(f"{release.name}/release-receipt.json", receipt_bytes)
        for relative, content in files.items():
            output.writestr(
                f"{release.name}/OMR Grader/{relative}",
                (archive_overrides or {}).get(relative, content),
            )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return release, archive


def _expect_rejected(
    release: Path, archive: Path, fragment: str, repository: Path | None = None
) -> None:
    with pytest.raises(portable_release.ReleaseVerificationError, match=fragment):
        portable_release.verify_release(release, archive, repository)


def test_format_2_control_bundle_has_exact_disk_and_zip_binding(tmp_path: Path) -> None:
    release, archive = _write_bundle(tmp_path)

    outcome = portable_release.verify_release(release, archive)

    assert outcome.status == "STRUCTURE_PASS"
    assert outcome.payload_files == 2
    assert outcome.global_approval is False


def test_normal_zip_directory_records_are_validated_but_not_payload_files(tmp_path: Path) -> None:
    release, archive = _write_bundle(tmp_path)
    with zipfile.ZipFile(archive, "a") as output:
        # Compress-Archive-style directory entry: safe ancestor, no file bytes.
        output.writestr(f"{release.name}/OMR Grader/_internal/", b"")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    assert portable_release.verify_release(release, archive).status == "STRUCTURE_PASS"


def test_extra_unrecorded_dll_is_rejected(tmp_path: Path) -> None:
    release, archive = _write_bundle(tmp_path)
    (release / "OMR Grader" / "_internal" / "surprise.dll").write_bytes(b"unrecorded")

    _expect_rejected(release, archive, "payload inventory differs")


@pytest.mark.parametrize("mutation, expected", [("zip-exe", "ZIP payload bytes differ"), ("zip-receipt", "ZIP receipt bytes differ")])
def test_zip_bytes_and_internal_receipt_are_not_trusted_by_name(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    overrides = {"OMR Grader.exe": b"tampered"} if mutation == "zip-exe" else None
    release, archive = _write_bundle(tmp_path, archive_overrides=overrides)
    if mutation == "zip-receipt":
        with zipfile.ZipFile(archive) as source:
            original = {
                item.filename: source.read(item)
                for item in source.infolist()
                if not item.is_dir()
            }
        with zipfile.ZipFile(archive, "w") as output:
            for name, content in original.items():
                output.writestr(
                    name,
                    b"{}" if name.endswith("release-receipt.json") else content,
                )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")

    _expect_rejected(release, archive, expected)


def test_missing_archive_sidecar_is_a_required_failure(tmp_path: Path) -> None:
    release, archive = _write_bundle(tmp_path)
    Path(f"{archive}.sha256").unlink()

    _expect_rejected(release, archive, "sidecar is missing")


@pytest.mark.parametrize("entry, expected", [("../escape/", "unsafe Windows path"), ("link", "symlink")])
def test_zip_rejects_unsafe_directory_and_symlink_records(tmp_path: Path, entry: str, expected: str) -> None:
    release, archive = _write_bundle(tmp_path)
    with zipfile.ZipFile(archive, "a") as output:
        if entry == "link":
            info = zipfile.ZipInfo(entry)
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            output.writestr(info, b"outside")
        else:
            output.writestr(entry, b"")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    _expect_rejected(release, archive, expected)


@pytest.mark.parametrize(
    ("field", "replacement", "expected"),
    [
        ("payload_files", [{"path": "../parent.txt", "size": 1, "sha256": "a" * 64}], "unsafe Windows path"),
        ("payload_files", [{"path": "A.dll", "size": 1, "sha256": "a" * 64}, {"path": "a.dll", "size": 1, "sha256": "a" * 64}], "case-colliding"),
        ("executable", {"path": "OMR Grader.exe", "size": 999, "sha256": "a" * 64}, "matching payload"),
        ("build_inputs", [{"path": "C:/outside", "sha256": "a" * 64}], "drive or UNC"),
    ],
)
def test_receipt_rejects_paths_aliases_and_ignored_record_hashes(
    tmp_path: Path, field: str, replacement: object, expected: str
) -> None:
    release, archive = _write_bundle(tmp_path)
    receipt = json.loads((release / "release-receipt.json").read_text())
    receipt[field] = replacement
    (release / "release-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    _expect_rejected(release, archive, expected)


def test_duplicate_json_key_is_rejected_before_file_access(tmp_path: Path) -> None:
    release, archive = _write_bundle(tmp_path)
    (release / "release-receipt.json").write_bytes(b'{"format":2,"format":2}')

    _expect_rejected(release, archive, "duplicate JSON key")


def test_format_1_is_only_a_limited_audit(tmp_path: Path) -> None:
    release = tmp_path / "legacy"
    (release / "OMR Grader").mkdir(parents=True)
    (release / "release-receipt.json").write_text('{"format":1}', encoding="utf-8")
    archive = tmp_path / "legacy.zip"
    archive.write_bytes(b"not inspected for legacy audit")

    outcome = portable_release.verify_release(release, archive)

    assert outcome.status == "LEGACY_AUDIT_ONLY"
    assert outcome.global_approval is False


def test_source_input_hash_change_is_rejected_when_provenance_is_requested(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    for relative in (
        "main.py",
        "pyproject.toml",
        "packaging/OMR_Grader.spec",
        "tools/build-portable-folder.ps1",
        "tools/verify-portable-folder.ps1",
        "tools/smoke-portable-onedir.py",
        "tools/portable_release.py",
        "requirements/direct-pins.txt",
        "constraints/windows-py312.lock",
    ):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)
    snapshot = portable_release.snapshot_inputs(repository)
    release, archive = _write_bundle(tmp_path)
    receipt = json.loads((release / "release-receipt.json").read_text())
    receipt["git_head"] = snapshot["git_head"]
    receipt["build_inputs"] = snapshot["inputs"]
    (release / "release-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    # The archive intentionally remains stale, so first rebuild it with exactly
    # the independent fixture bytes.
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(f"{release.name}/release-receipt.json", (release / "release-receipt.json").read_bytes())
        for item in receipt["payload_files"]:
            output.write(release / "OMR Grader" / item["path"], f"{release.name}/OMR Grader/{item['path']}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    assert portable_release.verify_release(release, archive, repository).status == "STRUCTURE_PASS"
    (repository / "tools" / "portable_release.py").write_text("changed", encoding="utf-8")

    _expect_rejected(release, archive, "release source is not clean", repository)
