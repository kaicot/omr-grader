from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import (
    BackupExportRequest,
    BackupValidateRequest,
    CollisionPolicy,
    GenerationMutation,
    MetadataSemanticView,
    RestoreCommand,
    SnapshotRequest,
)
from omr_grader.domain.enums import (
    CreationKind,
    ExamTerm,
    OperationKind,
    SessionState,
    SnapshotPurpose,
)
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import (
    CurrentPointer,
    IdentityRecord,
    ManifestFile,
    ManifestSummary,
    RestoreProvenance,
    SessionManifest,
    SessionRecord,
)
from omr_grader.infrastructure.atomic_io import atomic_write_json
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore

STAMP = "2026-07-28T12:00:00.000000Z"
SHA = "a" * 64


def _write(path: Path, value: object) -> None:
    assert isinstance(atomic_write_json(path, value), Ok)


def _source_store(root: Path) -> SessionStore:
    store = SessionStore(root)
    store._mkdirs()
    session_id = "session-1"
    generation_id = "generation-1"
    display = root / "source-exam"
    generation = display / "generations" / f"g00000001_{generation_id}"
    generation.mkdir(parents=True)
    payload = b"nonempty backup payload"
    (generation / "payload.bin").write_bytes(payload)
    manifest = SessionManifest(
        schema_version=1,
        session_id=session_id,
        revision=1,
        generation_id=generation_id,
        parent_revision=None,
        parent_generation_id=None,
        parent_manifest_sha256=None,
        operation_id="create-1",
        operation_kind=OperationKind.CREATE,
        app_version="test",
        created_at=STAMP,
        state=SessionState.CREATED,
        base_response_ids=(),
        profile_sha256=None,
        roster_sha256=SHA,
        key_sha256=SHA,
        threshold_version=None,
        threshold_sha256=None,
        files=(
            ManifestFile(
                "payload.bin",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                "application/octet-stream",
            ),
        ),
        summary=ManifestSummary(0, 0, 0, None),
    )
    _write(
        display / "IDENTITY.json", IdentityRecord(1, session_id, STAMP, CreationKind.SCAN).to_dict()
    )
    _write(
        generation / "session.json",
        SessionRecord(
            1, session_id, 1, SessionState.CREATED, "Math", 2026, ExamTerm.FIRST, STAMP, None, STAMP
        ).to_dict(),
    )
    _write(generation / "manifest.json", manifest.to_dict())
    digest = hashlib.sha256((generation / "manifest.json").read_bytes()).hexdigest()
    _write(
        display / "CURRENT.json",
        CurrentPointer(
            1, session_id, 1, generation_id, f"generations/g00000001_{generation_id}", digest, STAMP
        ).to_dict(),
    )
    _write(
        display / "LOCATION.json", store._location_metadata(session_id, "source-exam", "create-1")
    )
    gate = store._gate_path(session_id, generation_id)
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.touch()
    return store


def _service(source: SessionStore, destination: SessionStore) -> BackupApplicationService:
    return BackupApplicationService(
        SessionCommitCoordinator(source), restore_publisher=destination.restore_publisher()
    )


def test_nonempty_current_only_archive_restores_with_provenance_and_collision(
    tmp_path: Path,
) -> None:
    source = _source_store(tmp_path / "source")
    destination = SessionStore(tmp_path / "restore")
    service = _service(source, destination)
    archive = tmp_path / "session.omrbak"
    exported = service.export_backup(
        BackupExportRequest(
            SnapshotRequest("session-1", None, SnapshotPurpose.BACKUP),
            str(archive),
            CollisionPolicy.ERROR,
            "backup-1",
        )
    )
    assert isinstance(exported, Ok)
    validated = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(validated, Ok)
    restored = service.restore_backup(
        RestoreCommand(validated.value, str(destination.root), "restore-1")
    )
    assert isinstance(restored, Ok)
    session = destination.root / "session-1"
    assert (
        session / "generations" / "g00000001_generation-1" / "payload.bin"
    ).read_bytes() == b"nonempty backup payload"
    assert destination._validate_current(session)[0].generation_id == "generation-1"
    provenance = RestoreProvenance.from_dict(
        json.loads((session / "RESTORE_PROVENANCE.json").read_text(encoding="utf-8"))
    )
    assert provenance.archive_sha256 == exported.value.destination_sha256
    second = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(second, Ok)
    assert isinstance(
        service.restore_backup(RestoreCommand(second.value, str(destination.root), "restore-2")),
        Err,
    )
    assert session.exists()


def test_revision_two_backup_excludes_control_files_from_payload_allowlist(tmp_path: Path) -> None:
    source = _source_store(tmp_path / "source")
    session = source.root / "source-exam"
    parent = SessionRecord.from_dict(
        json.loads(
            (session / "generations" / "g00000001_generation-1" / "session.json").read_text(
                encoding="utf-8"
            )
        )
    )
    record = SessionRecord(
        1,
        parent.session_id,
        2,
        parent.state,
        parent.exam_name,
        parent.exam_year,
        parent.exam_term,
        parent.created_at,
        parent.graded_at,
        "2026-07-28T12:01:00.000000Z",
    )
    committed = source.commit_generation(
        GenerationMutation(
            "session-1",
            "metadata-2",
            OperationKind.METADATA_EDIT,
            1,
            SessionState.CREATED,
            MetadataSemanticView(record),
            None,
        )
    )
    assert isinstance(committed, Ok)
    archive = tmp_path / "revision-two.omrbak"
    exported = _service(source, SessionStore(tmp_path / "unused")).export_backup(
        BackupExportRequest(
            SnapshotRequest("session-1", 2, SnapshotPurpose.BACKUP),
            str(archive),
            CollisionPolicy.ERROR,
            "backup-2",
        )
    )
    assert isinstance(exported, Ok)
    with zipfile.ZipFile(archive) as contents:
        names = contents.namelist()
    prefix = f"omrbak-v1/generations/g00000002_{committed.value.generation_id}/"
    assert names.count(f"{prefix}session.json") == 1
    assert names.count(f"{prefix}manifest.json") == 1
    assert f"{prefix}session.json" not in {
        f"{prefix}{entry.path}" for entry in source._validate_current(session)[1].files
    }
    assert f"{prefix}manifest.json" not in {
        f"{prefix}{entry.path}" for entry in source._validate_current(session)[1].files
    }
    lease = source.open_committed_snapshot(SnapshotRequest("session-1", 2, SnapshotPurpose.BACKUP))
    assert isinstance(lease, Ok)
    assert isinstance(lease.value.open_allowlisted("session.json"), Err)
    assert isinstance(lease.value.open_allowlisted("manifest.json"), Err)
    assert isinstance(lease.value.close(), Ok)


def test_tampering_or_precommit_fault_never_publishes_a_partial_session(tmp_path: Path) -> None:
    source = _source_store(tmp_path / "source")
    archive = tmp_path / "session.omrbak"
    assert isinstance(
        _service(source, SessionStore(tmp_path / "unused")).export_backup(
            BackupExportRequest(
                SnapshotRequest("session-1", None, SnapshotPurpose.BACKUP),
                str(archive),
                CollisionPolicy.ERROR,
                "backup-1",
            )
        ),
        Ok,
    )
    with zipfile.ZipFile(archive, "a") as tampered:
        tampered.writestr("extra", b"x")
    assert isinstance(
        _service(source, SessionStore(tmp_path / "tampered")).validate_backup(
            BackupValidateRequest(str(archive))
        ),
        Err,
    )
    destination = SessionStore(tmp_path / "fault")
    clean = tmp_path / "clean.omrbak"
    assert isinstance(
        _service(source, destination).export_backup(
            BackupExportRequest(
                SnapshotRequest("session-1", None, SnapshotPurpose.BACKUP),
                str(clean),
                CollisionPolicy.ERROR,
                "backup-2",
            )
        ),
        Ok,
    )
    destination._barrier = (
        lambda stage: (_ for _ in ()).throw(OSError("fault"))
        if stage == "after_restore_gate_create"
        else None
    )
    service = _service(source, destination)
    token = service.validate_backup(BackupValidateRequest(str(clean)))
    assert isinstance(token, Ok)
    assert isinstance(
        service.restore_backup(RestoreCommand(token.value, str(destination.root), "fault-restore")),
        Err,
    )
    assert not (destination.root / "session-1").exists()


def test_restore_rejects_mutation_after_final_verification(tmp_path: Path) -> None:
    source = _source_store(tmp_path / "source")
    destination = SessionStore(tmp_path / "restore")
    archive = tmp_path / "session.omrbak"
    assert isinstance(
        _service(source, destination).export_backup(
            BackupExportRequest(
                SnapshotRequest("session-1", None, SnapshotPurpose.BACKUP),
                str(archive),
                CollisionPolicy.ERROR,
                "backup-1",
            )
        ),
        Ok,
    )

    def mutate(stage: str) -> None:
        if stage == "after_restore_verification":
            prepared = next(destination.root.glob(".restore-*.staging"))
            (prepared / "generations" / "g00000001_generation-1" / "payload.bin").write_bytes(
                b"substituted"
            )

    destination._barrier = mutate
    service = _service(source, destination)
    token = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(token, Ok)
    restored = service.restore_backup(
        RestoreCommand(token.value, str(destination.root), "mutation-restore")
    )
    assert isinstance(restored, Err)
    assert not (destination.root / "session-1").exists()
@pytest.mark.skipif(os.name != "nt", reason="Windows verified-tree publication coverage")
def test_windows_restore_fails_closed_when_verified_publication_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_store(tmp_path / "source")
    destination = SessionStore(tmp_path / "restore")
    archive = tmp_path / "session.omrbak"
    assert isinstance(
        _service(source, destination).export_backup(
            BackupExportRequest(
                SnapshotRequest("session-1", None, SnapshotPurpose.BACKUP),
                str(archive),
                CollisionPolicy.ERROR,
                "backup-1",
            )
        ),
        Ok,
    )

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("SetFileInformationByHandle unavailable")

    monkeypatch.setattr(
        "omr_grader.infrastructure.session_store._WindowsVerifiedRestoreTree", unavailable
    )
    token = _service(source, destination).validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(token, Ok)
    restored = _service(source, destination).restore_backup(
        RestoreCommand(token.value, str(destination.root), "win32-fail-closed")
    )

    assert isinstance(restored, Err)
    assert not (destination.root / "session-1").exists()
