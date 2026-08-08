"""Dashboard listing and lifecycle use cases."""

from __future__ import annotations

from dataclasses import dataclass

from omr_grader.application.dto import (
    PermanentDeleteResult,
    SessionMutationRequest,
    SoftDeleteResult,
    TrashRestoreResult,
)
from omr_grader.application.ports import InternalSessionCoordinator
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.dashboard_repository import DashboardListing, DashboardRepository


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
