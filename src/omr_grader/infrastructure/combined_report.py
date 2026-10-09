"""Read graded sessions and write the combined (합산) score report.

Every part is read from the CURRENT committed generation of its session through the same
snapshot readers grading uses, and its points come from ``domain.grading`` exactly as the
per-exam score book gets them.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from omr_grader.application.dto import SnapshotRequest
from omr_grader.domain.enums import KeyQuestionStatus, SessionState, SnapshotPurpose
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.grading import CORRECT, REVIEW, question_outcomes
from omr_grader.domain.models import RosterSnapshot, SessionRecord
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.result_layout import result_base_name
from omr_grader.infrastructure.session_store import SessionCommitCoordinator
from omr_grader.workbooks.combined_score_book import (
    PartScores,
    PartStudent,
    build_combined_score_book,
    summarize_combined_scores,
)
from omr_grader.workbooks.subject_config import (
    SubjectConfig,
    parse_subject_config,
    subject_config_sample_bytes,
)


@dataclass(frozen=True, slots=True)
class CombinedReportSummary:
    path: str
    students: int
    complete: int
    needs_review: int
    unreadable: int


def _fail(reason: str, field: str | None = None) -> Err:
    return Err(
        (
            ErrorInfo(
                "COMBINED_REPORT_FAILED",
                "error.combined_report_failed",
                field,
                context={"reason": reason},
            ),
        )
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("JSON object is invalid")
    return value


def _read_session(
    coordinator: SessionCommitCoordinator, session_id: str
) -> Result[tuple[SessionRecord, RosterSnapshot, int]]:
    """The current revision's session record and roster, read from its pinned generation."""
    opened = coordinator.open_committed_snapshot(
        SnapshotRequest(session_id, None, SnapshotPurpose.DETAIL)
    )
    if isinstance(opened, Err):
        return _fail("시험 기록을 읽을 수 없습니다. 시험이 삭제되었거나 사용 중일 수 있습니다.")
    lease = opened.value
    try:
        stream = lease.open_allowlisted("semantic_inputs.json")
        if isinstance(stream, Err):
            return _fail("시험 기록을 읽을 수 없습니다.")
        with stream.value:
            combined = _object(_object(json.load(stream.value)).get("combined"))
        record = SessionRecord.from_dict(_object(combined["session"]))
        roster = RosterSnapshot.from_dict(_object(combined["roster"]))
        if record.session_id != session_id or record.revision != lease.snapshot_ref.revision:
            raise ValueError("canonical session does not match pinned snapshot")
        return Ok((record, roster, lease.snapshot_ref.revision))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return _fail("시험 기록이 손상되어 읽을 수 없습니다.")
    finally:
        lease.close()


def load_part_scores(
    coordinator: SessionCommitCoordinator, session_id: str, label: str
) -> Result[PartScores]:
    """Per-student points of one graded (GRADED or FINALIZED) session, under part ``label``."""
    session = _read_session(coordinator, session_id)
    if isinstance(session, Err):
        return session
    record, roster, revision = session.value
    if record.state not in (SessionState.GRADED, SessionState.FINALIZED):
        return _fail(f"채점하지 않은 시험이 있습니다: {record.exam_name}")
    read = CommittedGradingSnapshotReader(coordinator).read_grading_snapshot(session_id, revision)
    if isinstance(read, Err):
        return _fail(f"채점 기록을 읽을 수 없습니다: {record.exam_name}")
    snapshot = read.value
    key = snapshot.answer_key
    names = {row.student_id: row.name for row in roster.rows if row.student_id is not None}
    committed = (
        {row.work_item_id: row.score for row in snapshot.scores.rows}
        if snapshot.scores is not None
        else {}
    )
    asked = tuple(entry.status is not KeyQuestionStatus.UNASKED for entry in key.entries)
    question_points = tuple(
        Decimal(entry.points) if is_asked else None
        for entry, is_asked in zip(key.entries, asked, strict=True)
    )
    students: list[tuple[int, PartStudent]] = []
    for position, response in enumerate(snapshot.responses):
        outcomes = question_outcomes(response, key)
        held_back = REVIEW in outcomes
        earned = tuple(
            None if points is None else points if outcome == CORRECT else Decimal(0)
            for outcome, points in zip(outcomes, question_points, strict=True)
        )
        total = (
            None
            if held_back
            else committed.get(response.work_item_id)
            or sum((item for item in earned if item is not None), Decimal(0))
        )
        shown_id = response.student_id or ""
        name = names.get(shown_id, "") if shown_id else ""
        students.append(
            (
                position,
                PartStudent(
                    response.student_id,
                    name,
                    total,
                    () if held_back else earned,
                    response.source_label,
                    0,
                ),
            )
        )
    # The part's own score book lists by name in 가나다 order, then by student ID.
    ordered = sorted(
        students,
        key=lambda item: (
            not item[1].name,
            item[1].name,
            not item[1].student_id,
            item[1].student_id or "",
            item[0],
        ),
    )
    rows = tuple(
        PartStudent(
            student.student_id,
            student.name,
            student.total,
            student.earned,
            student.source_label,
            serial,
        )
        for serial, (_, student) in enumerate(ordered, 1)
    )
    return Ok(
        PartScores(
            label,
            record.exam_name,
            result_base_name(record.exam_name, record.created_at),
            record.graded_at,
            question_points,
            rows,
        )
    )


def _check_config(config: SubjectConfig, part_count: int) -> Err | None:
    missing = [number for number in config.part_numbers() if number > part_count]
    if not missing:
        return None
    names = ", ".join(f"파트{number}" for number in missing)
    return _fail(
        f"과목 구성 파일에 선택하지 않은 파트가 있습니다: {names} "
        f"(선택한 시험은 {part_count}개입니다)",
        "subject_config",
    )


def export_combined_report(
    coordinator: SessionCommitCoordinator,
    session_ids: tuple[str, ...],
    subject_config_path: str | None,
    destination: str,
    generated_at: str,
) -> Result[CombinedReportSummary]:
    """Write the 합산 성적표 for ``session_ids`` (파트1, 파트2... in the given order).

    ``destination`` is the file the user chose in a save dialog, so an existing file there is
    replaced.  The workbook is written to a temporary file next to it and moved into place, so
    nobody ever sees a partial file.
    """
    if not session_ids or len(set(session_ids)) != len(session_ids):
        return _fail("합산할 시험을 중복 없이 하나 이상 골라 주세요.", "session_ids")
    config: SubjectConfig | None = None
    if subject_config_path is not None:
        parsed = parse_subject_config(subject_config_path)
        if isinstance(parsed, Err):
            return parsed
        config = parsed.value
        mismatch = _check_config(config, len(session_ids))
        if mismatch is not None:
            return mismatch
    parts: list[PartScores] = []
    for number, session_id in enumerate(session_ids, 1):
        loaded = load_part_scores(coordinator, session_id, f"파트{number}")
        if isinstance(loaded, Err):
            return loaded
        parts.append(loaded.value)
    try:
        payload = build_combined_score_book(parts, config, generated_at)
    except (ValueError, TypeError):
        return _fail("합산 성적표를 만들 수 없습니다. 시험 기록을 확인해 주세요.")
    counts = summarize_combined_scores(parts)
    target = Path(destination)
    temporary_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent
        )
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    except OSError:
        return _fail("합산 성적표를 저장할 수 없습니다. 저장 위치를 확인해 주세요.", "destination")
    finally:
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
    return Ok(
        CombinedReportSummary(
            str(target), counts.students, counts.complete, counts.needs_review, counts.unreadable
        )
    )


def write_subject_config_sample(path: str) -> Result[None]:
    """Atomically write the sample subject-configuration workbook."""
    target = Path(path)
    temporary_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent
        )
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(subject_config_sample_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    except OSError:
        return _fail("예시 파일을 저장할 수 없습니다. 저장 위치를 확인해 주세요.", "path")
    finally:
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)
    return Ok(None)


__all__ = [
    "CombinedReportSummary",
    "export_combined_report",
    "load_part_scores",
    "write_subject_config_sample",
]
