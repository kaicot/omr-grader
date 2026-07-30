"""Lease-backed dashboard cache adapter.

The index is strictly a disposable projection; all rebuild input is supplied as live
committed leases by the owning store adapter.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from omr_grader.application.ports import CommittedSnapshotLease
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import (
    DashboardIndexEntry,
    SessionRecord,
    validate_portable_component,
)
from omr_grader.infrastructure.dashboard_index import (
    ActiveLeaseDiscovery,
    DashboardIndexBuild,
    EntryProjector,
    rebuild_dashboard_index,
)


@dataclass(frozen=True, slots=True)
class DashboardListing:
    entries: tuple[DashboardIndexEntry, ...]
    warnings: tuple[ErrorInfo, ...] = ()


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason}),))


def project_dashboard_entry(lease: CommittedSnapshotLease) -> Result[DashboardIndexEntry]:
    """Project metadata from an allowlisted generation control payload."""
    try:
        ref = lease.snapshot_ref
        semantic = _semantic_statistics(lease)
        location = _location(lease)
        if semantic is None or location is None:
            return _error("DASHBOARD_SESSION_INVALID", "커밋된 세션 교차 참조가 올바르지 않습니다.")
        record, participant_count, average, highest, lowest = semantic
        if record.session_id != ref.session_id or record.revision != ref.revision:
            return _error(
                "DASHBOARD_SESSION_INVALID", "세션 메타데이터가 스냅샷과 일치하지 않습니다."
            )
        display_folder = location
        return Ok(
            DashboardIndexEntry(
                ref.session_id,
                ref.revision,
                ref.generation_id,
                ref.manifest_sha256,
                display_folder,
                record.exam_name,
                record.exam_year,
                record.exam_term,
                record.state,
                record.graded_at,
                participant_count,
                average,
                highest,
                lowest,
                lease.manifest.summary.manual_review,
            )
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _error("DASHBOARD_SESSION_INVALID", "세션 메타데이터가 올바르지 않습니다.")


def _semantic_statistics(
    lease: CommittedSnapshotLease,
) -> tuple[SessionRecord, int, str | None, str | None, str | None] | None:
    source = lease.open_allowlisted("semantic_inputs.json")
    if isinstance(source, Err):
        return None
    try:
        with source.value:
            envelope = _mapping(json.load(source.value))
        if set(envelope) != {"combined"}:
            return None
        combined = _mapping(envelope["combined"])
        session = _mapping(combined["session"])
        record = SessionRecord.from_dict(session)
        ref = lease.snapshot_ref
        if record.session_id != ref.session_id or record.revision != ref.revision:
            return None
        scores = combined.get("scores")
        if scores is None:
            return (record, 0, None, None, None)
        score_set = _mapping(scores)
        statistics = _mapping(score_set["statistics"])
        count = statistics.get("participant_count")
        average = statistics.get("average_score")
        highest = statistics.get("highest_score")
        lowest = statistics.get("lowest_score")
        values = (average, highest, lowest)
        if type(count) is not int or count < 0:
            return None
        if count == 0:
            return (record, 0, None, None, None) if values == (None, None, None) else None
        if not (isinstance(average, str) and isinstance(highest, str) and isinstance(lowest, str)):
            return None
        parsed = (Decimal(average), Decimal(highest), Decimal(lowest))
        if any(value < 0 for value in parsed) or not parsed[2] <= parsed[0] <= parsed[1]:
            return None
        return (record, count, average, highest, lowest)
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation, json.JSONDecodeError):
        return None


def _location(lease: CommittedSnapshotLease) -> str | None:
    """Read only the session-local metadata bound to the pinned generation identity."""
    try:
        session = Path(lease.root_path).parent.parent
        if Path(lease.root_path).parent.name != "generations":
            return None
        payload = _mapping(json.loads((session / "LOCATION.json").read_text(encoding="utf-8")))
        display_name = payload.get("display_name")
        if (
            payload.get("schema_version") != 1
            or payload.get("session_id") != lease.snapshot_ref.session_id
            or not isinstance(display_name, str)
            or isinstance(validate_portable_component(display_name), Err)
        ):
            return None
        if session.name != display_name:
            return None
        return display_name
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("session payload is not an object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


class DashboardRepository:
    """Read cache when sound, otherwise rebuild from store-owned active leases."""

    def __init__(
        self,
        index_path: str | Path,
        discover_active_leases: ActiveLeaseDiscovery,
        *,
        projector: EntryProjector = project_dashboard_entry,
        list_trash_entries: Callable[[], Result[tuple[DashboardIndexEntry, ...]]] | None = None,
    ) -> None:
        self._index_path = Path(index_path)
        self._discover = discover_active_leases
        self._projector = projector
        self._list_trash_entries = list_trash_entries

    def list_active(self) -> Result[DashboardListing]:
        # A cache cannot establish that it still represents CURRENT. Rebuild from
        # committed leases, retaining index replacement failures as warnings.
        rebuilt = self.rebuild_index()
        if isinstance(rebuilt, Err):
            return rebuilt
        return Ok(
            DashboardListing(rebuilt.value.record.entries, rebuilt.value.quarantined),
            rebuilt.value.quarantined,
        )

    def list_trash(self) -> Result[DashboardListing]:
        if self._list_trash_entries is None:
            return Ok(DashboardListing(()))
        rows = self._list_trash_entries()
        if isinstance(rows, Err):
            return rows
        return Ok(DashboardListing(rows.value))

    def rebuild_index(self) -> Result[DashboardIndexBuild]:
        return rebuild_dashboard_index(self._discover, self._projector, self._index_path)


__all__ = ["DashboardListing", "DashboardRepository", "project_dashboard_entry"]
