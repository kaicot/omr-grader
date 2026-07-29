"""Immutable value contracts consumed by the detail-result UI and controller."""

from __future__ import annotations

from dataclasses import dataclass


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty str")


@dataclass(frozen=True, slots=True)
class NormalizedCell:
    """A keyboard/click-activatable OMR cell in normalized image coordinates."""

    kind: str
    question: int | None
    option: int | None
    left: float
    top: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if self.kind not in {"answer", "id"}:
            raise ValueError("kind must be 'answer' or 'id'")
        if self.kind == "answer" and (type(self.question) is not int or self.question < 1):
            raise ValueError("answer cells require a positive question")
        if self.kind == "answer" and (type(self.option) is not int or not 1 <= self.option <= 5):
            raise ValueError("answer cells require option 1 through 5")
        if self.kind == "id" and self.question is not None:
            raise ValueError("id cells cannot have a question")
        if self.kind == "id" and (
            self.option is not None
            and (type(self.option) is not int or not 0 <= self.option <= 9)
        ):
            raise ValueError("id cells require a digit option or None")
        if not all(type(v) in (int, float) for v in (self.left, self.top, self.width, self.height)):
            raise TypeError("cell bounds must be numeric")
        if not 0 <= self.left <= 1 or not 0 <= self.top <= 1 or self.width <= 0 or self.height <= 0:
            raise ValueError("cell bounds must be normalized and nonempty")
        if self.left + self.width > 1 or self.top + self.height > 1:
            raise ValueError("cell bounds must fit normalized image")


@dataclass(frozen=True, slots=True)
class DetailAnswerDisplay:
    question: int
    answer: int | None
    correct: bool | None

    def __post_init__(self) -> None:
        if type(self.question) is not int or self.question < 1:
            raise ValueError("question must be positive int")
        if self.answer is not None and (type(self.answer) is not int or not 1 <= self.answer <= 5):
            raise ValueError("answer must be 1 through 5 or None")
        if self.correct is not None and type(self.correct) is not bool:
            raise TypeError("correct must be bool or None")


@dataclass(frozen=True, slots=True)
class DetailStudentDisplay:
    # work_item_id, not student_id, is the correction authority. student_id may be duplicate.
    work_item_id: str
    student_id: str
    name: str
    rank: int | None
    score: str
    answers: tuple[DetailAnswerDisplay, ...]
    image_bytes: bytes | None = None
    cells: tuple[NormalizedCell, ...] = ()
    id_digits: tuple[int | None, ...] = ()
    id_conflict: str | None = None

    def __post_init__(self) -> None:
        _text(self.work_item_id, "work_item_id")
        if not isinstance(self.student_id, str) or not isinstance(self.name, str):
            raise TypeError("student_id and name must be strings")
        if self.rank is not None and (type(self.rank) is not int or self.rank < 1):
            raise ValueError("rank must be positive int or None")
        if not isinstance(self.score, str):
            raise TypeError("score must be str")
        if self.image_bytes is not None and not isinstance(self.image_bytes, bytes):
            raise TypeError("image_bytes must be immutable bytes or None")
        if not isinstance(self.answers, tuple) or not isinstance(self.cells, tuple):
            raise TypeError("answers and cells must be tuples")
        if not all(isinstance(answer, DetailAnswerDisplay) for answer in self.answers):
            raise TypeError("answers must contain DetailAnswerDisplay values")
        if not isinstance(self.id_digits, tuple):
            raise TypeError("id_digits must be tuple")
        if self.id_digits and (
            len(self.id_digits) != 8
            or any(
                d is not None and (type(d) is not int or not 0 <= d <= 9) for d in self.id_digits
            )
        ):
            raise ValueError("id_digits must contain exactly eight digits or None values")
        if not all(isinstance(cell, NormalizedCell) for cell in self.cells):
            raise TypeError("cells must contain NormalizedCell values")
        if self.id_conflict is not None and not isinstance(self.id_conflict, str):
            raise TypeError("id_conflict must be str or None")


@dataclass(frozen=True, slots=True)
class DetailSummaryDisplay:
    student_count: int
    average_score: str
    high_score: str
    low_score: str

    def __post_init__(self) -> None:
        if type(self.student_count) is not int or self.student_count < 0:
            raise ValueError("student_count must be nonnegative int")
        if not all(
            isinstance(v, str) for v in (self.average_score, self.high_score, self.low_score)
        ):
            raise TypeError("score summaries must be strings")


@dataclass(frozen=True, slots=True)
class DetailPageDisplay:
    session_id: str
    revision: int
    exam_name: str
    summary: DetailSummaryDisplay
    students: tuple[DetailStudentDisplay, ...]
    detail_handle: str | None = None

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        if self.detail_handle is not None:
            _text(self.detail_handle, "detail_handle")
        if (
            not isinstance(self.exam_name, str)
            or type(self.revision) is not int
            or self.revision < 0
        ):
            raise ValueError("invalid detail display")
        if not isinstance(self.summary, DetailSummaryDisplay):
            raise TypeError("summary must be DetailSummaryDisplay")
        if not isinstance(self.students, tuple):
            raise TypeError("students must be tuple")
        if not all(isinstance(student, DetailStudentDisplay) for student in self.students):
            raise TypeError("students must contain DetailStudentDisplay values")


@dataclass(frozen=True, slots=True)
class DetailAnswerEdit:
    work_item_id: str
    question: int
    before: int | None
    after: int | None

    def __post_init__(self) -> None:
        _text(self.work_item_id, "work_item_id")
        if type(self.question) is not int or self.question < 1:
            raise ValueError("question must be positive int")
        for value in (self.before, self.after):
            if value is not None and (type(value) is not int or not 1 <= value <= 5):
                raise ValueError("answer values must be 1 through 5 or None")


@dataclass(frozen=True, slots=True)
class DetailIdEdit:
    work_item_id: str
    position: int
    before: int | None
    after: int | None

    def __post_init__(self) -> None:
        _text(self.work_item_id, "work_item_id")
        if type(self.position) is not int or not 1 <= self.position <= 8:
            raise ValueError("id position must be 1 through 8")
        for value in (self.before, self.after):
            if value is not None and (type(value) is not int or not 0 <= value <= 9):
                raise ValueError("id values must be 0 through 9 or None")


DetailEdit = DetailAnswerEdit | DetailIdEdit


@dataclass(frozen=True, slots=True)
class DetailLoadRequest:
    session_id: str
    revision: int
    detail_handle: str | None
    work_item_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        if self.detail_handle is not None and (
            type(self.detail_handle) is not str or not self.detail_handle.strip()
        ):
            raise ValueError("detail_handle must be None or nonempty str")
        _text(self.work_item_id, "work_item_id")
        _text(self.correlation_id, "correlation_id")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be nonnegative int")

@dataclass(frozen=True, slots=True)
class DetailPageRequest:
    session_id: str
    revision: int
    intent: str
    edits: tuple[DetailEdit, ...]
    detail_handle: str | None
    correlation_id: str

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        if self.detail_handle is not None:
            _text(self.detail_handle, "detail_handle")
        _text(self.correlation_id, "correlation_id")
        if (
            type(self.revision) is not int
            or self.revision < 0
            or not isinstance(self.intent, str)
            or not self.intent
        ):
            raise ValueError("invalid request")
        if not isinstance(self.edits, tuple) or not all(
            isinstance(edit, DetailAnswerEdit | DetailIdEdit) for edit in self.edits
        ):
            raise TypeError("edits must be typed detail edits")


@dataclass(frozen=True, slots=True)
class DetailLoadResult:
    correlation_id: str
    student: DetailStudentDisplay

    def __post_init__(self) -> None:
        _text(self.correlation_id, "correlation_id")
        if not isinstance(self.student, DetailStudentDisplay):
            raise TypeError("student must be DetailStudentDisplay")



@dataclass(frozen=True, slots=True)
class DetailPreviewResult:
    correlation_id: str
    display: DetailPageDisplay

    def __post_init__(self) -> None:
        _text(self.correlation_id, "correlation_id")
        if not isinstance(self.display, DetailPageDisplay):
            raise TypeError("display must be DetailPageDisplay")


@dataclass(frozen=True, slots=True)
class DetailSaveResult:
    correlation_id: str
    display: DetailPageDisplay

    def __post_init__(self) -> None:
        _text(self.correlation_id, "correlation_id")
        if not isinstance(self.display, DetailPageDisplay):
            raise TypeError("display must be DetailPageDisplay")


__all__ = [name for name in globals() if name.startswith("Detail") or name == "NormalizedCell"]
