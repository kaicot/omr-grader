"""Immutable request and display values for the grading screen."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from omr_grader.application.dto import AnswerKeyValidation
from omr_grader.domain.enums import KeyQuestionStatus
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result


@dataclass(frozen=True, slots=True)
class ConnectedSessionDisplay:
    """Read-only identity and revision of the response session shown by the view."""

    session_id: str
    revision: int
    exam_name: str
    response_path: str
    is_regrade: bool = False

    def __post_init__(self) -> None:
        for name in ("session_id", "exam_name", "response_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be nonnegative int")
        if type(self.is_regrade) is not bool:
            raise TypeError("is_regrade must be bool")


@dataclass(frozen=True, slots=True)
class GradingPageRequest:
    """Value-only user intent emitted by :class:`GradingPage`."""

    session_id: str
    revision: int
    response_path: str
    answer_key_path: str | None
    answer_key_sheet: str | None
    operation_id: str
    intent: str
    is_regrade: bool

    def __post_init__(self) -> None:
        for name in ("session_id", "response_path", "operation_id", "intent"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be nonnegative int")
        for name in ("answer_key_path", "answer_key_sheet"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty str or None")
        if (self.answer_key_path is None) != (self.answer_key_sheet is None):
            raise ValueError("answer key path and sheet must be provided together")
        if type(self.is_regrade) is not bool:
            raise TypeError("is_regrade must be bool")


@dataclass(frozen=True, slots=True)
class AnswerKeyValidationDisplay:
    source_path: str | None
    sheet_name: str | None
    source_name: str | None
    question_count: int
    unasked_count: int
    total_points: str
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("source_path", "sheet_name", "source_name"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty str or None")
        if (self.source_path is None) != (self.sheet_name is None):
            raise ValueError("source path and sheet name must be provided together")
        if type(self.question_count) is not int or self.question_count < 0:
            raise ValueError("question_count must be nonnegative")
        if type(self.unasked_count) is not int or self.unasked_count < 0:
            raise ValueError("unasked_count must be nonnegative")
        if not isinstance(self.total_points, str):
            raise TypeError("total_points must be str")
        if not all(isinstance(error, str) and error for error in self.errors):
            raise ValueError("errors must be nonempty strings")

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.source_name is not None


@dataclass(frozen=True, slots=True)
class GradingProgressDisplay:
    completed: int
    total: int
    elapsed_seconds: int
    eta_seconds: int | None
    status: str = ""

    def __post_init__(self) -> None:
        if any(
            type(value) is not int for value in (self.completed, self.total, self.elapsed_seconds)
        ):
            raise TypeError("progress counts and elapsed_seconds must be ints")
        if self.eta_seconds is not None and type(self.eta_seconds) is not int:
            raise TypeError("eta_seconds must be int or None")
        if self.completed < 0 or self.total < 0 or self.completed > self.total:
            raise ValueError("invalid grading progress")
        if self.elapsed_seconds < 0 or self.eta_seconds is not None and self.eta_seconds < 0:
            raise ValueError("progress times must be nonnegative")
        if not isinstance(self.status, str):
            raise TypeError("status must be str")


class GradingPresenter:
    """Maps typed validation data into Korean, immutable UI values."""

    @staticmethod
    def connected_session(
        session_id: str,
        revision: int,
        exam_name: str,
        response_path: str,
        *,
        is_regrade: bool = False,
    ) -> ConnectedSessionDisplay:
        return ConnectedSessionDisplay(session_id, revision, exam_name, response_path, is_regrade)

    @staticmethod
    def validation(
        result: AnswerKeyValidation | Result[AnswerKeyValidation] | Iterable[ErrorInfo],
    ) -> AnswerKeyValidationDisplay:
        if isinstance(result, Ok):
            result = result.value
        if isinstance(result, Err):
            return GradingPresenter.errors(result.errors)
        if isinstance(result, AnswerKeyValidation):
            snapshot = result.snapshot
            errors = tuple(
                GradingPresenter.error_text(error) for error in snapshot.validation_errors
            )
            asked = sum(entry.status is not KeyQuestionStatus.UNASKED for entry in snapshot.entries)
            unasked = sum(entry.status is KeyQuestionStatus.UNASKED for entry in snapshot.entries)
            total = sum((Decimal(entry.points) for entry in snapshot.entries), Decimal("0"))
            return AnswerKeyValidationDisplay(
                None,
                None,
                snapshot.source_name,
                asked,
                unasked,
                GradingPresenter._decimal_text(total),
                errors,
            )
        return GradingPresenter.errors(tuple(result))

    @staticmethod
    def errors(errors: Iterable[ErrorInfo]) -> AnswerKeyValidationDisplay:
        return AnswerKeyValidationDisplay(
            None,
            None,
            None,
            0,
            0,
            "0",
            tuple(GradingPresenter.error_text(error) for error in errors),
        )

    @staticmethod
    def error_text(error: ErrorInfo) -> str:
        location = f"{error.field_path}: " if error.field_path else ""
        value = error.context.get("value")
        labels = {
            "XLSX_READ_FAILED": "정답표 파일을 읽을 수 없습니다.",
            "XLSX_OPEN_FAILED": "정답표 파일을 열 수 없습니다.",
            "XLSX_WRITE_FAILED": "정답표 샘플 파일을 저장할 수 없습니다.",
            "XLSX_INVALID_WORKBOOK": "올바른 Excel 정답표 파일이 아닙니다.",
            "XLSX_INVALID_PACKAGE": "정답표 파일 구조가 올바르지 않습니다.",
            "XLSX_PACKAGE_QUOTA": "정답표 파일 크기 또는 압축 해제 범위를 초과했습니다.",
            "XLSX_FORBIDDEN_FEATURE": "정답표에 허용되지 않는 기능이 포함되어 있습니다.",
            "XLSX_FORMULA_FORBIDDEN": "정답표에는 수식을 사용할 수 없습니다.",
            "XLSX_SHEET_NOT_FOUND": "선택한 정답표 시트를 찾을 수 없습니다.",
            "XLSX_DIMENSION_QUOTA": "정답표의 행 또는 열 수가 허용 범위를 초과했습니다.",
            "XLSX_HEADERS_INVALID": (
                "정답표 열 이름이 올바르지 않습니다. '문항번호, 정답, 배점'을 확인하세요."
            ),
            "XLSX_CELL_TYPE": "문항번호는 정수이고 정답은 문자, 배점은 숫자여야 합니다.",
            "XLSX_QUESTION_INVALID": "문항번호는 1~100 사이의 중복되지 않은 정수여야 합니다.",
            "XLSX_QUESTION_DUPLICATE": "문항번호가 중복되었습니다.",
            "XLSX_ANSWER_INVALID": "정답은 1~5, 복수 정답, 0 또는 전체만 입력할 수 있습니다.",
            "XLSX_POINTS_INVALID": "배점은 0 이상의 숫자여야 합니다.",
            "XLSX_UNASKED_POINTS": "미출제 문항의 배점은 0이어야 합니다.",
            "XLSX_FORMAT_INVALID": "올바른 Excel 정답표 파일이 아닙니다.",
        }
        detail = labels.get(error.code, "정답표를 검증하지 못했습니다.")
        if value is not None:
            detail = f"{detail} 입력값: '{value}'"
        return f"{location}{detail}"

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text


__all__ = [
    "AnswerKeyValidationDisplay",
    "ConnectedSessionDisplay",
    "GradingPageRequest",
    "GradingPresenter",
    "GradingProgressDisplay",
]
