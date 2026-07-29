from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import BackupValidateRequest
from omr_grader.application.validation_token import ValidatedBackup
from omr_grader.domain.enums import LineageState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.domain.models import RestoreProvenance
from omr_grader.infrastructure.backup_archive import (
    BackupArchive,
    StagingOwnership,
    _move_directory_no_replace,
    _RestorePlatformUnavailable,
    _WindowsRestoreAdapter,
)
from omr_grader.infrastructure.session_store import (
    _path_identity,
    _publish_directory_no_replace,
    _WindowsVerifiedRestoreTree,
)


def _hostile_archive(path: Path, name: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"x")


def test_preflight_rejects_portable_path_escapes_and_missing_manifest(tmp_path: Path) -> None:
    service = BackupApplicationService(object())  # type: ignore[arg-type]
    for number, name in enumerate(
        ("../escape", "C:/drive", "//unc", "name:stream", "CON", "a/../b")
    ):
        archive = tmp_path / f"hostile-{number}.omrbak"
        _hostile_archive(archive, name)
        result = service.validate_backup(BackupValidateRequest(str(archive)))
        assert isinstance(result, Err)


def test_preflight_rejects_encrypted_and_symlink_metadata(tmp_path: Path) -> None:
    service = BackupApplicationService(object())  # type: ignore[arg-type]
    encrypted = tmp_path / "encrypted.omrbak"
    with zipfile.ZipFile(encrypted, "w") as archive:
        entry = zipfile.ZipInfo("omrbak-v1/archive_manifest.json")
        entry.flag_bits |= 1
        archive.writestr(entry, b"{}")
    assert isinstance(service.validate_backup(BackupValidateRequest(str(encrypted))), Err)


def test_preflight_rejects_high_compression_archive_before_restore(tmp_path: Path) -> None:
    archive = tmp_path / "compressed.omrbak"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("payload", b"x" * 1024 * 1024)
    service = BackupApplicationService(object())  # type: ignore[arg-type]
    assert isinstance(service.validate_backup(BackupValidateRequest(str(archive))), Err)


def test_borrowed_inspection_close_and_replay_cannot_consume_token(tmp_path: Path) -> None:
    source = tmp_path / "backup.omrbak"
    source.write_bytes(b"validated backup")
    opened = ValidatedBackup.open(str(source))
    assert isinstance(opened, Ok)
    token = opened.value
    escaped: list[Any] = []

    def inspect(handle: Any) -> Ok[str]:
        escaped.append(handle)
        assert handle.read() == b"validated backup"
        handle.close()
        return Ok("inspected")

    try:
        first = token.inspect_revalidated(inspect)
        second = token.inspect_revalidated(lambda handle: Ok(handle.read()))
        assert isinstance(first, Ok)
        assert first.value == "inspected"
        assert isinstance(second, Ok)
        assert second.value == b"validated backup"
        with pytest.raises(ValueError, match="borrowed inspection handle is closed"):
            escaped[0].read()

        consumed = token.consume_for_restore()
        assert isinstance(consumed, Ok)
        assert consumed.value.read() == b"validated backup"
        assert isinstance(token.consume_for_restore(), Err)
    finally:
        assert isinstance(token.close(), Ok)


def test_borrowed_inspection_exception_invalidates_escaped_view(tmp_path: Path) -> None:
    source = tmp_path / "backup.omrbak"
    source.write_bytes(b"validated backup")
    opened = ValidatedBackup.open(str(source))
    assert isinstance(opened, Ok)
    token = opened.value
    escaped: list[Any] = []

    def fail(handle: Any) -> Ok[None]:
        escaped.append(handle)
        raise RuntimeError("inspection failed")

    try:
        with pytest.raises(RuntimeError, match="inspection failed"):
            token.inspect_revalidated(fail)
        with pytest.raises(ValueError, match="borrowed inspection handle is closed"):
            escaped[0].read()
        assert isinstance(token.consume_for_restore(), Ok)
    finally:
        assert isinstance(token.close(), Ok)


def test_borrowed_inspection_rejects_substituted_source(tmp_path: Path) -> None:
    source = tmp_path / "backup.omrbak"
    source.write_bytes(b"validated backup")
    opened = ValidatedBackup.open(str(source))
    assert isinstance(opened, Ok)
    token = opened.value
    replacement = tmp_path / "replacement.omrbak"
    replacement.write_bytes(b"substituted token")
    try:
        os.replace(replacement, source)
    except PermissionError:
        # Windows validation handles deliberately deny delete-sharing, so substitution
        # is prevented by the OS before token revalidation is needed.
        assert os.name == "nt"
        assert isinstance(token.revalidate(), Ok)
    else:
        result = token.inspect_revalidated(lambda handle: Ok(handle.read()))
        assert isinstance(result, Err)
        assert result.errors[0].code == "BACKUP_SOURCE_CHANGED"
    finally:
        assert isinstance(token.close(), Ok)


def test_restore_publication_refuses_a_raced_target(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    raced = tmp_path / "session-1"
    staged.mkdir()
    (staged / "proven.txt").write_text("proven", encoding="utf-8")
    raced.mkdir()
    (raced / "attacker.txt").write_text("attacker", encoding="utf-8")

    if os.name == "nt":
        with pytest.raises(OSError, match="verified-tree"):
            _publish_directory_no_replace(staged, raced)
    else:
        with pytest.raises(FileExistsError):
            _publish_directory_no_replace(staged, raced)

    assert staged.exists()
    assert (raced / "attacker.txt").read_text(encoding="utf-8") == "attacker"
@pytest.mark.skipif(os.name != "nt", reason="Windows verified-tree identity coverage")
def test_windows_verified_tree_pins_identities_and_renames_by_handle(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    payload = prepared / "payload.bin"
    payload.write_bytes(b"verified")
    provenance = RestoreProvenance(
        1,
        "session-1",
        "a" * 64,
        1,
        "generation-1",
        "b" * 64,
        None,
        "2026-01-01T00:00:00.000000Z",
        LineageState.VALID_TRUNCATED_ANCESTOR,
    )
    (prepared / "LOCATION.json").write_text("{}", encoding="utf-8")
    (prepared / "RESTORE_PROVENANCE.json").write_text(
        json.dumps(provenance.to_dict()), encoding="utf-8"
    )
    expected = {"payload.bin": (len(b"verified"), hashlib.sha256(b"verified").hexdigest())}

    tree = _WindowsVerifiedRestoreTree(
        prepared,
        expected,
        set(),
        provenance,
        {},
        _path_identity(prepared),
        _path_identity(tmp_path),
    )
    try:
        replacement = tmp_path / "replacement.bin"
        replacement.write_bytes(b"attacker")
        with pytest.raises(PermissionError):
            os.replace(replacement, payload)
        target = tmp_path / "published"
        tree.publish(target)
        assert not prepared.exists()
        assert target.joinpath("payload.bin").read_bytes() == b"verified"
    finally:
        tree.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows verified-tree publication coverage")
def test_windows_publication_refuses_pathname_fallback(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    with pytest.raises(OSError, match="verified-tree"):
        _publish_directory_no_replace(staged, tmp_path / "target")
    assert staged.exists()


def test_restore_cleanup_failure_is_a_typed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "retained-staging"
    staging.mkdir()

    def fail_cleanup(_path: str) -> None:
        raise OSError("cleanup blocked")

    monkeypatch.setattr("omr_grader.infrastructure.backup_archive.shutil.rmtree", fail_cleanup)
    metadata = os.stat(staging)
    parent = os.stat(staging.parent)
    result = BackupArchive().discard(
        str(staging),
        StagingOwnership(metadata.st_dev, metadata.st_ino, parent.st_dev, parent.st_ino),
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "BACKUP_RESTORE_CLEANUP_REQUIRED"
    assert result.errors[0].field_path == str(staging)

def test_discard_refuses_a_replaced_prepared_tree(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    original = os.stat(staging)
    parent = os.stat(tmp_path)
    ownership = StagingOwnership(
        original.st_dev, original.st_ino, parent.st_dev, parent.st_ino
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    try:
        os.replace(replacement, staging)
    except (FileExistsError, PermissionError):
        pytest.skip("host cannot replace an existing ownership target")
    result = BackupArchive().discard(str(staging), ownership)

    assert isinstance(result, Err)
    assert result.errors[0].code == "BACKUP_RESTORE_OWNERSHIP_LOST"


def _cleanup_failure() -> Err:
    return Err((
        ErrorInfo(
            "BACKUP_RESTORE_CLEANUP_REQUIRED",
            "error.backup_restore_cleanup_required",
        ),
    ))


def test_extract_preserves_source_change_when_discard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.omrbak"
    with zipfile.ZipFile(source, "w"):
        pass
    archive = BackupArchive()
    monkeypatch.setattr(
        archive,
        "_inspect",
        lambda _handle: Ok(SimpleNamespace(archive_manifest=SimpleNamespace(entries=()))),
    )
    digests = iter(("before", "after"))
    monkeypatch.setattr(
        "omr_grader.infrastructure.backup_archive._sha_handle", lambda _handle: next(digests)
    )
    monkeypatch.setattr(archive, "discard", lambda _staging, _ownership: _cleanup_failure())

    with source.open("rb") as handle:
        result = archive.extract(handle, str(tmp_path / "target"))

    assert isinstance(result, Err)
    assert tuple(error.code for error in result.errors) == (
        "BACKUP_SOURCE_CHANGED",
        "BACKUP_RESTORE_CLEANUP_REQUIRED",
    )


def test_extract_preserves_extraction_fault_when_discard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.omrbak"
    with zipfile.ZipFile(source, "w"):
        pass
    archive = BackupArchive()
    monkeypatch.setattr(
        archive,
        "_inspect",
        lambda _handle: Ok(
            SimpleNamespace(
                archive_manifest=SimpleNamespace(entries=(SimpleNamespace(path="missing"),))
            )
        ),
    )
    monkeypatch.setattr(archive, "discard", lambda _staging, _ownership: _cleanup_failure())

    with source.open("rb") as handle:
        result = archive.extract(handle, str(tmp_path / "target"))

    assert isinstance(result, Err)
    assert tuple(error.code for error in result.errors) == (
        "BACKUP_RESTORE_FAILED",
        "BACKUP_RESTORE_CLEANUP_REQUIRED",
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor binding coverage")
def test_posix_staging_rejects_replaced_target_name(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    archive = BackupArchive()
    try:
        with archive._owned_staging(target) as (
            staging,
            ownership,
            target_fd,
            staging_fd,
        ):
            moved = tmp_path / "moved-target"
            os.replace(target, moved)
            target.mkdir()
            with pytest.raises(ValueError, match="parent ownership changed"):
                archive._verify_owned_fds(target, staging, ownership, target_fd, staging_fd)
    except _RestorePlatformUnavailable:
        pytest.skip("host lacks descriptor-relative directory operations")
    finally:
        shutil.rmtree(tmp_path / "moved-target", ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows MoveFileW coverage")
def test_windows_quarantine_move_succeeds_and_refuses_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    quarantine = tmp_path / "quarantine"
    _move_directory_no_replace(source, quarantine)

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    with pytest.raises(OSError):
        _move_directory_no_replace(replacement, quarantine)

    assert replacement.exists()
    assert quarantine.exists()
@pytest.mark.skipif(os.name != "nt", reason="Windows handle-pinning coverage")
def test_windows_restore_adapter_rejects_reparse_targets_and_pins_staging(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    reparse = tmp_path / "reparse"
    try:
        reparse.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("test host cannot create a directory reparse point")
    with pytest.raises(ValueError, match="reparse point"):
        _WindowsRestoreAdapter.create(reparse)

    with _WindowsRestoreAdapter.create(target) as restore:
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        with pytest.raises(OSError):
            os.replace(replacement, restore.staging)
    shutil.rmtree(restore.staging)
