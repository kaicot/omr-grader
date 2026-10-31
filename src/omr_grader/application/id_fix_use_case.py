"""Fix, or undo the fix of, one answer sheet's student ID as a committed generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from omr_grader.domain.enums import OperationKind, SessionState
from omr_grader.domain.errors import Err, ErrorInfo, Result
from omr_grader.domain.id_fixes import StudentIdFix

from .dto import CommitGenerationResult, GenerationMutation, StudentIdFixView
from .grading_use_case import CommittedGradingSnapshotReader
from .ports import InternalSessionCoordinator

_STUDENT_ID = re.compile(r"[0-9]{8}")
_FIXABLE = (SessionState.RECOGNIZED, SessionState.GRADED)


@dataclass(frozen=True, slots=True)
class StudentIdFixCommand:
    """``student_id`` None puts the sheet back to the ID it read as."""

    session_id: str
    expected_revision: int
    operation_id: str
    work_item_id: str
    student_id: str | None


def _refuse(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", "student_id", {"reason": reason}),))


class StudentIdFixUseCase:
    def __init__(
        self,
        snapshots: CommittedGradingSnapshotReader,
        coordinator: InternalSessionCoordinator,
    ) -> None:
        self._snapshots = snapshots
        self._coordinator = coordinator

    def fix(self, command: StudentIdFixCommand) -> Result[CommitGenerationResult]:
        committed = self._snapshots.read_grading_snapshot(
            command.session_id, command.expected_revision
        )
        if isinstance(committed, Err):
            return committed
        snapshot = committed.value
        if snapshot.state not in _FIXABLE:
            return _refuse(
                "ID_FIX_STATE",
                "확정한 시험의 학번은 고칠 수 없습니다. 인식만 했거나 채점한 시험에서 고치세요.",
            )
        read = next(
            (item for item in snapshot.responses if item.work_item_id == command.work_item_id),
            None,
        )
        if read is None:
            return _refuse("ID_FIX_SHEET", "고칠 답안지를 이 시험에서 찾지 못했습니다.")
        others = tuple(
            fix for fix in snapshot.id_fixes if fix.work_item_id != command.work_item_id
        )
        current = next(
            (fix for fix in snapshot.id_fixes if fix.work_item_id == command.work_item_id), None
        )
        target = command.student_id
        if target is not None and _STUDENT_ID.fullmatch(target) is None:
            return _refuse("ID_FIX_INVALID", "학번은 숫자 8자리로 입력하세요.")
        if target is None or target == read.student_id:
            # Back to what the sheet read as: only a sheet that was fixed can go back.
            if current is None:
                return _refuse(
                    "ID_FIX_NOOP",
                    "이 답안지는 이미 그 학번입니다."
                    if target is not None
                    else "이 답안지는 고친 적이 없습니다.",
                )
            fixes = others
        else:
            if current is not None and current.student_id == target:
                return _refuse("ID_FIX_NOOP", "이 답안지는 이미 그 학번으로 고쳐져 있습니다.")
            fixed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            fixes = (*others, StudentIdFix(command.work_item_id, target, read.student_id, fixed_at))
        mutation = GenerationMutation(
            command.session_id,
            command.operation_id,
            OperationKind.FIX_STUDENT_ID,
            command.expected_revision,
            snapshot.state,
            StudentIdFixView(snapshot.state, tuple(sorted(fixes, key=lambda fix: fix.work_item_id))),
            # Before grading, what was read travels as the projection each generation re-pins.
            snapshot.projection_request,
        )
        return self._coordinator.commit_generation(mutation)


__all__ = ["StudentIdFixCommand", "StudentIdFixUseCase"]
