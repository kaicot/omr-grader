from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from omr_grader.application.dto import RecoveryRequest, SessionMutationRequest, SnapshotRequest
from omr_grader.domain.enums import (
    CreationKind,
    ExamTerm,
    OperationKind,
    SessionState,
    SnapshotPurpose,
)
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import (
    DeleteTombstone,
    IdentityRecord,
    ManifestSummary,
    SessionManifest,
    SessionRecord,
    SessionReservation,
)
from omr_grader.infrastructure.session_lease import GateBackend, GateHandle
from omr_grader.infrastructure.session_store import SessionStore

STAMP = "2026-07-28T12:00:00.000000Z"
SHA = "c" * 64


@dataclass
class _Handle:
    backend: MemoryGateBackend
    path: Path
    exclusive: bool
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.backend.held.remove(self)


class MemoryGateBackend(GateBackend):
    """Process-local lock oracle: readers coexist; writers never overlap readers/writers."""

    def __init__(self) -> None:
        self.held: list[_Handle] = []
        self.acquires: list[tuple[Path, bool]] = []

    def acquire(self, path: Path, *, exclusive: bool, blocking: bool) -> GateHandle | None:
        self.acquires.append((path, exclusive))
        conflicting = [
            item for item in self.held if item.path == path and (exclusive or item.exclusive)
        ]
        if conflicting:
            return None
        handle = _Handle(self, path, exclusive)
        self.held.append(handle)
        return handle


def _identity() -> IdentityRecord:
    return IdentityRecord(1, "race-session", STAMP, CreationKind.SCAN)


def _record() -> SessionRecord:
    return SessionRecord(
        1, "race-session", 1, SessionState.CREATED, "Math", 2026, ExamTerm.FIRST, STAMP, None, STAMP
    )


def _manifest() -> SessionManifest:
    return SessionManifest(
        1,
        "race-session",
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
        identity=_identity(), manifest=_manifest(), session=_record(), display_name="race-exam"
    )
    assert isinstance(result, Ok)


def _code(result: object) -> str:
    assert isinstance(result, Err)
    return result.errors[0].code


def test_reader_wins_against_destructive_move_without_directory_mutation(tmp_path: Path) -> None:
    backend = MemoryGateBackend()
    store = SessionStore(tmp_path, gate_backend=backend)
    _create(store)
    lease = store.open_committed_snapshot(
        SnapshotRequest("race-session", 1, SnapshotPurpose.DETAIL)
    )
    assert isinstance(lease, Ok)

    moved = store.soft_delete(SessionMutationRequest("race-session", 1, "trash-1"))
    assert _code(moved) == "SESSION_BUSY_READERS"
    assert (tmp_path / "race-exam").is_dir()
    assert not (tmp_path / "_휴지통" / "세션" / "race-session").exists()
    lease.value.close()


def test_generation_gate_is_created_before_session_visibility(tmp_path: Path) -> None:
    observed: list[bool] = []

    def observe(point: str) -> None:
        if point == "after_session_rename":
            observed.append(
                (tmp_path / ".locks" / "lifetime" / "race-session" / "generation-1.gate").exists()
            )

    store = SessionStore(tmp_path, fault_barrier=observe)
    _create(store)
    assert observed == [True]


def test_missing_or_colliding_generation_gate_fails_closed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    gate = tmp_path / ".locks" / "lifetime" / "race-session" / "generation-1.gate"
    gate.unlink()

    missing = store.open_committed_snapshot(
        SnapshotRequest("race-session", 1, SnapshotPurpose.DETAIL)
    )
    assert _code(missing) == "GENERATION_GATE_MISSING"

    colliding_root = tmp_path / "colliding"
    colliding_gate = colliding_root / ".locks" / "lifetime" / "race-session" / "generation-1.gate"
    colliding_gate.parent.mkdir(parents=True)
    colliding_gate.touch()
    collision = SessionStore(colliding_root)
    assert (
        _code(
            collision.create_initial_generation(
                identity=_identity(),
                manifest=_manifest(),
                session=_record(),
                display_name="other-name",
            )
        )
        == "SESSION_CREATE_FAILED"
    )
    assert not (colliding_root / "other-name").exists()


def test_identity_uniqueness_covers_active_trash_reservation_and_deleting(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    assert (
        _code(
            store.create_initial_generation(
                identity=_identity(),
                manifest=_manifest(),
                session=_record(),
                display_name="duplicate",
            )
        )
        == "SESSION_ID_CONFLICT"
    )

    assert isinstance(store.soft_delete(SessionMutationRequest("race-session", 1, "trash-1")), Ok)
    assert (
        _code(
            store.create_initial_generation(
                identity=_identity(),
                manifest=_manifest(),
                session=_record(),
                display_name="duplicate",
            )
        )
        == "SESSION_ID_CONFLICT"
    )

    trash = tmp_path / "_휴지통" / "세션" / "race-session"
    trash.rename(tmp_path / "_saved-trash")
    reservation = tmp_path / ".reservations" / "race-session.json"
    reservation.write_text(
        json.dumps(
            SessionReservation(
                1, "race-session", "reserve-1", CreationKind.SCAN, STAMP, "x"
            ).to_dict()
        ),
        encoding="utf-8",
    )
    assert (
        _code(
            store.create_initial_generation(
                identity=_identity(),
                manifest=_manifest(),
                session=_record(),
                display_name="duplicate",
            )
        )
        == "SESSION_ID_CONFLICT"
    )

    reservation.unlink()
    deleting = tmp_path / ".deleting" / "delete-1"
    deleting.mkdir()
    (deleting / "DELETE.json").write_text(
        json.dumps(
            DeleteTombstone(1, "race-session", "delete-1", STAMP, ("generation-1",)).to_dict()
        ),
        encoding="utf-8",
    )
    assert (
        _code(
            store.create_initial_generation(
                identity=_identity(),
                manifest=_manifest(),
                session=_record(),
                display_name="duplicate",
            )
        )
        == "SESSION_ID_CONFLICT"
    )


@pytest.mark.parametrize(
    "session_id",
    ("../outside", "..\\outside", "C:\\outside", "\\\\server\\share", "CON", "name:stream"),
)
def test_session_id_is_one_portable_component(session_id: str) -> None:
    with pytest.raises(ValueError):
        IdentityRecord(1, session_id, STAMP, CreationKind.SCAN)
    with pytest.raises(ValueError):
        SnapshotRequest(session_id, None, SnapshotPurpose.DETAIL)


def test_recovery_uses_revalidated_identities_for_gate_ownership(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    _create(store)
    (tmp_path / ".unrelated.staging").mkdir()
    orphan = tmp_path / ".locks" / "lifetime" / "orphan-session"
    orphan.mkdir()

    result = store.recover_sessions(RecoveryRequest(str(tmp_path), False, False, "recover-gates"))

    assert isinstance(result, Ok)
    issues = result.value.quarantined
    assert any(issue.code == "SESSION_STAGING_ORPHAN" for issue in issues)
    assert any(
        issue.code == "GENERATION_GATE_ORPHAN" and issue.session_id == "orphan-session"
        for issue in issues
    )
    assert not any(
        issue.code == "GENERATION_GATE_ORPHAN" and issue.session_id == "race-session"
        for issue in issues
    )
