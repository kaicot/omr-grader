from __future__ import annotations

import json
from pathlib import Path

from omr_grader.application.dto import (
    GenerationMutation,
    MetadataSemanticView,
    RecoveryRequest,
    SessionMutationRequest,
)
from omr_grader.domain.enums import (
    CreationKind,
    ExamTerm,
    OperationKind,
    SessionState,
)
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import IdentityRecord, ManifestSummary, SessionManifest, SessionRecord
from omr_grader.infrastructure.session_store import SessionStore

STAMP = "2026-07-28T12:00:00.000000Z"
SHA = "b" * 64


def _record(revision: int = 1) -> SessionRecord:
    return SessionRecord(
        1,
        "fault-session",
        revision,
        SessionState.CREATED,
        "Math",
        2026,
        ExamTerm.FIRST,
        STAMP,
        None,
        STAMP,
    )


def _manifest() -> SessionManifest:
    return SessionManifest(
        1,
        "fault-session",
        1,
        "generation-1",
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


def _create(store: SessionStore) -> None:
    result = store.create_initial_generation(
        identity=IdentityRecord(1, "fault-session", STAMP, CreationKind.SCAN),
        manifest=_manifest(),
        session=_record(),
        display_name="fault-exam",
    )
    assert isinstance(result, Ok)


def _mutation() -> GenerationMutation:
    return GenerationMutation(
        "fault-session",
        "metadata-2",
        OperationKind.METADATA_EDIT,
        1,
        SessionState.CREATED,
        MetadataSemanticView(_record(2)),
        None,
    )


def _code(result: object) -> str:
    assert isinstance(result, Err)
    return result.errors[0].code


def test_create_fault_before_session_rename_never_makes_a_session_visible(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after_gate_create":
            raise OSError("injected before rename")

    store = SessionStore(tmp_path, fault_barrier=fail)
    result = store.create_initial_generation(
        identity=IdentityRecord(1, "fault-session", STAMP, CreationKind.SCAN),
        manifest=_manifest(),
        session=_record(),
        display_name="fault-exam",
    )
    assert _code(result) == "SESSION_CREATE_FAILED"
    assert not (tmp_path / "fault-exam" / "CURRENT.json").exists()
    assert list((tmp_path / ".reservations").glob("fault-session.json"))


def test_commit_fault_before_pointer_replace_keeps_prior_current_or_reports_error(
    tmp_path: Path,
) -> None:
    def fail(point: str) -> None:
        if point == "after_generation_rename":
            raise OSError("injected before pointer")

    clean = SessionStore(tmp_path)
    _create(clean)
    before = (tmp_path / "fault-exam" / "CURRENT.json").read_bytes()
    result = SessionStore(tmp_path, fault_barrier=fail).commit_generation(_mutation())

    assert _code(result) == "SESSION_COMMIT_FAILED"
    assert (tmp_path / "fault-exam" / "CURRENT.json").read_bytes() == before


def test_commit_fault_after_pointer_replace_is_not_reported_as_uncommitted(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after_pointer_replace":
            raise OSError("injected after commit point")

    _create(SessionStore(tmp_path))
    result = SessionStore(tmp_path, fault_barrier=fail).commit_generation(_mutation())

    assert isinstance(result, Ok), (
        "CURRENT.json is the commit point; postcommit fault must be success"
    )
    pointer = json.loads((tmp_path / "fault-exam" / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["revision"] == 2


def test_postcommit_delete_cleanup_failure_is_success_with_pending_warning(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "before_delete_cleanup":
            raise OSError("injected cleanup failure")

    clean = SessionStore(tmp_path)
    _create(clean)
    deleted = clean.soft_delete(SessionMutationRequest("fault-session", 1, "trash-1"))
    assert isinstance(deleted, Ok)
    result = SessionStore(tmp_path, fault_barrier=fail).permanently_delete(
        SessionMutationRequest("fault-session", 1, "delete-1")
    )

    assert isinstance(result, Ok)
    assert result.value.cleanup_state.value == "pending"
    assert [warning.code for warning in result.warnings] == ["DELETE_CLEANUP_PENDING"]


def test_recovery_is_conservative_and_cleanup_is_idempotent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    orphan = tmp_path / ".interrupted.staging"
    orphan.mkdir()
    request = RecoveryRequest(str(tmp_path), False, True, "recover-1")

    first = store.recover_sessions(request)
    second = store.recover_sessions(request)
    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert not orphan.exists()
    assert any(issue.code == "SESSION_STAGING_ORPHAN" for issue in first.value.cleaned)
    assert not second.value.cleaned


def test_recovery_keeps_a_revalidated_session_gate_owned_despite_unrelated_quarantine(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    (tmp_path / ".unrelated.staging").mkdir()

    result = store.recover_sessions(RecoveryRequest(str(tmp_path), False, False, "recover-2"))

    assert isinstance(result, Ok)
    assert any(issue.code == "SESSION_STAGING_ORPHAN" for issue in result.value.quarantined)
    assert not any(
        issue.code == "GENERATION_GATE_ORPHAN" and issue.session_id == "fault-session"
        for issue in result.value.quarantined
    )
