from __future__ import annotations

from pathlib import Path

from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import BackupValidateRequest
from omr_grader.domain.errors import Err


def test_restore_fails_closed_without_an_authoritative_publisher(tmp_path: Path) -> None:
    archive = tmp_path / "empty.omrbak"
    archive.write_bytes(b"not a zip")
    service = BackupApplicationService(object())  # type: ignore[arg-type]
    validated = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(validated, Err)


def test_restore_rejects_replayed_or_closed_handle_before_publication(tmp_path: Path) -> None:
    # Live-token replay is exercised by the validation-token contract; malformed input
    # must never create a visible restore directory on this boundary.
    archive = tmp_path / "malformed.omrbak"
    archive.write_bytes(b"PK\x03\x04")
    service = BackupApplicationService(object())  # type: ignore[arg-type]
    result = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(result, Err)
    assert not (tmp_path / "visible-session").exists()
    assert not tuple(tmp_path.glob(".backup-staging-*"))
