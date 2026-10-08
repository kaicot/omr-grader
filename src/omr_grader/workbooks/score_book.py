"""Formula-free score and final workbook projections.

These writers are projections of committed snapshots.  They never read a
workbook back and never use a workbook as scoring authority.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook
from openpyxl.packaging.custom import StringProperty
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.styleable import StyleableObject
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from omr_grader.application.dto import ScoreResult, ScoreSet
from omr_grader.domain.enums import KeyQuestionStatus, StudentIdStatus
from omr_grader.domain.grading import CORRECT, INCORRECT, REVIEW, question_outcomes
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    CellEvidence,
    EffectiveResponse,
)
from omr_grader.infrastructure.result_layout import (
    FINAL_KIND,
    SCORE_KIND,
    result_workbook_filename,
    scored_image_filename,
)

from .answer_key import ANSWER_KEY_HEADERS
from .response_book import (
    join_notes,
    mark_review,
    response_text_ranges,
    review_note,
    save_workbook,
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
_ITEM_SHEET_NAME = "문항 분석"
_EVIDENCE_SHEET_NAME = "판독 근거"
_ANSWER_KEY_SHEET_NAME = "정답표"
_EVERYONE = "전원"
_ITEM_HEADERS = (
    "문항",
    "정답",
    "배점",
    "응시자 수",
    "정답률(%)",
    *"①②③④⑤",
    "무응답",
    "중복",
    "확인 필요",
    "변별도",
)
_EVIDENCE_HEADERS = ("순번", "학번", "이름", *(f"Q{number}" for number in range(1, 101)))
_EVIDENCE_NOTE = "가장 진한 칸과 두 번째 칸의 차이, 작을수록 판독이 애매함"
_FREEZE_PANES = "F2"
_DANGEROUS_PREFIXES = ("=", "+", "-", "@")
# Item analysis: the top and bottom 27% of the scored students by total form the groups for
# 변별도, and fewer than 10 scored students give no meaningful groups.
_GROUP_SHARE = 0.27
_MIN_GROUP_STUDENTS = 10
_LOW_RATE_PERCENT = 30.0
_FIRST_CHOICE_COLUMN = 6
_RATE_COLUMN = _ITEM_HEADERS.index("정답률(%)") + 1
_DISCRIMINATION_COLUMN = _ITEM_HEADERS.index("변별도") + 1
# Evidence sheet: a margin under 0.10 is yellow.  A question whose darkest cell is below 0.11
# (the recognition thresholds' blank ceiling) is simply unmarked, so a tiny margin there is
# normal and stays plain.
_MARGIN_WATCH = 0.10
_NO_MARK_BELOW = 0.11
# Excel's "Bad" colors mark answers scored as wrong.  Fonts repeat openpyxl's default face and
# size so styled cells match the unstyled ones around them.
_WRONG_FILL = PatternFill(fill_type="solid", start_color="FFFFC7CE", end_color="FFFFC7CE")
_WRONG_FONT = Font(name="Calibri", size=11, color="FF9C0006")
_HEADER_FONT = Font(name="Calibri", size=11, bold=True)


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
    evidence_by_work_item: Mapping[str, Sequence[CellEvidence]] | None = None,
) -> Path:
    """Write the exact score projection to a generation-owned path.

    ``manifest_sha256`` identifies the immutable source generation, never the
    manifest that will later include this projection.  ``evidence_by_work_item`` is the
    automatic recognition evidence for the 판독 근거 sheet; imported rows have none.
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
        evidence_by_work_item=evidence_by_work_item or {},
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
    evidence_by_work_item: Mapping[str, Sequence[CellEvidence]] | None = None,
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
        evidence_by_work_item=evidence_by_work_item or {},
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
    evidence_by_work_item: Mapping[str, Sequence[CellEvidence]],
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
    ordered = student_order(responses, names_by_student_id)
    workbook = Workbook()
    chosen_sheet = workbook.active
    if chosen_sheet is None:
        raise RuntimeError("new workbook must have an active worksheet")
    chosen_sheet.title = sheet_name
    outcome_sheet = workbook.create_sheet(_OUTCOME_SHEET_NAME)
    chosen_sheet.append(list(headers))
    outcome_sheet.append(list(headers))
    outcomes_by_student: list[tuple[str, ...]] = []
    for serial, response in enumerate(ordered, 1):
        score = scores_by_work_item[response.work_item_id]
        student_id = _shown_student_id(response)
        name = names_by_student_id.get(student_id, "") if student_id else ""
        outcomes = question_outcomes(response, key)
        outcomes_by_student.append(outcomes)
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
        chosen = [_answer_cell(answer.choices) for answer in response.answers]
        chosen_sheet.append([*head, *chosen, *tail])
        outcome_sheet.append([*head, *(_display_text(value) for value in outcomes), *tail])
        _mark_outcomes(chosen_sheet, serial + 1, outcomes)
        _mark_outcomes(outcome_sheet, serial + 1, outcomes)
    style_header_row(chosen_sheet, _FREEZE_PANES)
    style_header_row(outcome_sheet, _FREEZE_PANES)
    _write_item_analysis(workbook, ordered, outcomes_by_student, key, scores_by_work_item)
    # The raw responses in the importable 응답원본 layout, so this one book can also start a
    # new exam through '응답 엑셀로 시작'.
    write_effective_response_sheet(
        workbook.create_sheet(RESPONSE_SHEET_NAME),
        ordered,
        names_by_student_id,
        review_notes=True,
    )
    _write_evidence(workbook, ordered, names_by_student_id, evidence_by_work_item)
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
        save_workbook(workbook, temporary, _text_ranges(workbook, sheet_name))
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


_TOTAL_COLUMN = _SCORE_HEADERS.index("총점") + 1
_FIRST_QUESTION_COLUMN = _SCORE_HEADERS.index("Q1") + 1


def student_order(
    responses: Sequence[EffectiveResponse], names_by_student_id: Mapping[str, str]
) -> tuple[EffectiveResponse, ...]:
    """The responses in the order the books list them (순번 1, 2, ...).

    Teachers read the book by name: 가나다 order, then students without a roster name by
    student ID.  Hangul syllables sort in 가나다 order by code point.  Everything that
    carries a 순번 uses this one function so the artifacts cannot disagree.
    """
    ranked = sorted(
        enumerate(responses),
        key=lambda item: _name_order(item[0], item[1], names_by_student_id),
    )
    return tuple(response for _, response in ranked)


def scored_image_names(
    responses: Sequence[EffectiveResponse], names_by_student_id: Mapping[str, str]
) -> dict[str, str]:
    """Scored page image file name (``<순번>_<학번>_<이름>.jpg``) by work item ID."""
    names: dict[str, str] = {}
    for serial, response in enumerate(student_order(responses, names_by_student_id), 1):
        student_id = _shown_student_id(response)
        name = names_by_student_id.get(student_id, "") if student_id else ""
        names[response.work_item_id] = scored_image_filename(serial, student_id, name)
    return names


def answer_margins(evidence: Iterable[CellEvidence]) -> dict[int, tuple[float, float]]:
    """Per question, the darkest choice fill and its lead over the second darkest choice.

    Questions with fewer than two measured choices are left out.
    """
    fills: dict[int, list[float]] = {}
    for cell in evidence:
        if cell.question is not None and cell.fill_score is not None:
            fills.setdefault(cell.question, []).append(float(cell.fill_score))
    margins: dict[int, tuple[float, float]] = {}
    for question, values in fills.items():
        ranked = sorted(values, reverse=True)
        if len(ranked) >= 2:
            margins[question] = (ranked[0], ranked[0] - ranked[1])
    return margins


def _answer_cell(choices: Sequence[int]) -> int | str | None:
    """One choice as a number, several as text such as "1,3", none as an empty cell."""
    if not choices:
        return None
    if len(choices) == 1:
        return choices[0]
    return ",".join(str(choice) for choice in choices)


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


def _text_ranges(workbook: Workbook, score_sheet_name: str) -> dict[str, tuple[str, ...]]:
    """Ranges that hold text on purpose (IDs, answers), so Excel flags no 'number as text'."""

    def last_row(title: str) -> int:
        return int(workbook[title].max_row)

    def column(letter: str, title: str) -> tuple[str, ...]:
        return (f"{letter}2:{letter}{last_row(title)}",) if last_row(title) >= 2 else ()

    last_choice = get_column_letter(_FIRST_CHOICE_COLUMN + 99)
    answers = (
        (f"F2:{last_choice}{last_row(score_sheet_name)}",)
        if last_row(score_sheet_name) >= 2
        else ()
    )
    return {
        score_sheet_name: (*column("B", score_sheet_name), *answers),
        _OUTCOME_SHEET_NAME: column("B", _OUTCOME_SHEET_NAME),
        _ITEM_SHEET_NAME: column("B", _ITEM_SHEET_NAME),
        RESPONSE_SHEET_NAME: response_text_ranges(last_row(RESPONSE_SHEET_NAME)),
        _EVIDENCE_SHEET_NAME: column("B", _EVIDENCE_SHEET_NAME),
        _ANSWER_KEY_SHEET_NAME: column("B", _ANSWER_KEY_SHEET_NAME),
    }


def _write_answer_key(workbook: Workbook, key: AnswerKeySnapshot) -> None:
    """The answer key this book was graded with, as its own sheet."""
    sheet = workbook.create_sheet(_ANSWER_KEY_SHEET_NAME)
    sheet.append(list(ANSWER_KEY_HEADERS))
    for entry in key.entries:
        sheet.append([entry.question, _key_answer_cell(entry), Decimal(entry.points)])
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
    sheet.freeze_panes = "A2"


def _key_answer_cell(entry: AnswerKeyEntry) -> int | str | None:
    """The key's answer as this book shows it: a number, "1,3", or 전원 for everyone-correct."""
    if entry.status is KeyQuestionStatus.UNASKED:
        return ""
    if entry.status is KeyQuestionStatus.ALL:
        return _EVERYONE
    return _answer_cell(entry.answer.choices)


def _write_item_analysis(
    workbook: Workbook,
    ordered: Sequence[EffectiveResponse],
    outcomes_by_student: Sequence[tuple[str, ...]],
    key: AnswerKeySnapshot,
    scores_by_work_item: Mapping[str, ScoreResult],
) -> None:
    """One row per asked question: correct rate, choice counts and discrimination."""
    sheet = workbook.create_sheet(_ITEM_SHEET_NAME)
    sheet.append(list(_ITEM_HEADERS))
    groups = _discrimination_groups(ordered, scores_by_work_item)
    for index, entry in enumerate(key.entries):
        if entry.status is KeyQuestionStatus.UNASKED:
            continue
        outcomes = [student[index] for student in outcomes_by_student]
        choices = [response.answers[index].choices for response in ordered]
        values = _item_row(entry, outcomes, choices, groups)
        sheet.append(values)
        _mark_item_row(sheet, sheet.max_row, entry, values, outcomes.count(CORRECT))
        sheet.cell(sheet.max_row, _RATE_COLUMN).number_format = "0.0"
        sheet.cell(sheet.max_row, _DISCRIMINATION_COLUMN).number_format = "0.00"
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
    sheet.freeze_panes = "A2"


def _discrimination_groups(
    ordered: Sequence[EffectiveResponse], scores_by_work_item: Mapping[str, ScoreResult]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Positions of the top and bottom 27% by total; empty with fewer than 10 scored students."""
    scored = [
        (position, score)
        for position, response in enumerate(ordered)
        if (score := scores_by_work_item[response.work_item_id].score) is not None
    ]
    if len(scored) < _MIN_GROUP_STUDENTS:
        return (), ()
    size = int(len(scored) * _GROUP_SHARE + 0.5)
    ranked = [position for position, _ in sorted(scored, key=lambda item: item[1], reverse=True)]
    return tuple(ranked[:size]), tuple(ranked[-size:])


def _correct_rate(outcomes: Sequence[str], positions: Iterable[int]) -> float:
    chosen = tuple(positions)
    return sum(outcomes[position] == CORRECT for position in chosen) / len(chosen)


def _item_row(
    entry: AnswerKeyEntry,
    outcomes: Sequence[str],
    choices: Sequence[tuple[int, ...]],
    groups: tuple[tuple[int, ...], tuple[int, ...]],
) -> list[object]:
    """Values of one 문항 분석 row.

    Unconfirmed answers are left out of the head count.  중복 counts several marks scored as
    wrong; several marks that match a multi-answer key are correct and count only in 정답률.
    """
    scored = [position for position, outcome in enumerate(outcomes) if outcome != REVIEW]
    correct = sum(outcomes[position] == CORRECT for position in scored)
    upper, lower = groups
    discrimination = (
        round(_correct_rate(outcomes, upper) - _correct_rate(outcomes, lower), 2) if upper else None
    )
    return [
        entry.question,
        _key_answer_cell(entry),
        Decimal(entry.points),
        len(scored),
        round(correct / len(scored) * 100, 1) if scored else None,
        *(_choice_count(choices, scored, number) for number in range(1, 6)),
        sum(not choices[position] for position in scored),
        sum(len(choices[position]) > 1 and outcomes[position] != CORRECT for position in scored),
        sum(outcome == REVIEW for outcome in outcomes),
        discrimination,
    ]


def _choice_count(choices: Sequence[tuple[int, ...]], scored: Sequence[int], number: int) -> int:
    """Students who marked only this choice."""
    return sum(choices[position] == (number,) for position in scored)


def _mark_item_row(
    sheet: Worksheet, row: int, entry: AnswerKeyEntry, values: Sequence[object], correct: int
) -> None:
    """Yellow for a correct rate under 30%, a wrong choice picked more than the answer, 변별도 <= 0.

    Everyone-correct questions are never marked.
    """
    if entry.status is KeyQuestionStatus.ALL:
        return
    rate = values[_RATE_COLUMN - 1]
    if isinstance(rate, float) and rate < _LOW_RATE_PERCENT:
        mark_review(sheet.cell(row, _RATE_COLUMN))
    for number in range(1, 6):
        count = values[_RATE_COLUMN - 1 + number]
        if number not in entry.answer.choices and cast(int, count) > correct:
            mark_review(sheet.cell(row, _RATE_COLUMN + number))
    discrimination = values[_DISCRIMINATION_COLUMN - 1]
    # A question everyone answered right separates nobody; its 0 is not a warning sign.
    all_right = isinstance(rate, float) and rate >= 100
    if isinstance(discrimination, float) and (
        discrimination < 0 or (discrimination == 0 and not all_right)
    ):
        mark_review(sheet.cell(row, _DISCRIMINATION_COLUMN))


def _write_evidence(
    workbook: Workbook,
    ordered: Sequence[EffectiveResponse],
    names_by_student_id: Mapping[str, str],
    evidence_by_work_item: Mapping[str, Sequence[CellEvidence]],
) -> None:
    """Per student and question, how far the darkest choice leads the second darkest."""
    sheet = workbook.create_sheet(_EVIDENCE_SHEET_NAME)
    sheet.append([*_EVIDENCE_HEADERS, _EVIDENCE_NOTE])
    for serial, response in enumerate(ordered, 1):
        student_id = _shown_student_id(response)
        name = names_by_student_id.get(student_id, "") if student_id else ""
        margins = answer_margins(evidence_by_work_item.get(response.work_item_id, ()))
        row: list[object] = [serial, _display_text(student_id), _display_text(name)]
        row.extend(
            None if question not in margins else round(margins[question][1], 2)
            for question in range(1, 101)
        )
        sheet.append(row)
        for question, (darkest, lead) in margins.items():
            cell = sheet.cell(serial + 1, 3 + question)
            cell.number_format = "0.00"
            if darkest >= _NO_MARK_BELOW and round(lead, 2) < _MARGIN_WATCH:
                mark_review(cell)
    style_header_row(sheet, "D2")


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
    """Explain the colors and special cells on a small extra sheet."""
    sheet = workbook.create_sheet("색 설명")
    sheet.append(["분홍", "오답, 무응답, 중복 표기 (틀린 것으로 채점)"])
    sheet.append(
        [
            "노랑",
            "확인 필요: 인식이 애매해 검토 전까지 채점하지 않음 (총점도 노랑, 비고에 문항 번호)",
        ]
    )
    sheet.append(["색 없음", "정답, 또는 정답표에 없는 문항"])
    sheet.append(
        [
            "노랑 (문항 분석)",
            "정답률 30% 미만, 정답보다 많이 고른 오답 보기, 변별도 0 이하(모두 맞힌 문항 제외)."
            " 변별도 = 총점 상위 27% 정답률 - 하위 27% 정답률 (확인 필요가 있는 학생 제외, 응시자 10명 미만이면 비움)",
        ]
    )
    sheet.append(["노랑 (판독 근거)", f"{_EVIDENCE_NOTE}. 0.10 미만이면 노랑, 학번·답을 직접 확인"])
    sheet.append(["전원", "정답표의 '전원' = 모든 학생 정답 처리"])
    _mark_wrong(sheet.cell(1, 1))
    for row in (2, 4, 5):
        mark_review(sheet.cell(row, 1))
    sheet.column_dimensions["A"].width = 18
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


__all__ = [
    "answer_margins",
    "final_filename",
    "score_filename",
    "scored_image_names",
    "student_order",
    "write_final_book",
    "write_score_book",
]
