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

from omr_grader.application.dto import ScoreSet
from omr_grader.domain.enums import StudentIdStatus
from omr_grader.domain.grading import question_outcomes
from omr_grader.domain.models import AnswerKeySnapshot, EffectiveResponse
from omr_grader.infrastructure.result_layout import result_base_name

_SCORE_HEADERS = (
    "석차",
    "학번",
    "이름",
    "총점",
    *(f"Q{number}" for number in range(1, 101)),
    "비고",
)
_FINAL_HEADERS = (*_SCORE_HEADERS, "수정여부", "수정문항", "확정일시")
_DANGEROUS_PREFIXES = ("=", "+", "-", "@")
def score_filename(exam_name: str, committed_at: str) -> str:
    return f"02_score_{result_base_name(exam_name, committed_at)}_채점결과.xlsx"


def final_filename(exam_name: str, committed_at: str) -> str:
    return (
        f"03_final_{result_base_name(exam_name, committed_at)}_최종성적표.xlsx"
    )


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
    """Write the exact A:DA score projection to a generation-owned path.

    ``manifest_sha256`` identifies the immutable source generation, never the
    manifest that will later include this projection.
    """
    return _write(
        destination,
        filename=score_filename(exam_name, committed_at),
        sheet_name="채점결과",
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
    """Write the exact A:DD final projection to a generation-owned path.

    ``manifest_sha256`` identifies the immutable source generation, never the
    manifest that will later include this projection.
    """
    return _write(
        destination,
        filename=final_filename(exam_name, committed_at),
        sheet_name="최종성적표",
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
    ordered = sorted(
        enumerate(responses),
        key=lambda item: (
            scores_by_work_item[item[1].work_item_id].rank is None,
            scores_by_work_item[item[1].work_item_id].rank or 0,
            item[0],
            item[1].work_item_id,
        ),
    )
    workbook = Workbook()
    worksheet = workbook.active
    if worksheet is None:
        raise RuntimeError("new workbook must have an active worksheet")
    worksheet.title = sheet_name
    worksheet.append(list(headers))
    response_sheet = workbook.create_sheet("응답내역")
    response_sheet.append(list(headers))
    for _, response in ordered:
        score = scores_by_work_item[response.work_item_id]
        student_id = response.student_id
        if (
            response.student_id_status not in (StudentIdStatus.NORMAL, StudentIdStatus.DUPLICATE)
            or student_id is None
        ):
            student_id = ""
        name = names_by_student_id.get(student_id, "") if student_id else ""
        note = "중복확인필요" if response.student_id in duplicate_ids else ""
        row: list[object] = [
            score.rank,
            _display_text(student_id),
            _display_text(name),
            score.score,
        ]
        row.extend(_display_text(value) for value in question_outcomes(response, key))
        row.append(_display_text(note))
        if finalized_at is not None:
            corrected = bool(response.corrected_targets)
            targets = ",".join(_correction_label(target) for target in response.corrected_targets)
            row.extend((corrected, _display_text(targets), _display_text(finalized_at)))
        worksheet.append(row)
        response_row: list[object] = [
            score.rank,
            _display_text(student_id),
            _display_text(name),
            score.score,
        ]
        response_row.extend(
            _display_text(",".join(str(choice) for choice in answer.choices))
            for answer in response.answers
        )
        response_row.append(_display_text(note))
        if finalized_at is not None:
            corrected = bool(response.corrected_targets)
            targets = ",".join(_correction_label(target) for target in response.corrected_targets)
            response_row.extend((corrected, _display_text(targets), _display_text(finalized_at)))
        response_sheet.append(response_row)
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
