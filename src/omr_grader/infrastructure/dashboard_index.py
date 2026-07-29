"""Non-authoritative, atomically replaced dashboard index projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omr_grader.application.dto import RecoveryRequest
from omr_grader.application.ports import CommittedSnapshotLease, InternalSessionCoordinator
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import DashboardIndexEntry, DashboardIndexRecord
from omr_grader.infrastructure.atomic_io import _normalize_json, atomic_write_json

_INDEX_NAME = "dashboard_index.json"


class EntryProjector(Protocol):
    """Projects one already-open committed snapshot into an index entry."""

    def __call__(self, lease: CommittedSnapshotLease) -> Result[DashboardIndexEntry]: ...


class ActiveLeaseDiscovery(Protocol):
    """Store-owned discovery boundary; an index must never discover from itself."""

    def __call__(self) -> Result[tuple[CommittedSnapshotLease, ...]]: ...


@dataclass(frozen=True, slots=True)
class DashboardIndexBuild:
    """A rebuilt record plus non-fatal quarantine and replacement warnings."""

    record: DashboardIndexRecord
    quarantined: tuple[ErrorInfo, ...]


def _issue(
    code: str, reason: str, *, warning: bool = True, exc: BaseException | None = None
) -> ErrorInfo:
    return ErrorInfo(
        code,
        f"{'warning' if warning else 'error'}.{code.lower()}",
        context={"reason": reason},
        cause_type=type(exc).__name__ if exc is not None else None,
    )


def _canonical_timestamp(value: datetime | None) -> str:
    instant = datetime.now(UTC) if value is None else value
    if instant.tzinfo is None:
        raise ValueError("built_at must be timezone-aware")
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _source_digest(entries: tuple[DashboardIndexEntry, ...]) -> str:
    """Hash precisely the authoritative generation identity, not this projection's clock."""
    source = [
        {
            "generation_id": entry.generation_id,
            "manifest_sha256": entry.manifest_sha256,
            "revision": entry.revision,
            "session_id": entry.session_id,
        }
        for entry in entries
    ]
    payload = json.dumps(source, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _manifest_digest(lease: CommittedSnapshotLease) -> str:
    """Reproduce the canonical on-disk manifest digest bound by ``SnapshotRef``."""
    payload = (
        json.dumps(
            _normalize_json(lease.manifest.to_dict()),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(payload).hexdigest()


def _close_lease(lease: CommittedSnapshotLease) -> ErrorInfo | None:
    try:
        closed = lease.close()
    except BaseException as exc:
        return _issue(
            "DASHBOARD_LEASE_CLOSE_FAILED",
            "대시보드 인덱스 스냅샷 잠금을 해제하지 못했습니다.",
            exc=exc,
        )
    if isinstance(closed, Err):
        return _issue(
            "DASHBOARD_LEASE_CLOSE_FAILED",
            "대시보드 인덱스 스냅샷 잠금을 해제하지 못했습니다.",
        )
    return None


def build_dashboard_index(
    leases: Iterable[CommittedSnapshotLease],
    projector: EntryProjector,
    *,
    built_at: datetime | None = None,
) -> Result[DashboardIndexBuild]:
    """Build from committed leases only; invalid snapshots are quarantined, never invented."""
    entries: list[DashboardIndexEntry] = []
    warnings: list[ErrorInfo] = []
    seen: set[str] = set()
    for lease in leases:
        try:
            snapshot = lease.snapshot_ref
            try:
                manifest_sha256 = _manifest_digest(lease)
            except (RecursionError, TypeError, ValueError):
                manifest_sha256 = None
            if manifest_sha256 != snapshot.manifest_sha256:
                warnings.append(
                    _issue(
                        "DASHBOARD_MANIFEST_MISMATCH",
                        "커밋된 스냅샷 매니페스트 다이제스트가 일치하지 않습니다.",
                    )
                )
                continue
            projected = projector(lease)
            if isinstance(projected, Err):
                warnings.append(
                    _issue(
                        "DASHBOARD_SESSION_QUARANTINED",
                        "손상된 커밋 세션을 대시보드 인덱스에서 제외했습니다.",
                    )
                )
                continue
            entry = projected.value
            if entry.session_id in seen:
                warnings.append(
                    _issue(
                        "DASHBOARD_DUPLICATE_SESSION", "중복된 활성 세션은 인덱스에서 제외했습니다."
                    )
                )
                continue
            if (
                entry.session_id != snapshot.session_id
                or entry.revision != snapshot.revision
                or entry.generation_id != snapshot.generation_id
                or entry.manifest_sha256 != snapshot.manifest_sha256
            ):
                warnings.append(
                    _issue(
                        "DASHBOARD_SNAPSHOT_MISMATCH", "커밋된 스냅샷 식별자가 일치하지 않습니다."
                    )
                )
                continue
            seen.add(entry.session_id)
            entries.append(entry)
        finally:
            close_warning = _close_lease(lease)
            if close_warning is not None:
                warnings.append(close_warning)
    ordered = tuple(sorted(entries, key=lambda entry: entry.session_id))
    try:
        record = DashboardIndexRecord(
            1, _canonical_timestamp(built_at), _source_digest(ordered), ordered
        )
    except ValueError as exc:
        return Err(
            (
                _issue(
                    "DASHBOARD_INDEX_INVALID",
                    "대시보드 인덱스를 만들 수 없습니다.",
                    warning=False,
                    exc=exc,
                ),
            )
        )
    return Ok(DashboardIndexBuild(record, tuple(warnings)), tuple(warnings))


def replace_dashboard_index(
    target: Path, build: DashboardIndexBuild
) -> Result[DashboardIndexBuild]:
    """Atomically replace the projection after a successful authoritative commit."""
    saved = atomic_write_json(target, build.record.to_dict())
    if isinstance(saved, Err):
        warning = _issue(
            "DASHBOARD_INDEX_WRITE_FAILED", "대시보드 인덱스를 갱신하지 못했습니다.", exc=None
        )
        return Ok(
            DashboardIndexBuild(build.record, (*build.quarantined, warning)),
            (*build.quarantined, warning),
        )
    return Ok(build, build.quarantined)


def rebuild_dashboard_index(
    discover_active_leases: ActiveLeaseDiscovery,
    projector: EntryProjector,
    target: Path,
    *,
    built_at: datetime | None = None,
) -> Result[DashboardIndexBuild]:
    """Directly query active committed leases; the old index is deliberately ignored."""
    previous = load_dashboard_index(target) if target.exists() else None
    discovered = discover_active_leases()
    if isinstance(discovered, Err):
        return discovered
    built = build_dashboard_index(discovered.value, projector, built_at=built_at)
    if isinstance(built, Err):
        return built
    warnings = built.value.quarantined
    if isinstance(previous, Err) or (
        isinstance(previous, Ok)
        and previous.value.source_digest != built.value.record.source_digest
    ):
        warnings = (
            *warnings,
            _issue(
                "DASHBOARD_INDEX_STALE",
                "기존 대시보드 인덱스를 무시하고 권한 스냅샷에서 재구축했습니다.",
            ),
        )
    return replace_dashboard_index(target, DashboardIndexBuild(built.value.record, warnings))


def recover_then_rebuild_dashboard_index(
    session_store: InternalSessionCoordinator,
    recovery: RecoveryRequest,
    discover_active_leases: ActiveLeaseDiscovery,
    projector: EntryProjector,
    target: Path,
    *,
    built_at: datetime | None = None,
) -> Result[DashboardIndexBuild]:
    """Recovery seam: store owns transaction recovery; this module owns only projection repair."""
    recovered = session_store.recover_sessions(recovery)
    if isinstance(recovered, Err):
        return recovered
    rebuilt = rebuild_dashboard_index(discover_active_leases, projector, target, built_at=built_at)
    if isinstance(rebuilt, Err):
        return rebuilt
    recovery_warnings = tuple(
        _issue("DASHBOARD_SESSION_QUARANTINED", "복구 중 손상된 세션을 격리했습니다.")
        for _ in recovered.value.quarantined
    )
    result = DashboardIndexBuild(
        rebuilt.value.record, (*rebuilt.value.quarantined, *recovery_warnings)
    )
    return Ok(result, result.quarantined)


def load_dashboard_index(target: Path) -> Result[DashboardIndexRecord]:
    """Strictly load a projection; callers must rebuild rather than treat failures as authority."""
    try:
        with target.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return Ok(DashboardIndexRecord.from_dict(value))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return Err(
            (
                _issue(
                    "DASHBOARD_INDEX_STALE",
                    "대시보드 인덱스를 신뢰할 수 없어 재구축해야 합니다.",
                    warning=False,
                    exc=exc,
                ),
            )
        )


__all__ = [
    "ActiveLeaseDiscovery",
    "DashboardIndexBuild",
    "EntryProjector",
    "build_dashboard_index",
    "load_dashboard_index",
    "rebuild_dashboard_index",
    "recover_then_rebuild_dashboard_index",
    "replace_dashboard_index",
]
