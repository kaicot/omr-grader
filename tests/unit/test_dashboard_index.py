from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import omr_grader.infrastructure.dashboard_index as dashboard_index
from omr_grader.application.dto import SnapshotRef
from omr_grader.domain.enums import ExamTerm, SessionState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.infrastructure.dashboard_index import (
    build_dashboard_index,
    load_dashboard_index,
    rebuild_dashboard_index,
    replace_dashboard_index,
)

TIME = datetime(2026, 7, 28, 1, 2, 3, 456789, tzinfo=UTC)


@dataclass(frozen=True)
class Manifest:
    value: dict[str, str]

    def to_dict(self) -> dict[str, str]:
        return self.value


@dataclass
class Lease:
    snapshot_ref: SnapshotRef
    manifest: Manifest
    close_result: object = None
    close_calls: int = 0

    def close(self):
        self.close_calls += 1
        return Ok(None) if self.close_result is None else self.close_result


def entry(item: Lease, score: str = "1.25") -> DashboardIndexEntry:
    snapshot = item.snapshot_ref
    return DashboardIndexEntry(
        snapshot.session_id,
        snapshot.revision,
        snapshot.generation_id,
        snapshot.manifest_sha256,
        snapshot.session_id,
        "Exam",
        2026,
        ExamTerm.FIRST,
        SessionState.GRADED,
        "2026-07-28T01:02:03.456789Z",
        1,
        score,
        score,
        score,
        0,
    )


def lease(session_id: str, *, digest: str | None = None, close_result: object = None) -> Lease:
    manifest = Manifest({"generation_id": f"generation-{session_id}", "session_id": session_id})
    payload = (
        json.dumps(manifest.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )
    return Lease(
        SnapshotRef(
            session_id,
            1,
            f"generation-{session_id}",
            digest or hashlib.sha256(payload).hexdigest(),
        ),
        manifest,
        close_result,
    )


def test_build_is_sorted_and_digest_is_independent_of_clock() -> None:
    def project(item: Lease):
        return Ok(entry(item))

    first_items = (lease("b"), lease("a"))
    second_items = (lease("a"), lease("b"))
    first = build_dashboard_index(first_items, project, built_at=TIME)
    second = build_dashboard_index(second_items, project, built_at=TIME)

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert [item.session_id for item in first.value.record.entries] == ["a", "b"]
    assert first.value.record.source_digest == second.value.record.source_digest
    assert first.value.record.built_at == "2026-07-28T01:02:03.456789Z"
    assert first.value.record.entries[0].average_score == "1.25"
    assert all(item.close_calls == 1 for item in (*first_items, *second_items))


def test_invalid_committed_snapshot_is_quarantined_not_indexed() -> None:
    def project(item: Lease):
        return Err((ErrorInfo("INVALID_SESSION", "error.invalid_session"),))

    item = lease("a")
    result = build_dashboard_index((item,), project, built_at=TIME)

    assert isinstance(result, Ok)
    assert result.value.record.entries == ()
    assert result.warnings[0].code == "DASHBOARD_SESSION_QUARANTINED"
    assert item.close_calls == 1


def test_damaged_index_is_not_accepted_as_truth(tmp_path: Path) -> None:
    target = tmp_path / "dashboard_index.json"
    target.write_text("{bad", encoding="utf-8")
    assert isinstance(load_dashboard_index(target), Err)

    item = lease("a")
    rebuilt = rebuild_dashboard_index(
        lambda: Ok((item,)), lambda value: Ok(entry(value)), target, built_at=TIME
    )
    assert isinstance(rebuilt, Ok)
    assert load_dashboard_index(target).value.entries[0].session_id == "a"
    assert "DASHBOARD_INDEX_STALE" in {warning.code for warning in rebuilt.warnings}
    assert item.close_calls == 1


def test_postcommit_index_write_failure_is_a_warning(tmp_path: Path, monkeypatch) -> None:
    item = lease("a")
    built = build_dashboard_index((item,), lambda value: Ok(entry(value)), built_at=TIME)
    assert isinstance(built, Ok)
    monkeypatch.setattr(
        dashboard_index,
        "atomic_write_json",
        lambda _target, _value: Err(
            (ErrorInfo("ATOMIC_WRITE_FAILED", "error.atomic_write_failed"),)
        ),
    )

    result = replace_dashboard_index(tmp_path / "dashboard_index.json", built.value)

    assert isinstance(result, Ok)
    assert result.warnings[-1].code == "DASHBOARD_INDEX_WRITE_FAILED"
    assert item.close_calls == 1


def test_write_failure_does_not_leave_discovered_lease_open(tmp_path: Path, monkeypatch) -> None:
    item = lease("a")
    monkeypatch.setattr(
        dashboard_index,
        "atomic_write_json",
        lambda _target, _value: Err(
            (ErrorInfo("ATOMIC_WRITE_FAILED", "error.atomic_write_failed"),)
        ),
    )

    result = rebuild_dashboard_index(
        lambda: Ok((item,)),
        lambda value: Ok(entry(value)),
        tmp_path / "dashboard_index.json",
        built_at=TIME,
    )

    assert isinstance(result, Ok)
    assert item.close_calls == 1
    assert result.warnings[-1].code == "DASHBOARD_INDEX_WRITE_FAILED"


def test_mismatched_manifest_is_quarantined_before_projection_and_closed() -> None:
    item = lease("a", digest="b" * 64)
    projected: list[Lease] = []

    result = build_dashboard_index(
        (item,), lambda value: projected.append(value) or Ok(entry(value)), built_at=TIME
    )

    assert isinstance(result, Ok)
    assert result.value.record.entries == ()
    assert projected == []
    assert item.close_calls == 1
    assert {warning.code for warning in result.warnings} == {"DASHBOARD_MANIFEST_MISMATCH"}


def test_close_failure_is_a_warning_and_does_not_change_projection_truth() -> None:
    item = lease(
        "a",
        close_result=Err((ErrorInfo("LEASE_CLOSE_FAILED", "error.lease_close_failed"),)),
    )

    result = build_dashboard_index((item,), lambda value: Ok(entry(value)), built_at=TIME)

    assert isinstance(result, Ok)
    assert [value.session_id for value in result.value.record.entries] == ["a"]
    assert item.close_calls == 1
    assert result.warnings[-1].code == "DASHBOARD_LEASE_CLOSE_FAILED"
