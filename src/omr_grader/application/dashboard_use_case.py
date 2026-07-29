"""Dashboard reporting use case with held committed-snapshot leases."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omr_grader.application.dto import (
    CollisionPolicy,
    CombinedReportRequest,
    CombinedReportResult,
    PermanentDeleteResult,
    SessionMutationRequest,
    SnapshotRequest,
    SoftDeleteResult,
    TrashRestoreResult,
)
from omr_grader.application.ports import CommittedSnapshotLease, InternalSessionCoordinator
from omr_grader.domain.enums import ExamTerm, SessionState, SnapshotPurpose
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.dashboard_repository import DashboardListing, DashboardRepository
from omr_grader.workbooks.combined_book import (
    CombinedExam,
    build_combined_workbook,
    parse_combined_semantics,
)


def _error(code: str, reason: str, exc: BaseException | None = None) -> Err:
    return Err(
        (
            ErrorInfo(
                code,
                f"error.{code.lower()}",
                context={"reason": reason},
                cause_type=None if exc is None else type(exc).__name__,
            ),
        )
    )


def _publish(destination: Path, payload: bytes, collision: CollisionPolicy) -> Result[str]:
    """Publish a fully written sibling temporary file without exposing partial XLSX bytes."""
    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(16)}.tmp"
    digest: str | None = None
    primary: Err | None = None
    cleanup: Err | None = None
    try:
        if not destination.parent.is_dir():
            primary = _error("REPORT_DESTINATION_INVALID", "저장 폴더를 찾을 수 없습니다.")
        else:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if collision is CollisionPolicy.REPLACE:
                os.replace(temporary, destination)
            else:
                # Hard-link publication is an atomic create-if-absent operation on the same volume.
                os.link(temporary, destination)
            digest = hashlib.sha256(payload).hexdigest()
    except FileExistsError as exc:
        primary = _error("REPORT_DESTINATION_EXISTS", "같은 이름의 보고서가 이미 있습니다.", exc)
    except OSError as exc:
        primary = _error(
            "REPORT_PUBLISH_FAILED", "통합 성적표를 원자적으로 저장할 수 없습니다.", exc
        )
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            cleanup = _error(
                "REPORT_TEMPORARY_CLEANUP_FAILED",
                "통합 성적표 임시 파일을 제거할 수 없습니다.",
                exc,
            )
    if primary is not None and cleanup is not None:
        return Err(primary.errors + cleanup.errors)
    if primary is not None:
        return primary
    if cleanup is not None:
        return cleanup
    if digest is None:
        return _error("REPORT_PUBLISH_FAILED", "통합 성적표를 원자적으로 저장할 수 없습니다.")
    return Ok(digest)

def _close_leases(leases: list[CommittedSnapshotLease]) -> tuple[ErrorInfo, ...]:
    errors: list[ErrorInfo] = []
    for lease in reversed(leases):
        try:
            closed = lease.close()
        except BaseException as exc:
            errors.extend(
                _error(
                    "COMBINED_LEASE_CLOSE_FAILED",
                    "통합 성적표 스냅샷 임대를 해제할 수 없습니다.",
                    exc,
                ).errors
            )
            continue
        if isinstance(closed, Err):
            errors.extend(closed.errors)
        elif not isinstance(closed, Ok):
            errors.extend(
                _error(
                    "COMBINED_LEASE_CLOSE_FAILED",
                    "통합 성적표 스냅샷 임대 해제 결과가 올바르지 않습니다.",
                    TypeError(f"expected Result[None], got {type(closed).__name__}"),
                ).errors
            )
    return tuple(errors)


def _cleanup_warning(error: ErrorInfo) -> ErrorInfo:
    return ErrorInfo(
        error.code,
        f"warning.{error.code.lower()}",
        error.field_path,
        dict(error.context),
        error.retryable,
        error.cause_type,
    )

@dataclass(frozen=True, slots=True)
class DashboardFilter:
    query: str = ""
    exam_year: int | None = None
    exam_term: ExamTerm | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise TypeError("query must be str")
        if self.exam_year is not None and (
            type(self.exam_year) is not int or not 2000 <= self.exam_year <= 2100
        ):
            raise ValueError("exam_year must be between 2000 and 2100")
        if self.exam_term is not None and not isinstance(self.exam_term, ExamTerm):
            raise TypeError("exam_term must be ExamTerm")


class DashboardApplicationService:
    """Value-only dashboard and lifecycle facade over injected authority seams."""

    def __init__(
        self,
        coordinator: InternalSessionCoordinator,
        repository: DashboardRepository,
        *,
        write_enabled: bool = True,
    ) -> None:
        if type(write_enabled) is not bool:
            raise TypeError("write_enabled must be bool")
        self._coordinator = coordinator
        self._repository = repository
        self._write_enabled = write_enabled

    def list_exams(self) -> Result[DashboardListing]:
        return self._repository.list_active()

    def search_filter(self, filter: DashboardFilter) -> Result[DashboardListing]:
        listed = self.list_exams()
        if isinstance(listed, Err):
            return listed
        needle = filter.query.strip().casefold()
        rows = tuple(
            row
            for row in listed.value.entries
            if (not needle or needle in row.exam_name.casefold())
            and (filter.exam_year is None or row.exam_year == filter.exam_year)
            and (filter.exam_term is None or row.exam_term is filter.exam_term)
        )
        rows = tuple(sorted(rows, key=lambda row: (row.exam_name.casefold(), row.session_id)))
        return Ok(DashboardListing(rows, listed.value.warnings), listed.warnings)

    def _mutation_allowed(self) -> Err | None:
        return (
            None
            if self._write_enabled
            else _error(
                "DASHBOARD_READ_ONLY", "읽기 전용 실행에서는 시험 기록을 변경할 수 없습니다."
            )
        )

    def soft_delete(self, request: SessionMutationRequest) -> Result[SoftDeleteResult]:
        denied = self._mutation_allowed()
        return denied if denied is not None else self._coordinator.soft_delete(request)

    def restore_from_trash(self, request: SessionMutationRequest) -> Result[TrashRestoreResult]:
        denied = self._mutation_allowed()
        return denied if denied is not None else self._coordinator.restore_from_trash(request)

    def permanently_delete(self, request: SessionMutationRequest) -> Result[PermanentDeleteResult]:
        denied = self._mutation_allowed()
        return denied if denied is not None else self._coordinator.permanently_delete(request)


class DashboardUseCase:
    """Build reports only from live, leased, allowlisted semantic authority."""

    def __init__(
        self, coordinator: InternalSessionCoordinator, *, write_enabled: bool = True
    ) -> None:
        if type(write_enabled) is not bool:
            raise TypeError("write_enabled must be bool")
        self._coordinator = coordinator
        self._write_enabled = write_enabled

    def build_combined_report(self, request: CombinedReportRequest) -> Result[CombinedReportResult]:
        if not self._write_enabled:
            return _error(
                "DASHBOARD_READ_ONLY", "읽기 전용 실행에서는 통합 성적표를 만들 수 없습니다."
            )
        leases: list[CommittedSnapshotLease] = []
        close_errors: tuple[ErrorInfo, ...] = ()
        result: Result[CombinedReportResult]
        try:
            if len(set(request.session_ids)) != len(request.session_ids):
                result = _error(
                    "COMBINED_SELECTION_DUPLICATE", "같은 시험을 둘 이상 선택할 수 없습니다."
                )
            else:
                for session_id in request.session_ids:
                    opened = self._coordinator.open_committed_snapshot(
                        SnapshotRequest(session_id, None, SnapshotPurpose.COMBINED)
                    )
                    if isinstance(opened, Err):
                        result = opened
                        break
                    leases.append(opened.value)
                else:
                    exams: list[CombinedExam] = []
                    for lease in leases:
                        if request.graded_only and lease.manifest.state not in (
                            SessionState.GRADED,
                            SessionState.FINALIZED,
                        ):
                            result = _error(
                                "COMBINED_GRADED_SELECTION_REQUIRED",
                                "채점된 시험만 선택해야 합니다.",
                            )
                            break
                        source = lease.open_allowlisted("semantic_inputs.json")
                        if isinstance(source, Err):
                            result = _error(
                                "COMBINED_SEMANTICS_MISSING",
                                "통합 보고서용 의미 스냅샷이 없습니다.",
                            )
                            break
                        try:
                            with source.value:
                                payload = json.load(source.value)
                            exams.append(parse_combined_semantics(payload, lease.snapshot_ref))
                        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                            result = _error(
                                "COMBINED_SEMANTICS_INVALID",
                                "통합 성적표용 의미 스냅샷이 올바르지 않습니다.",
                                exc,
                            )
                            break
                    else:
                        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        try:
                            workbook = build_combined_workbook(exams, generated_at)
                        except (TypeError, ValueError) as exc:
                            result = _error(
                                "COMBINED_PROJECTION_INVALID",
                                "통합 성적표 데이터를 만들 수 없습니다.",
                                exc,
                            )
                        else:
                            published = _publish(
                                Path(request.destination), workbook, request.collision
                            )
                            if isinstance(published, Err):
                                result = published
                            else:
                                result = Ok(
                                    CombinedReportResult(
                                        True,
                                        request.destination,
                                        published.value,
                                        tuple(lease.snapshot_ref for lease in leases),
                                        request.operation_id,
                                    )
                                )
        except BaseException as exc:
            result = _error(
                "COMBINED_REPORT_FAILED",
                "통합 성적표를 만들 수 없습니다.",
                exc,
            )
        finally:
            close_errors = _close_leases(leases)
        if isinstance(result, Err):
            return (
                result
                if not close_errors
                else Err(result.errors + close_errors)
            )
        if close_errors:
            return Ok(
                result.value,
                result.warnings + tuple(_cleanup_warning(error) for error in close_errors),
            )
        return result
