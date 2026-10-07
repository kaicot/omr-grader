from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import replace
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
    IdentityRecord,
    ManifestFile,
    ManifestSummary,
    SessionManifest,
    SessionRecord,
)
from omr_grader.infrastructure import session_store as session_store_module
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore

STAMP = "2026-07-28T12:00:00.000000Z"
SHA = "a" * 64


def _identity(session_id: str = "session-1") -> IdentityRecord:
    return IdentityRecord(1, session_id, STAMP, CreationKind.SCAN)


def _record(session_id: str = "session-1", revision: int = 1) -> SessionRecord:
    return SessionRecord(
        1,
        session_id,
        revision,
        SessionState.CREATED,
        "Math",
        2026,
        ExamTerm.FIRST,
        STAMP,
        None,
        STAMP,
    )


def _manifest(
    session_id: str = "session-1", generation_id: str = "generation-1"
) -> SessionManifest:
    return SessionManifest(
        1,
        session_id,
        1,
        generation_id,
        None,
        None,
        None,
        "create-1",
        OperationKind.CREATE,
        "test",
        STAMP,
        SessionState.CREATED,
        (),
        None,
        SHA,
        SHA,
        None,
        None,
        (),
        ManifestSummary(0, 0, 0, None),
    )


def _create(store: SessionStore, session_id: str = "session-1") -> None:
    result = store.create_initial_generation(
        identity=_identity(session_id),
        manifest=_manifest(session_id),
        session=_record(session_id),
        display_name=f"exam-{session_id}",
    )
    assert isinstance(result, Ok)


def _mutation(session_id: str = "session-1", expected_revision: int = 1) -> GenerationMutation:
    updated = _record(session_id, expected_revision + 1)
    return GenerationMutation(
        session_id,
        f"metadata-{expected_revision + 1}",
        OperationKind.METADATA_EDIT,
        expected_revision,
        SessionState.CREATED,
        MetadataSemanticView(updated),
        None,
    )


def _code(result: object) -> str:
    assert isinstance(result, Err)
    return result.errors[0].code


def test_generation_one_create_publishes_only_a_committed_snapshot(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)

    pointer = json.loads((tmp_path / "exam-session-1" / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["revision"] == 1
    assert pointer["generation_id"] == "generation-1"
    location = json.loads(
        (tmp_path / "exam-session-1" / "LOCATION.json").read_text(encoding="utf-8")
    )
    assert location == {
        "schema_version": 1,
        "session_id": "session-1",
        "display_name": "exam-session-1",
        "operation_id": "create-1",
    }
    assert not list(tmp_path.glob("*.staging"))
    snapshot = store.open_committed_snapshot(
        SnapshotRequest("session-1", 1, SnapshotPurpose.DETAIL)
    )
    assert isinstance(snapshot, Ok)
    assert snapshot.value.snapshot_ref.revision == 1
    assert snapshot.value.manifest.session_id == "session-1"
    assert isinstance(snapshot.value.close(), Ok)


def test_heavy_artifacts_live_only_in_session_root_and_remain_allowlisted(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    artifacts = {
        "images/page.png": b"normalized",
        "sources/scans/001_source.pdf": b"source-pdf",
        "02채점결과이미지/page.jpg": b"review-jpeg",
    }
    files = tuple(
        ManifestFile(path, len(payload), hashlib.sha256(payload).hexdigest(), "application/octet-stream")
        for path, payload in sorted(artifacts.items(), key=lambda item: item[0].encode("utf-8"))
    )
    manifest = replace(_manifest(), files=files)
    created = store._create_initial_generation(
        identity=_identity(),
        manifest=manifest,
        session=_record(),
        display_name="exam-session-1",
        artifacts=artifacts,
    )
    assert isinstance(created, Ok)
    session = tmp_path / "exam-session-1"
    generation = session / "generations" / "g00000001_generation-1"

    assert not (generation / "images").exists()
    assert not (generation / "sources").exists()
    assert not (generation / "02채점결과이미지").exists()
    assert (session / "01원본스캔" / "page.png").read_bytes() == b"normalized"
    assert (session / "01원본스캔" / "001_source.pdf").read_bytes() == b"source-pdf"
    assert (session / "02채점결과이미지" / "page.jpg").read_bytes() == b"review-jpeg"

    opened = store.open_committed_snapshot(
        SnapshotRequest("session-1", 1, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok)
    image = opened.value.open_allowlisted("images/page.png")
    assert isinstance(image, Ok)
    with image.value:
        assert image.value.read() == b"normalized"
    assert isinstance(opened.value.close(), Ok)

    committed = store.commit_generation(_mutation())
    assert isinstance(committed, Ok)
    for generation_path in (session / "generations").iterdir():
        assert not (generation_path / "images").exists()
        assert not (generation_path / "sources").exists()
        assert not (generation_path / "02채점결과이미지").exists()
    assert (session / "01원본스캔" / "page.png").read_bytes() == b"normalized"
    assert (session / "01원본스캔" / "001_source.pdf").read_bytes() == b"source-pdf"
    assert (session / "02채점결과이미지" / "page.jpg").read_bytes() == b"review-jpeg"


def test_commit_replaces_current_pointer_and_rejects_stale_cas(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)

    committed = store.commit_generation(_mutation())
    assert isinstance(committed, Ok)
    pointer = json.loads((tmp_path / "exam-session-1" / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["revision"] == 2
    assert pointer["generation_id"] == committed.value.generation_id
    assert (tmp_path / "exam-session-1" / pointer["generation_relpath"] / "manifest.json").is_file()
    generations = tuple(
        path.name
        for path in (tmp_path / "exam-session-1" / "generations").iterdir()
        if path.is_dir()
    )
    assert generations == (Path(pointer["generation_relpath"]).name,)
    assert (
        _code(store.commit_generation(_mutation(expected_revision=1)))
        == "SESSION_REVISION_CONFLICT"
    )


def test_lease_allows_only_manifest_files_and_cannot_be_reused_after_close(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    result = store.open_committed_snapshot(SnapshotRequest("session-1", 1, SnapshotPurpose.DETAIL))
    assert isinstance(result, Ok)
    lease = result.value

    assert _code(lease.open_allowlisted("CURRENT.json")) == "SNAPSHOT_PATH_FORBIDDEN"
    assert isinstance(lease.close(), Ok)
    assert _code(lease.open_allowlisted("session.json")) == "SESSION_LEASE_CLOSED"


def test_manifest_allowlist_binds_opened_file_bytes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    session = tmp_path / "exam-session-1"
    pointer = json.loads((session / "CURRENT.json").read_text(encoding="utf-8"))
    generation = session / pointer["generation_relpath"]
    manifest_path = generation / "manifest.json"
    payload = manifest_path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == pointer["manifest_sha256"]
    result = store.open_committed_snapshot(SnapshotRequest("session-1", 1, SnapshotPurpose.DETAIL))
    assert isinstance(result, Ok)
    assert _code(result.value.open_allowlisted("manifest.json")) == "SNAPSHOT_PATH_FORBIDDEN"
    result.value.close()


def test_semantic_mismatch_is_rejected_before_staging(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    mutation = GenerationMutation(
        "session-1",
        "metadata-wrong-session",
        OperationKind.METADATA_EDIT,
        1,
        SessionState.CREATED,
        MetadataSemanticView(_record("other-session", 2)),
        None,
    )

    assert _code(store.commit_generation(mutation)) == "SESSION_SEMANTIC_MISMATCH"
    assert not (tmp_path / "exam-session-1" / ".staging").exists()


def _hidden(path: Path) -> bool:
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_HIDDEN)


def test_saved_sessions_hide_internal_folders_and_leave_no_staging(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    session = tmp_path / "exam-session-1"
    windows = os.name == "nt"

    assert _hidden(session / "generations") is windows

    assert isinstance(store.commit_generation(_mutation()), Ok)
    assert isinstance(store.commit_generation(_mutation(expected_revision=2)), Ok)

    assert not (session / ".staging").exists()
    assert _hidden(session / "generations") is windows
    assert not any(_hidden(path) for path in session.rglob("*") if path.name != "generations")
    opened = store.open_committed_snapshot(
        SnapshotRequest("session-1", 3, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok)
    opened.value.close()


def test_generation_prune_failure_preserves_committed_current_and_retries_safely(
    tmp_path: Path,
) -> None:
    failed = False

    def fail_first_prune(name: str) -> None:
        nonlocal failed
        if name == "before_generation_prune" and not failed:
            failed = True
            raise OSError("injected generation prune failure")

    store = SessionStore(tmp_path, fault_barrier=fail_first_prune)
    _create(store)

    first = store.commit_generation(_mutation())

    assert isinstance(first, Ok)
    assert any(warning.code == "GENERATION_PRUNE_RETRY_REQUIRED" for warning in first.warnings)
    session = tmp_path / "exam-session-1"
    pointer = json.loads((session / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["revision"] == 2
    opened = store.open_committed_snapshot(
        SnapshotRequest("session-1", 2, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok)
    opened.value.close()
    assert len(tuple((session / "generations").iterdir())) == 2

    retried = SessionStore(tmp_path).commit_generation(_mutation(expected_revision=2))

    assert isinstance(retried, Ok)
    pointer = json.loads((session / "CURRENT.json").read_text(encoding="utf-8"))
    generations = tuple((session / "generations").iterdir())
    assert pointer["revision"] == 3
    assert len(generations) == 1
    assert generations[0].name == Path(pointer["generation_relpath"]).name
    retention = json.loads((session / "RETENTION.json").read_text(encoding="utf-8"))
    assert retention["boundary_generation_id"] == pointer["generation_id"]
    assert retention["omitted_parent"]["generation_id"] != pointer["generation_id"]


def test_missing_retention_boundary_cannot_bypass_lineage_validation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    committed = store.commit_generation(_mutation())
    assert isinstance(committed, Ok)
    session = tmp_path / "exam-session-1"
    pointer_before = (session / "CURRENT.json").read_bytes()
    (session / "RETENTION.json").unlink()

    opened = store.open_committed_snapshot(SnapshotRequest("session-1", 2, SnapshotPurpose.DETAIL))

    assert isinstance(opened, Err)
    assert opened.errors[0].code == "SESSION_COMMITTED_GENERATION_INVALID"
    assert (session / "CURRENT.json").read_bytes() == pointer_before


def test_result_view_failure_keeps_published_revision_consistent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    session = tmp_path / "exam-session-1"

    def fail_result_view(_session: Path, _generation: Path) -> None:
        raise PermissionError(errno.EACCES, "OneDrive locked result view")

    monkeypatch.setattr(session_store_module, "_refresh_result_view", fail_result_view)

    result = store.commit_generation(_mutation())

    assert isinstance(result, Ok)
    assert any(warning.code == "POSTCOMMIT_RECOVERY_REQUIRED" for warning in result.warnings)
    pointer = json.loads((session / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["revision"] == 2
    opened = store.open_committed_snapshot(
        SnapshotRequest("session-1", 2, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok)
    opened.value.close()


def _create_prune_fixture(root: Path) -> tuple[SessionStore, Path]:
    store = SessionStore(root)
    artifacts = {
        "payload.bin": b"immutable synthetic history",
        "images/page.png": b"synthetic normalized image",
        "sources/scans/page.pdf": b"synthetic original PDF",
    }
    manifest = replace(
        _manifest(),
        files=tuple(
            ManifestFile(
                path, len(payload), hashlib.sha256(payload).hexdigest(), "application/octet-stream"
            )
            for path, payload in sorted(artifacts.items())
        ),
    )
    created = store._create_initial_generation(
        identity=_identity(),
        manifest=manifest,
        session=_record(),
        display_name="exam-session-1",
        artifacts=artifacts,
    )
    assert isinstance(created, Ok)
    first = store.commit_generation(_mutation())
    assert isinstance(first, Ok) and not first.warnings
    session = root / "exam-session-1"
    assert len(tuple((session / "generations").iterdir())) == 1
    return store, session


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_prune_backup_roundtrip(store: SessionStore, revision: int, root: Path) -> None:
    target = SessionStore(root / "restored")
    service = BackupApplicationService(
        SessionCommitCoordinator(store), restore_publisher=target.restore_publisher()
    )
    archive = root / "prune.omrbak"
    exported = service.export_backup(
        BackupExportRequest(
            SnapshotRequest("session-1", revision, SnapshotPurpose.BACKUP),
            str(archive),
            CollisionPolicy.ERROR,
            "prune-backup",
        )
    )
    assert isinstance(exported, Ok), exported
    validated = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(validated, Ok), validated
    restored = service.restore_backup(
        RestoreCommand(validated.value, str(target.root), "prune-restore")
    )
    assert isinstance(restored, Ok), restored
    opened = target.open_committed_snapshot(
        SnapshotRequest("session-1", revision, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok), opened
    try:
        for path, payload in (
            ("payload.bin", b"immutable synthetic history"),
            ("images/page.png", b"synthetic normalized image"),
            ("sources/scans/page.pdf", b"synthetic original PDF"),
        ):
            stream = opened.value.open_allowlisted(path)
            assert isinstance(stream, Ok), stream
            with stream.value:
                assert stream.value.read() == payload
    finally:
        assert isinstance(opened.value.close(), Ok)


@pytest.mark.parametrize("successful_moves", (0, 1))
def test_prune_move_failure_keeps_current_readable_backupable_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, successful_moves: int
) -> None:
    store, session = _create_prune_fixture(tmp_path / "source")
    revision = 2
    if successful_moves:
        # Keep two obsolete generations so the injected failure really follows
        # a successful prune move, with the older revision-one boundary absent.
        def defer_prune(stage: str) -> None:
            if stage == "before_generation_prune":
                raise OSError("defer synthetic cleanup")

        deferred = SessionStore(store.root, fault_barrier=defer_prune).commit_generation(
            _mutation(expected_revision=2)
        )
        assert isinstance(deferred, Ok) and deferred.warnings
        revision = 3

    moved: list[Path] = []
    retry_replace = session_store_module.retry_replace

    def fail_prune_move(source: Path, destination: Path) -> None:
        if source.parent == session / "generations":
            if len(moved) == successful_moves:
                raise OSError("injected owned-fixture prune move error")
            retry_replace(source, destination)
            moved.append(source)
        else:
            retry_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(session_store_module, "retry_replace", fail_prune_move)
        committed = store.commit_generation(_mutation(expected_revision=revision))
    assert isinstance(committed, Ok), committed
    assert committed.value.revision == revision + 1
    assert [warning.code for warning in committed.warnings] == ["GENERATION_PRUNE_RETRY_REQUIRED"]
    assert len(moved) == successful_moves
    revision += 1
    pointer_bytes = (session / "CURRENT.json").read_bytes()
    pointer = json.loads(pointer_bytes)
    current = session / pointer["generation_relpath"]
    canonical = _tree_bytes(current)
    raw = _tree_bytes(session / "01원본스캔")

    reopened_store = SessionStore(store.root)
    before_read = _tree_bytes(store.root)
    opened = reopened_store.open_committed_snapshot(
        SnapshotRequest("session-1", revision, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok), opened
    assert opened.value.snapshot_ref.generation_id == committed.value.generation_id
    assert isinstance(opened.value.close(), Ok)
    assert _tree_bytes(store.root) == before_read
    _assert_prune_backup_roundtrip(reopened_store, revision, tmp_path)
    assert (session / "CURRENT.json").read_bytes() == pointer_bytes
    assert _tree_bytes(current) == canonical
    assert _tree_bytes(session / "01원본스캔") == raw

    retried = reopened_store.commit_generation(_mutation(expected_revision=revision))
    assert isinstance(retried, Ok) and not retried.warnings, retried
    generations = tuple((session / "generations").iterdir())
    assert len(generations) == 1
    assert not (session / ".staging" / "prune").exists()
    assert _tree_bytes(session / "01원본스캔") == raw
    assert not any(path.suffix in {".png", ".pdf"} for path in generations[0].rglob("*"))
    assert {path.name for path in (store.root / ".locks" / "lifetime" / "session-1").iterdir()} == {
        f"{retried.value.generation_id}.gate"
    }
    final = SessionStore(store.root).open_committed_snapshot(
        SnapshotRequest("session-1", revision + 1, SnapshotPurpose.DETAIL)
    )
    assert isinstance(final, Ok), final
    assert isinstance(final.value.close(), Ok)


@pytest.mark.parametrize(
    "tamper",
    (
        "missing-retention",
        "bad-boundary-hash",
        "bad-parent-hash",
        "bad-retention-session",
        "boolean-schema",
        "bad-boundary-generation",
        "bad-boundary-revision",
        "bad-parent-generation",
        "extra-parent-field",
        "missing-retained-manifest",
        "bad-retained-manifest",
        "bad-retained-session",
        "bad-retained-generation",
        "renamed-retained-generation",
    ),
)
def test_interrupted_prune_does_not_bypass_retention_or_retained_manifest_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    store, session = _create_prune_fixture(tmp_path)
    retry_replace = session_store_module.retry_replace

    def fail_prune_move(source: Path, destination: Path) -> None:
        if source.parent == session / "generations":
            raise OSError("injected owned-fixture prune move error")
        retry_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(session_store_module, "retry_replace", fail_prune_move)
        committed = store.commit_generation(_mutation(expected_revision=2))
    assert isinstance(committed, Ok) and committed.warnings
    pointer_bytes = (session / "CURRENT.json").read_bytes()
    retention_path = session / "RETENTION.json"
    retention = json.loads(retention_path.read_bytes())
    if tamper == "missing-retention":
        retention_path.unlink()
    elif tamper == "missing-retained-manifest":
        parent = next((session / "generations").glob("g00000002_*")) / "manifest.json"
        parent.unlink()
    elif tamper.startswith("bad-retained-") or tamper == "renamed-retained-generation":
        parent = next((session / "generations").glob("g00000002_*")) / "manifest.json"
        value = json.loads(parent.read_bytes())
        field = {
            "bad-retained-manifest": "app_version",
            "bad-retained-session": "session_id",
            "bad-retained-generation": "generation_id",
            "renamed-retained-generation": "generation_id",
        }[tamper]
        value[field] = "tampered"
        parent.write_text(json.dumps(value), encoding="utf-8")
        if tamper == "renamed-retained-generation":
            parent.parent.rename(parent.parent.with_name("g00000002_tampered"))
    else:
        if tamper == "bad-boundary-hash":
            retention["boundary_manifest_sha256"] = "0" * 64
        elif tamper == "bad-parent-hash":
            retention["omitted_parent"]["manifest_sha256"] = "0" * 64
        elif tamper == "boolean-schema":
            retention["schema_version"] = True
        elif tamper == "bad-boundary-generation":
            retention["boundary_generation_id"] = "other-generation"
        elif tamper == "bad-boundary-revision":
            retention["boundary_revision"] = 99
        elif tamper == "bad-parent-generation":
            retention["omitted_parent"]["generation_id"] = "other-generation"
        elif tamper == "extra-parent-field":
            retention["omitted_parent"]["unexpected"] = True
        else:
            retention["session_id"] = "other-session"
        retention_path.write_text(json.dumps(retention), encoding="utf-8")
    before_read = _tree_bytes(store.root)
    opened = store.open_committed_snapshot(
        SnapshotRequest("session-1", 3, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Err), opened
    assert opened.errors[0].code == "SESSION_COMMITTED_GENERATION_INVALID"
    assert _tree_bytes(store.root) == before_read
    assert (session / "CURRENT.json").read_bytes() == pointer_bytes
