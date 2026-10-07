"""Formula-free score and final workbook projections.

These writers are projections of committed snapshots.  They never read a
workbook back and never use a workbook as scoring authority.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook
from openpyxl.packaging.custom import StringProperty
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.styleable import StyleableObject
from openpyxl.worksheet.worksheet import Worksheet

from omr_grader.application.dto import ScoreSet
from omr_grader.domain.enums import StudentIdStatus
from omr_grader.domain.grading import INCORRECT, REVIEW, question_outcomes
from omr_grader.domain.models import AnswerKeySnapshot, EffectiveResponse
from omr_grader.infrastructure.result_layout import (
    FINAL_KIND,
    SCORE_KIND,
    result_workbook_filename,
)

from .answer_key import ANSWER_KEY_HEADERS, answer_key_rows
from .response_book import (
    join_notes,
    mark_review,
    review_note,
    style_header_row,
    write_effective_response_sheet,
)
from .schemas import RESPONSE_SHEET_NAME

_SCORE_HEADERS = (
    "순번",
    "학번",
    "이름",
    "총점",
    "석차",
    *(f"Q{number}" for number in range(1, 101)),
    "비고",
)
_FINAL_HEADERS = (*_SCORE_HEADERS, "수정여부", "수정문항", "확정일시")
_OUTCOME_SHEET_NAME = "결과OX"
_FREEZE_PANES = "F2"
_DANGEROUS_PREFIXES = ("=", "+", "-", "@")
# Excel's "Bad" colors mark answers scored as wrong.  Fonts repeat openpyxl's default face and
# size so styled cells match the unstyled ones around them.
_WRONG_FILL = PatternFill(fill_type="solid", start_color="FFFFC7CE", end_color="FFFFC7CE")
_WRONG_FONT = Font(name="Calibri", size=11, color="FF9C0006")


def score_filename(exam_name: str, committed_at: str) -> str:
    return result_workbook_filename(SCORE_KIND, exam_name, committed_at)


def final_filename(exam_name: str, committed_at: str) -> str:
    return result_workbook_filename(FINAL_KIND, exam_name, committed_at)


def write_score_book(
    destination: str | Path,
    *,
    exam_name: str,
    committed_at: str,
    session_id: str,
    revision: int,
    manifest_sha256: str,
    responses: Sequence[EffectiveResponse],
    key: AnswerKeySnapshot,
    scores: ScoreSet,
    names_by_student_id: Mapping[str, str] | None = None,
) -> Path:
    """Write the exact score projection to a generation-owned path.

    ``manifest_sha256`` identifies the immutable source generation, never the
    manifest that will later include this projection.
    """
    return _write(
        destination,
        filename=score_filename(exam_name, committed_at),
        sheet_name=SCORE_KIND,
        headers=_SCORE_HEADERS,
        session_id=session_id,
        revision=revision,
        manifest_sha256=manifest_sha256,
        responses=responses,
        key=key,
        scores=scores,
        names_by_student_id=names_by_student_id or {},
        finalized_at=None,
    )


def write_final_book(
    destination: str | Path,
    *,
    exam_name: str,
    committed_at: str,
    session_id: str,
    revision: int,
    manifest_sha256: str,
    responses: Sequence[EffectiveResponse],
    key: AnswerKeySnapshot,
    scores: ScoreSet,
    names_by_student_id: Mapping[str, str] | None = None,
) -> Path:
    """Write the exact final projection to a generation-owned path.

    ``manifest_sha256`` identifies the immutable source generation, never the
    manifest that will later include this projection.
    """
    return _write(
        destination,
        filename=final_filename(exam_name, committed_at),
        sheet_name=FINAL_KIND,
        headers=_FINAL_HEADERS,
        session_id=session_id,
        revision=revision,
        manifest_sha256=manifest_sha256,
        responses=responses,
        key=key,
        scores=scores,
        names_by_student_id=names_by_student_id or {},
        finalized_at=committed_at,
    )


def _write(
    destination: str | Path,
    *,
    filename: str,
    sheet_name: str,
    headers: tuple[str, ...],
    session_id: str,
    revision: int,
    manifest_sha256: str,
    responses: Sequence[EffectiveResponse],
    key: AnswerKeySnapshot,
    scores: ScoreSet,
    names_by_student_id: Mapping[str, str],
    finalized_at: str | None,
) -> Path:
    if not isinstance(key, AnswerKeySnapshot) or not isinstance(scores, ScoreSet):
        raise TypeError("key and scores must be committed snapshots")
    if not all(isinstance(response, EffectiveResponse) for response in responses):
        raise TypeError("responses must be effective response snapshots")
    work_item_ids = tuple(response.work_item_id for response in responses)
    if len(set(work_item_ids)) != len(work_item_ids):
        raise ValueError("responses must have unique work_item_id values")
    if len(responses) != len(scores.rows):
        raise ValueError("responses and scores must contain one row per work item")
    scores_by_work_item = {row.work_item_id: row for row in scores.rows}
    if len(scores_by_work_item) != len(responses) or set(scores_by_work_item) != set(work_item_ids):
        raise ValueError("score rows must exactly match response work item IDs")

    duplicate_ids = {
        response.student_id
        for response in responses
        if response.student_id is not None
        and sum(item.student_id == response.student_id for item in responses) > 1
    }
    # Teachers read the book by name: 가나다 order, then students without a roster name
    # by student ID. Hangul syllables sort in 가나다 order by code point.
    ordered = [
        response
        for _, response in sorted(
            enumerate(responses),
            key=lambda item: _name_order(item[0], item[1], names_by_student_id),
        )
    ]
    workbook = Workbook()
    chosen_sheet = workbook.active
    if chosen_sheet is None:
        raise RuntimeError("new workbook must have an active worksheet")
    chosen_sheet.title = sheet_name
    outcome_sheet = workbook.create_sheet(_OUTCOME_SHEET_NAME)
    chosen_sheet.append(list(headers))
    outcome_sheet.append(list(headers))
    for serial, response in enumerate(ordered, 1):
        score = scores_by_work_item[response.work_item_id]
        student_id = _shown_student_id(response)
        name = names_by_student_id.get(student_id, "") if student_id else ""
        outcomes = question_outcomes(response, key)
        note = join_notes(
            "중복확인필요" if response.student_id in duplicate_ids else "",
            review_note(number for number, outcome in enumerate(outcomes, 1) if outcome == REVIEW),
        )
        head: list[object] = [
            serial,
            _display_text(student_id),
            _display_text(name),
            score.score,
            score.rank,
        ]
        tail: list[object] = [_display_text(note)]
        if finalized_at is not None:
            corrected = bool(response.corrected_targets)
            targets = ",".join(_correction_label(target) for target in response.corrected_targets)
            tail.extend((corrected, _display_text(targets), _display_text(finalized_at)))
        # 채점결과 shows what the student marked; 결과OX shows how each answer was scored.
        chosen = [",".join(str(choice) for choice in answer.choices) for answer in response.answers]
        chosen_sheet.append([*head, *chosen, *tail])
        outcome_sheet.append([*head, *(_display_text(value) for value in outcomes), *tail])
        _mark_outcomes(chosen_sheet, serial + 1, outcomes)
        _mark_outcomes(outcome_sheet, serial + 1, outcomes)
    style_header_row(chosen_sheet, _FREEZE_PANES)
    style_header_row(outcome_sheet, _FREEZE_PANES)
    # The raw responses in the importable 응답원본 layout, so this one book can also start a
    # new exam through '응답 엑셀로 시작'.
    write_effective_response_sheet(
        workbook.create_sheet(RESPONSE_SHEET_NAME),
        ordered,
        names_by_student_id,
        review_notes=True,
    )
    _write_answer_key(workbook, key)
    _write_legend(workbook)
    _set_provenance(workbook, session_id, revision, manifest_sha256)
    target = Path(destination) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook.save(temporary)
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


_TOTAL_COLUMN = _SCORE_HEADERS.index("총점") + 1
_FIRST_QUESTION_COLUMN = _SCORE_HEADERS.index("Q1") + 1


def _shown_student_id(response: EffectiveResponse) -> str:
    """The student ID as the book shows it: blank unless it was read cleanly."""
    if response.student_id is None or response.student_id_status not in (
        StudentIdStatus.NORMAL,
        StudentIdStatus.DUPLICATE,
    ):
        return ""
    return response.student_id


def _name_order(
    position: int, response: EffectiveResponse, names_by_student_id: Mapping[str, str]
) -> tuple[bool, str, bool, str, int]:
    student_id = _shown_student_id(response)
    name = names_by_student_id.get(student_id, "") if student_id else ""
    return (not name, name, not student_id, student_id, position)


def _write_answer_key(workbook: Workbook, key: AnswerKeySnapshot) -> None:
    """The answer key this book was graded with, as its own sheet."""
    sheet = workbook.create_sheet("정답표")
    sheet.append(list(ANSWER_KEY_HEADERS))
    for row in answer_key_rows(key):
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(name="Calibri", size=11, bold=True)
    sheet.freeze_panes = "A2"


def _mark_wrong(cell: StyleableObject) -> None:
    cell.fill = _WRONG_FILL
    cell.font = _WRONG_FONT


def _mark_outcomes(sheet: Worksheet, row: int, outcomes: Sequence[str]) -> None:
    """Pink for answers scored as wrong, yellow for answers waiting for a check.

    Correct and unasked questions stay plain.  A student with an unconfirmed answer has no
    total yet, so the total is yellow as well.
    """
    for column, outcome in enumerate(outcomes, _FIRST_QUESTION_COLUMN):
        if outcome == INCORRECT:
            _mark_wrong(sheet.cell(row, column))
        elif outcome == REVIEW:
            mark_review(sheet.cell(row, column))
    if REVIEW in outcomes:
        mark_review(sheet.cell(row, _TOTAL_COLUMN))


def _write_legend(workbook: Workbook) -> None:
    """Explain the colors on a small extra sheet."""
    sheet = workbook.create_sheet("색 설명")
    sheet.append(["분홍", "오답, 무응답, 중복 표기 (틀린 것으로 채점)"])
    sheet.append(
        [
            "노랑",
            "확인 필요: 인식이 애매해 검토 전까지 채점하지 않음 (총점도 노랑, 비고에 문항 번호)",
        ]
    )
    sheet.append(["색 없음", "정답, 또는 정답표에 없는 문항"])
    _mark_wrong(sheet.cell(1, 1))
    mark_review(sheet.cell(2, 1))
    sheet.column_dimensions["A"].width = 10
    sheet.column_dimensions["B"].width = 90


def _set_provenance(
    workbook: Workbook, session_id: str, revision: int, manifest_sha256: str
) -> None:
    properties = cast(Any, workbook).custom_doc_props
    properties.append(StringProperty(name="schema", value="1"))
    properties.append(StringProperty(name="session_id", value=session_id))
    properties.append(StringProperty(name="revision", value=str(revision)))
    properties.append(StringProperty(name="manifest_sha256", value=manifest_sha256))


def _display_text(value: str) -> str:
    return f"'{value}" if value.startswith(_DANGEROUS_PREFIXES) else value


def _correction_label(target: str) -> str:
    kind, number = target.split(":", 1)
    return f"학번{int(number) + 1}" if kind == "id_cell" else f"Q{int(number)}"


__all__ = ["final_filename", "score_filename", "write_final_book", "write_score_book"]
