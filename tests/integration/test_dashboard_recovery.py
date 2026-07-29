from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omr_grader.application.dto import RecoveryIssue, RecoveryReport, RecoveryRequest, SnapshotRef
from omr_grader.domain.enums import ExamTerm, IndexState, SessionState
from omr_grader.domain.errors import Ok
from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.infrastructure.dashboard_index import recover_then_rebuild_dashboard_index


@dataclass(frozen=True)
class Manifest:
    value: dict[str, str]

    def to_dict(self) -> dict[str, str]:
        return self.value


@dataclass
class Lease:
    snapshot_ref: SnapshotRef
    manifest: Manifest
    close_calls: int = 0

    def close(self):
        self.close_calls += 1
        return Ok(None)


def lease(session_id: str) -> Lease:
    manifest = Manifest({"generation_id": f"generation-{session_id}", "session_id": session_id})
    payload = (
        json.dumps(manifest.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )
    return Lease(
        SnapshotRef(session_id, 1, f"generation-{session_id}", hashlib.sha256(payload).hexdigest()),
        manifest,
    )


class Store:
    def recover_sessions(self, request: RecoveryRequest):
        return Ok(
            RecoveryReport(1, IndexState.STALE, (RecoveryIssue("a", "BAD_CURRENT", "bad"),), ())
        )


def test_recovery_rebuilds_from_direct_active_lease_discovery_not_old_index(tmp_path: Path) -> None:
    target = tmp_path / "dashboard_index.json"
    target.write_text('{"old":"index"}', encoding="utf-8")
    item = lease("a")

    def project(_: Lease):
        return Ok(
            DashboardIndexEntry(
                "a",
                1,
                "generation-a",
                item.snapshot_ref.manifest_sha256,
                "a",
                "Exam",
                2026,
                ExamTerm.FIRST,
                SessionState.GRADED,
                "2026-07-28T01:02:03.456789Z",
                1,
                "1",
                "1",
                "1",
                0,
            )
        )

    result = recover_then_rebuild_dashboard_index(
        Store(),
        RecoveryRequest("root", True, True, "operation-1"),
        lambda: Ok((item,)),
        project,
        target,
        built_at=datetime(2026, 7, 28, 1, 2, 3, 456789, tzinfo=UTC),
    )

    assert isinstance(result, Ok)
    assert result.value.record.entries[0].session_id == "a"
    assert any(warning.code == "DASHBOARD_SESSION_QUARANTINED" for warning in result.warnings)
    assert item.close_calls == 1
