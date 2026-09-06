from __future__ import annotations

import errno
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from omr_grader.application.dto import (
    GenerationMutation,
    MetadataSemanticView,
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
from omr_grader.infrastructure.session_store import SessionStore

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
