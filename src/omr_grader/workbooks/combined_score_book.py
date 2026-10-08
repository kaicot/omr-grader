"""Formula-free 합산 성적표: per-student totals across the parts of a split exam.

Each part is one separately scanned and graded session.  The builder is pure: it takes
already-read per-part scores and returns the workbook bytes, so the caller decides where and
how to write them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from omr_grader.workbooks.response_book import mark_review, style_header_row
from omr_grader.workbooks.schemas import escape_formula_text
from omr_grader.workbooks.subject_config import Subject, SubjectConfig

QUESTION_COUNT = 100
RESULT_SHEET_NAME = "합산결과"
UNREADABLE_SHEET_NAME = "학번 확인 필요"
PARTS_SHEET_NAME = "파트"
SUBJECT_SHEET_NAME = "과목구성"
PASS_TEXT = "합격"
FAIL_TEXT = "불합격"
_FREEZE_PANES = "D2"
_NOTE_SEPARATOR = ", "
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class PartStudent:
    """One graded answer sheet of a part.

    ``student_id`` is ``None`` (or blank) when the ID could not be read.  ``total`` is ``None``
    when the sheet is held back because answers still need a check; ``earned`` then is empty.
    Otherwise ``earned[question - 1]`` is the points the student got for that question
    (``None`` for questions the key does not ask).  ``part_order`` is the sheet's 순번 in the
    part's own score book.
    """

    student_id: str | None
    name: str
    total: Decimal | None
    earned: tuple[Decimal | None, ...]
    source_label: str
    part_order: int

    @property
    def readable_id(self) -> str | None:
        return self.student_id.strip() or None if self.student_id else None


@dataclass(frozen=True, slots=True)
class PartScores:
    """One graded session.  ``question_points[question - 1]`` is the key's points for a
    question, ``None`` where the key does not ask it (everyone-correct questions count as asked).
    """

    label: str
    exam_name: str
    folder_name: str
    graded_at: str | None
    question_points: tuple[Decimal | None, ...]
    rows: tuple[PartStudent, ...]

    @property
    def maximum(self) -> Decimal:
        return sum((item for item in self.question_points if item is not None), Decimal(0))


@dataclass(frozen=True, slots=True)
class CombinedCounts:
    students: int
    complete: int
    needs_review: int
    unreadable: int


@dataclass(frozen=True, slots=True)
class _Row:
    student_id: str
    name: str
    part_totals: tuple[Decimal | None, ...]
    total: Decimal | None
    rank: int | None
    subject_points: tuple[Decimal | None, ...]
    passed: bool | None
    problems: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.problems


def _text(value: str) -> str:
    return escape_formula_text(value)


def _validate(parts: Sequence[PartScores], subjects: SubjectConfig | None) -> None:
    if not parts:
        raise ValueError("at least one part is required")
    if len({part.label for part in parts}) != len(parts):
        raise ValueError("part labels must be unique")
    for part in parts:
        if len(part.question_points) != QUESTION_COUNT or any(
            item.earned and len(item.earned) != QUESTION_COUNT for item in part.rows
        ):
            raise ValueError("question points must cover 100 questions")
    if subjects is not None and any(number > len(parts) for number in subjects.part_numbers()):
        raise ValueError("subject configuration refers to a part that is not selected")


def _subject_max(subject: Subject, parts: Sequence[PartScores]) -> Decimal:
    total = Decimal(0)
    for item in subject.ranges:
        points = parts[item.part - 1].question_points
        total += sum(
            (value for value in points[item.first - 1 : item.last] if value is not None),
            Decimal(0),
        )
    return total


def _subject_earned(subject: Subject, students: Sequence[PartStudent | None]) -> Decimal:
    total = Decimal(0)
    for item in subject.ranges:
        student = students[item.part - 1]
        if student is None:
            continue
        total += sum(
            (value for value in student.earned[item.first - 1 : item.last] if value is not None),
            Decimal(0),
        )
    return total


def _collect(
    parts: Sequence[PartScores], subjects: SubjectConfig | None
) -> tuple[list[_Row], list[tuple[PartScores, PartStudent]]]:
    unreadable: list[tuple[PartScores, PartStudent]] = []
    by_id: list[dict[str, list[PartStudent]]] = []
    for part in parts:
        grouped: dict[str, list[PartStudent]] = {}
        for student in part.rows:
            student_id = student.readable_id
            if student_id is None:
                unreadable.append((part, student))
            else:
                grouped.setdefault(student_id, []).append(student)
        by_id.append(grouped)
    ids = {student_id for grouped in by_id for student_id in grouped}
    config = subjects.subjects if subjects is not None else ()
    maximum = sum((part.maximum for part in parts), Decimal(0))
    rows: list[_Row] = []
    for student_id in ids:
        found = [grouped.get(student_id, []) for grouped in by_id]
        names = [item.name for group in found for item in group if item.name]
        problems: list[str] = []
        singles: list[PartStudent | None] = []
        for part, group in zip(parts, found, strict=True):
            if not group:
                problems.append(f"{part.label} 기록 없음 (학번 확인)")
                singles.append(None)
            elif len(group) > 1:
                problems.append(f"{part.label} 학번 중복")
                singles.append(None)
            elif group[0].total is None:
                problems.append(f"{part.label} 확인 필요 문항")
                singles.append(None)
            else:
                singles.append(group[0])
        part_totals = tuple(group[0].total if len(group) == 1 else None for group in found)
        complete = not problems
        total = (
            sum((item.total for item in singles if item and item.total is not None), Decimal(0))
            if complete
            else None
        )
        points = tuple(
            _subject_earned(subject, singles) if complete else None for subject in config
        )
        passed: bool | None = None
        if complete and subjects is not None and subjects.criteria.present:
            criteria = subjects.criteria
            assert total is not None
            passed = criteria.total_percent is None or total * _HUNDRED >= (
                criteria.total_percent * maximum
            )
            if criteria.per_subject_percent is not None:
                passed = passed and all(
                    earned is not None
                    and earned * _HUNDRED
                    >= criteria.per_subject_percent * _subject_max(subject, parts)
                    for subject, earned in zip(config, points, strict=True)
                )
        rows.append(
            _Row(
                student_id,
                names[0] if names else "",
                part_totals,
                total,
                None,
                points,
                passed,
                tuple(problems),
            )
        )
    # Teachers read by name in 가나다 order, then students without a name by student ID.
    rows.sort(key=lambda row: (not row.name, row.name, row.student_id))
    totals = [row.total for row in rows if row.total is not None]
    ranked = [
        _Row(
            row.student_id,
            row.name,
            row.part_totals,
            row.total,
            None if row.total is None else 1 + sum(other > row.total for other in totals),
            row.subject_points,
            row.passed,
            row.problems,
        )
        for row in rows
    ]
    unreadable.sort(key=lambda item: (parts.index(item[0]), item[1].part_order))
    return ranked, unreadable


def summarize_combined_scores(parts: Sequence[PartScores]) -> CombinedCounts:
    """How many students the combined report has and how many of them are complete."""
    rows, unreadable = _collect(parts, None)
    complete = sum(row.complete for row in rows)
    return CombinedCounts(len(rows), complete, len(rows) - complete, len(unreadable))


def _append(sheet: Worksheet, values: Sequence[object]) -> None:
    sheet.append([_text(item) if isinstance(item, str) else item for item in values])


def _percent_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _write_result(
    workbook: Workbook,
    parts: Sequence[PartScores],
    subjects: SubjectConfig | None,
    rows: Sequence[_Row],
) -> None:
    sheet = workbook.active
    if sheet is None:
        raise RuntimeError("new workbook must have an active worksheet")
    sheet.title = RESULT_SHEET_NAME
    config = subjects.subjects if subjects is not None else ()
    show_pass = subjects is not None and subjects.criteria.present
    headers = [
        "순번",
        "학번",
        "이름",
        *(f"{part.label} 점수" for part in parts),
        "총점",
        "통합 석차",
        *(subject.name for subject in config),
        *(("합격 여부",) if show_pass else ()),
        "비고",
    ]
    _append(sheet, headers)
    for serial, row in enumerate(rows, 1):
        note = f"확인 필요: {_NOTE_SEPARATOR.join(row.problems)}" if row.problems else ""
        verdict = None if row.passed is None else PASS_TEXT if row.passed else FAIL_TEXT
        _append(
            sheet,
            [
                serial,
                row.student_id,
                row.name,
                *row.part_totals,
                row.total,
                row.rank,
                *row.subject_points,
                *((verdict,) if show_pass else ()),
                note,
            ],
        )
        if not row.complete:
            for cell in sheet[serial + 1]:
                mark_review(cell)
    style_header_row(sheet, _FREEZE_PANES)


def _write_unreadable(
    workbook: Workbook, unreadable: Sequence[tuple[PartScores, PartStudent]]
) -> None:
    sheet = workbook.create_sheet(UNREADABLE_SHEET_NAME)
    _append(sheet, ["파트", "순번", "원본", "점수"])
    for part, student in unreadable:
        _append(sheet, [part.label, student.part_order, student.source_label, student.total])
    style_header_row(sheet, "A2")


def _write_parts(workbook: Workbook, parts: Sequence[PartScores], generated_at: str) -> None:
    sheet = workbook.create_sheet(PARTS_SHEET_NAME)
    _append(sheet, ["파트", "시험명", "폴더", "채점 시각", "학생 수", "만점"])
    for part in parts:
        _append(
            sheet,
            [
                part.label,
                part.exam_name,
                part.folder_name,
                part.graded_at or "",
                len(part.rows),
                part.maximum,
            ],
        )
    style_header_row(sheet, "A2")
    sheet.append([])
    _append(sheet, ["작성 시각", generated_at])


def _write_subjects(
    workbook: Workbook, parts: Sequence[PartScores], subjects: SubjectConfig
) -> None:
    sheet = workbook.create_sheet(SUBJECT_SHEET_NAME)
    _append(sheet, ["과목명", "범위", "만점"])
    for subject in subjects.subjects:
        ranges = ", ".join(
            f"{parts[item.part - 1].label} {item.first}~{item.last}번" for item in subject.ranges
        )
        _append(sheet, [subject.name, ranges, _subject_max(subject, parts)])
    style_header_row(sheet, "A2")
    criteria = subjects.criteria
    if criteria.present:
        sheet.append([])
        _append(sheet, ["합격기준", "기준(%)"])
        for cell in sheet[sheet.max_row]:
            cell.font = Font(name="Calibri", size=11, bold=True)
        if criteria.total_percent is not None:
            _append(sheet, ["총점", criteria.total_percent])
        if criteria.per_subject_percent is not None:
            _append(sheet, ["과목별", criteria.per_subject_percent])


def build_combined_score_book(
    parts: Sequence[PartScores], subjects: SubjectConfig | None, generated_at: str
) -> bytes:
    """Build the 합산 성적표 workbook.

    ``parts`` are in part order (파트1 first); the subject configuration's 파트 numbers count
    from 1 in that order.  Raises ``ValueError`` for inconsistent input; the caller validates
    user-facing problems first.
    """
    _validate(parts, subjects)
    rows, unreadable = _collect(parts, subjects)
    workbook = Workbook()
    _write_result(workbook, parts, subjects, rows)
    if unreadable:
        _write_unreadable(workbook, unreadable)
    _write_parts(workbook, parts, generated_at)
    if subjects is not None:
        _write_subjects(workbook, parts, subjects)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


__all__ = [
    "CombinedCounts",
    "PartScores",
    "PartStudent",
    "build_combined_score_book",
    "summarize_combined_scores",
]
