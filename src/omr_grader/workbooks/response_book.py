"""Deterministic response-result workbook projection; never an authority reader."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from itertools import groupby
from pathlib import Path
from typing import Any, cast
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.packaging.custom import StringProperty
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.styleable import StyleableObject
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from omr_grader.domain.enums import AnswerStatus
from omr_grader.domain.models import AnswerValue, EffectiveResponse, ImportedResponseRef

from .schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME, escape_formula_text

# Excel's "Neutral" colors mark answers that a person still has to confirm.  Fonts repeat
# openpyxl's default face and size so styled cells match the unstyled ones around them.
_REVIEW_FILL = PatternFill(fill_type="solid", start_color="FFFFEB9C", end_color="FFFFEB9C")
_REVIEW_FONT = Font(name="Calibri", size=11, color="FF9C5700")
_HEADER_FONT = Font(name="Calibri", size=11, bold=True)
_NOTE_SEPARATOR = " / "
_MIN_RANGE_LENGTH = 3
_FIRST_ANSWER_COLUMN = 5


def _text(cell: Cell, value: str) -> None:
    cell.value = escape_formula_text(value)
    cell.data_type = "s"


def _cell(sheet: Worksheet, row: int, column: int) -> Cell:
    return cast(Cell, sheet.cell(row, column))


def response_text_ranges(last_row: int) -> tuple[str, ...]:
    """Cell ranges of a 응답원본 sheet that hold text on purpose: 학번 and every answer."""
    if last_row < 2:
        return ()
    last_answer = get_column_letter(_FIRST_ANSWER_COLUMN + 99)
    return (f"C2:C{last_row}", f"E2:{last_answer}{last_row}")


def save_workbook(
    workbook: Workbook, destination: str | Path, text_ranges: Mapping[str, Sequence[str]]
) -> None:
    """Save ``workbook``, telling Excel the listed ranges are text on purpose.

    openpyxl cannot write ``ignoredErrors``, so the saved package is patched: each sheet in
    ``text_ranges`` (title to ranges) gets its ``numberStoredAsText`` warning switched off,
    which keeps IDs and answers such as "12" free of green corner marks.
    """
    ranges = {
        f"xl/worksheets/sheet{workbook.sheetnames.index(title) + 1}.xml": tuple(cells)
        for title, cells in text_ranges.items()
        if cells
    }
    if not ranges:
        workbook.save(destination)
        return
    buffer = BytesIO()
    workbook.save(buffer)
    with ZipFile(buffer) as source, ZipFile(destination, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename in ranges:
                payload = _with_ignored_errors(payload, ranges[item.filename])
            target.writestr(item, payload)


def _with_ignored_errors(sheet_xml: bytes, cells: Sequence[str]) -> bytes:
    closing = b"</worksheet>"
    if not sheet_xml.endswith(closing) or any(
        tag in sheet_xml for tag in (b"<drawing", b"<legacyDrawing", b"<tableParts", b"<extLst")
    ):
        raise ValueError("worksheet XML has an unexpected layout")
    element = f'<ignoredErrors><ignoredError sqref="{" ".join(cells)}" numberStoredAsText="1"/>'
    return sheet_xml[: -len(closing)] + element.encode() + b"</ignoredErrors>" + closing


def mark_review(cell: StyleableObject) -> None:
    """Paint a cell yellow: a person still has to look at it before it counts."""
    cell.fill = _REVIEW_FILL
    cell.font = _REVIEW_FONT


def style_header_row(sheet: Worksheet, freeze_panes: str = "E2") -> None:
    """Bold the header and freeze it with the identity columns (E2 by default) for wide sheets."""
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
    sheet.freeze_panes = freeze_panes


def join_notes(*notes: str) -> str:
    """Join the non-empty notes for the 비고 column."""
    return _NOTE_SEPARATOR.join(note for note in notes if note)


def review_note(questions: Iterable[int]) -> str:
    """Name the questions that await a check, such as '확인 필요: 3~5, 17번' ('' when none)."""
    numbers = sorted(set(questions))
    spans: list[str] = []
    for _, group in groupby(enumerate(numbers), key=lambda pair: pair[1] - pair[0]):
        run = [number for _, number in group]
        # Short runs stay as plain numbers; only three or more become a range.
        spans.extend((f"{run[0]}~{run[-1]}",) if len(run) >= _MIN_RANGE_LENGTH else map(str, run))
    return f"확인 필요: {', '.join(spans)}번" if spans else ""


def recognition_note(answers: Sequence[AnswerValue], *, student_id_valid: bool) -> str:
    """비고 text for a recognized row: unconfirmed answers first, then an unusable student ID."""
    return join_notes(
        review_note(_uncertain_questions(answers)),
        "" if student_id_valid else "학번 확인 필요",
    )


def _uncertain_questions(answers: Sequence[AnswerValue]) -> tuple[int, ...]:
    return tuple(
        question
        for question, answer in enumerate(answers, 1)
        if answer.status is AnswerStatus.UNCERTAIN
    )


def _write_answers(sheet: Worksheet, row: int, answers: Sequence[AnswerValue]) -> None:
    """Write Q1..Q100 as text and paint the answers that are not confirmed yet."""
    for column, answer in enumerate(answers, 5):
        cell = _cell(sheet, row, column)
        _text(cell, "".join(str(choice) for choice in answer.choices))
        if answer.status is AnswerStatus.UNCERTAIN:
            mark_review(cell)


def write_response_projection(
    destination: Path,
    rows: tuple[ImportedResponseRef, ...],
    *,
    session_id: str,
    revision: int,
    manifest_sha256: str,
) -> None:
    """Write the exact A1:DA response projection from immutable imported truth."""
    workbook = Workbook(write_only=False)
    sheet = workbook.active
    if not isinstance(sheet, Worksheet):
        raise RuntimeError("new workbook must have an active worksheet")
    sheet.title = RESPONSE_SHEET_NAME
    for column, header in enumerate(RESPONSE_HEADERS, 1):
        _text(_cell(sheet, 1, column), header)
    for output_row, row in enumerate(
        sorted(rows, key=lambda item: (item.input_ordinal, item.work_item_id)), 2
    ):
        _cell(sheet, output_row, 1).value = row.serial
        _text(_cell(sheet, output_row, 2), row.source_filename)
        _text(_cell(sheet, output_row, 3), row.raw_student_id)
        _text(_cell(sheet, output_row, 4), row.name)
        _write_answers(sheet, output_row, row.answers)
        _text(_cell(sheet, output_row, 105), row.note)
    style_header_row(sheet)
    workbook.properties.creator = "OMR Grader"
    custom_doc_props = cast(Any, workbook).custom_doc_props
    custom_doc_props.append(StringProperty(name="schema", value="1"))
    custom_doc_props.append(StringProperty(name="session_id", value=session_id))
    custom_doc_props.append(StringProperty(name="revision", value=str(revision)))
    custom_doc_props.append(StringProperty(name="manifest_sha256", value=manifest_sha256))
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        save_workbook(
            workbook,
            temporary_path,
            {RESPONSE_SHEET_NAME: response_text_ranges(len(rows) + 1)},
        )
        os.replace(temporary_path, destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def write_effective_response_sheet(
    sheet: Worksheet,
    rows: Sequence[EffectiveResponse],
    names_by_student_id: Mapping[str, str],
    *,
    review_notes: bool,
) -> None:
    """Fill ``sheet`` in the importable 응답원본 layout from effective responses.

    With ``review_notes`` the 비고 also names unconfirmed answers, so a book a teacher
    re-imports is held back until those yellow cells have been checked.
    """
    for column, header in enumerate(RESPONSE_HEADERS, 1):
        _text(_cell(sheet, 1, column), header)
    for serial, row in enumerate(rows, 1):
        output_row = serial + 1
        _cell(sheet, output_row, 1).value = serial
        _text(_cell(sheet, output_row, 2), row.source_label)
        _text(_cell(sheet, output_row, 3), row.student_id or "")
        _text(
            _cell(sheet, output_row, 4),
            names_by_student_id.get(row.student_id, "") if row.student_id is not None else "",
        )
        _write_answers(sheet, output_row, row.answers)
        _text(
            _cell(sheet, output_row, 105),
            join_notes(
                review_note(_uncertain_questions(row.answers)) if review_notes else "",
                "수동 수정 반영" if row.corrected_targets else "",
            ),
        )
    style_header_row(sheet)


def write_effective_response_projection(
    destination: Path,
    rows: tuple[EffectiveResponse, ...],
    *,
    session_id: str,
    revision: int,
    manifest_sha256: str,
    names_by_student_id: dict[str, str],
) -> None:
    """Write corrected effective responses as the durable regrade input workbook."""
    workbook = Workbook(write_only=False)
    sheet = workbook.active
    if not isinstance(sheet, Worksheet):
        raise RuntimeError("new workbook must have an active worksheet")
    sheet.title = RESPONSE_SHEET_NAME
    write_effective_response_sheet(sheet, rows, names_by_student_id, review_notes=False)
    workbook.properties.creator = "OMR Grader"
    custom_doc_props = cast(Any, workbook).custom_doc_props
    custom_doc_props.append(StringProperty(name="schema", value="1"))
    custom_doc_props.append(StringProperty(name="session_id", value=session_id))
    custom_doc_props.append(StringProperty(name="revision", value=str(revision)))
    custom_doc_props.append(StringProperty(name="manifest_sha256", value=manifest_sha256))
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_workbook(workbook, destination, {RESPONSE_SHEET_NAME: response_text_ranges(len(rows) + 1)})


__all__ = [
    "join_notes",
    "mark_review",
    "recognition_note",
    "response_text_ranges",
    "review_note",
    "save_workbook",
    "style_header_row",
    "write_effective_response_sheet",
    "write_effective_response_projection",
    "write_response_projection",
]
