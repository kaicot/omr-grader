"""A teacher fixes a misbubbled student ID; every result follows, and the fix can be undone.

Synthetic exams only: part 1 has one sheet that reads 20260009.  Part 2 has two sheets:
the first student meant 20260009 but bubbled 20260001, the second reads 20260002.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import openpyxl
import pytest
from openpyxl import load_workbook

from omr_grader.application.answer_key_use_case import AnswerKeyWorkbookUseCase
from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import (
    BackupExportRequest,
    BackupValidateRequest,
    CollisionPolicy,
    FinalizeCommand,
    RegradeCommand,
    RestoreCommand,
    ScanCommand,
    ScanSource,
    SnapshotRequest,
)
from omr_grader.application.grading_use_case import GradingUseCase
from omr_grader.application.id_fix_use_case import StudentIdFixCommand, StudentIdFixUseCase
from omr_grader.bootstrap import bootstrap
from omr_grader.domain.enums import ExamTerm, SnapshotPurpose, StudentIdStatus
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import DATA_FORMAT_VERSION
from omr_grader.infrastructure.combined_report import export_combined_report
from omr_grader.infrastructure.detail_repository import DetailRepository
from omr_grader.infrastructure.form_detection import FormDetector
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.infrastructure.scan_runtime import ScanRuntime, bind_scan_runtime
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.answer_key import ANSWER_KEY_HEADERS
from omr_grader.workbooks.schemas import RESPONSE_SHEET_NAME
from tests.helpers.pdf_writer import write_pdf
from tests.helpers.synthetic_omr import encode_png, render_sheet

MEANT = "20260009"
MISREAD = "20260001"
OTHER = "20260002"
NAMES = {MEANT: "가람", OTHER: "나래"}
_ANSWERS = {question: (question * 7) % 5 + 1 for question in range(1, 101)}


@dataclass
class _School:
    paths: ManagedPaths
    coordinator: SessionCommitCoordinator
    inputs: Path
    token: object
    profile: str = ""

    def scan(self, name: str, student_ids: tuple[str, ...]) -> tuple[str, int]:
        pdf = self.inputs / f"{name}.pdf"
        write_pdf(
            pdf,
            [
                encode_png(render_sheet(_ANSWERS, student_id, seed=index + 1))
                for index, student_id in enumerate(student_ids)
            ],
        )
        profiles = ProfileStore(self.paths, self.token)
        if not self.profile:
            detected = FormDetector(profiles).detect((str(pdf),))
            assert isinstance(detected, Ok)
            saved = profiles.save_generated(
                detected.value.generated_profile or b"", detected.value.suggested_filename
            )
            assert isinstance(saved, Ok)
            self.profile = saved.value.stored_name
        scanned = bind_scan_runtime(ScanRuntime(profiles, SessionStore(self.paths))).run_scan(
            ScanCommand(
                f"scan-{uuid4().hex}", uuid4().hex, 0, name, None, ExamTerm.UNSPECIFIED,
                self.profile, str(self.inputs / "roster.xlsx"), ScanSource((str(pdf),)), 5, False,
            )
        )
        assert isinstance(scanned, Ok)
        return scanned.value.session_id, scanned.value.revision

    def grading(self) -> GradingUseCase:
        return GradingUseCase(
            CommittedGradingSnapshotReader(self.coordinator),
            AnswerKeyWorkbookUseCase(),
            self.coordinator,
        )

    def grade(self, session_id: str, revision: int) -> int:
        graded = self.grading().regrade(
            RegradeCommand(session_id, revision, str(self.inputs / "key.xlsx"), "정답표", uuid4().hex)
        )
        assert isinstance(graded, Ok)
        return graded.value.revision

    def fix(self, session_id: str, revision: int, work_item_id: str, student_id: str | None):
        return StudentIdFixUseCase(CommittedGradingSnapshotReader(self.coordinator), self.coordinator).fix(
            StudentIdFixCommand(session_id, revision, uuid4().hex, work_item_id, student_id)
        )

    def snapshot(self, session_id: str, revision: int):
        read = CommittedGradingSnapshotReader(self.coordinator).read_grading_snapshot(
            session_id, revision
        )
        assert isinstance(read, Ok)
        return read.value

    def folder(self, session_id: str) -> Path:
        return next(
            path
            for path in self.paths.data_dir.iterdir()
            if (path / "IDENTITY.json").is_file()
            and json.loads((path / "IDENTITY.json").read_text(encoding="utf-8"))["session_id"]
            == session_id
        )


@pytest.fixture
def school(tmp_path: Path) -> _School:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    roster = openpyxl.Workbook()
    sheet = roster.active
    sheet.title = "명단"
    sheet.append(["연번", "학번", "이름"])
    for serial, (student_id, name) in enumerate(NAMES.items(), 1):
        sheet.append([serial, student_id, name])
    roster.save(inputs / "roster.xlsx")
    key = openpyxl.Workbook()
    sheet = key.active
    sheet.title = "정답표"
    sheet.append(list(ANSWER_KEY_HEADERS))
    for question in range(1, 101):
        sheet.append([question, str(_ANSWERS[question]), 1])
    key.save(inputs / "key.xlsx")
    (tmp_path / "root").mkdir()
    outcome = bootstrap(ManagedPaths.from_root(tmp_path / "root"))
    assert not isinstance(outcome, Err)
    paths = outcome.value.paths
    return _School(
        paths,
        SessionCommitCoordinator(SessionStore(paths)),
        inputs,
        outcome.value.capability_token,
    )


def _misread_sheet(school: _School, session_id: str, revision: int) -> str:
    return next(
        response.work_item_id
        for response in school.snapshot(session_id, revision).responses
        if response.student_id == MISREAD
    )


def _score_book(school: _School, session_id: str):
    (path,) = school.folder(session_id).glob("*_채점결과_*.xlsx")
    return load_workbook(path)


def _rows(sheet, columns: tuple[int, ...]) -> set[tuple[object, ...]]:
    return {
        tuple(sheet.cell(row, column).value for column in columns)
        for row in range(2, sheet.max_row + 1)
    }


def test_a_fix_on_a_graded_exam_reaches_every_result_and_survives_regrading(
    school: _School,
) -> None:
    session_id, revision = school.scan("part2", (MISREAD, OTHER))
    revision = school.grade(session_id, revision)
    sheet = _misread_sheet(school, session_id, revision)

    fixed = school.fix(session_id, revision, sheet, MEANT)

    assert isinstance(fixed, Ok)
    revision = fixed.value.revision
    snapshot = school.snapshot(session_id, revision)
    # What was read stays as read; results show the fix.
    assert {item.student_id for item in snapshot.responses} == {MISREAD, OTHER}
    assert {item.student_id for item in snapshot.shown_responses} == {MEANT, OTHER}
    book = _score_book(school, session_id)
    scores = book["채점결과"]
    note_column = scores.max_column
    assert _rows(scores, (2, 3)) == {(MEANT, NAMES[MEANT]), (OTHER, NAMES[OTHER])}
    notes = {scores.cell(row, 2).value: scores.cell(row, note_column).value for row in (2, 3)}
    assert notes[MEANT] == f"학번 수정(원래 {MISREAD})"
    assert notes[OTHER] in (None, "")
    responses = book[RESPONSE_SHEET_NAME]
    assert {responses.cell(row, 3).value for row in (2, 3)} == {MEANT, OTHER}
    images = sorted(path.name for path in (school.folder(session_id) / "02채점결과이미지").iterdir())
    assert any(MEANT in name and NAMES[MEANT] in name for name in images)
    assert not any(MISREAD in name for name in images)
    assert len(images) == 2

    # Regrading keeps the fix; the next generation carries it forward.
    revision = school.grade(session_id, revision)
    assert {
        item.student_id for item in school.snapshot(session_id, revision).shown_responses
    } == {MEANT, OTHER}
    assert _rows(_score_book(school, session_id)["채점결과"], (2,)) == {(MEANT,), (OTHER,)}

    # 원래대로: the sheet reads as it was bubbled again and the note is gone.
    undone = school.fix(session_id, revision, sheet, None)
    assert isinstance(undone, Ok)
    snapshot = school.snapshot(session_id, undone.value.revision)
    assert snapshot.id_fixes == ()
    assert {item.student_id for item in snapshot.shown_responses} == {MISREAD, OTHER}
    scores = _score_book(school, session_id)["채점결과"]
    assert _rows(scores, (2,)) == {(MISREAD,), (OTHER,)}
    assert all(
        "학번 수정" not in str(scores.cell(row, scores.max_column).value or "") for row in (2, 3)
    )


def test_a_fix_before_grading_rewrites_the_response_book(school: _School) -> None:
    # The third sheet has no ID marked at all: its note must still ask for an ID check.
    session_id, revision = school.scan("part2", (MISREAD, OTHER, ""))
    sheet = _misread_sheet(school, session_id, revision)

    fixed = school.fix(session_id, revision, sheet, MEANT)

    assert isinstance(fixed, Ok)
    (path,) = school.folder(session_id).glob("*_응답결과_*.xlsx")
    responses = load_workbook(path)[RESPONSE_SHEET_NAME]
    note_column = responses.max_column
    rows = {
        responses.cell(row, 3).value or "": (
            responses.cell(row, 4).value,
            responses.cell(row, note_column).value,
        )
        for row in (2, 3, 4)
    }
    assert rows[MEANT] == (NAMES[MEANT], f"학번 수정(원래 {MISREAD})")
    assert "학번 확인 필요" in str(rows[""][1])
    # Grading afterwards keeps the fix.
    revision = school.grade(session_id, fixed.value.revision)
    assert _rows(_score_book(school, session_id)["채점결과"], (2,)) == {(MEANT,), (OTHER,), (None,)}


def test_the_combined_report_joins_the_fixed_sheet_to_its_student(
    school: _School, tmp_path: Path
) -> None:
    first, first_revision = school.scan("part1", (MEANT,))
    school.grade(first, first_revision)
    second, revision = school.scan("part2", (MISREAD, OTHER))
    revision = school.grade(second, revision)
    destination = tmp_path / "combined.xlsx"

    def students() -> set[object]:
        exported = export_combined_report(
            school.coordinator, (first, second), None, str(destination), "2026-10-31T00:00:00.000000Z"
        )
        assert isinstance(exported, Ok)
        sheet = load_workbook(destination).worksheets[0]
        return {
            sheet.cell(row, 2).value for row in range(2, sheet.max_row + 1) if sheet.cell(row, 2).value
        }

    # Before the fix the misbubbled sheet stands apart as a student of its own.
    assert MISREAD in students()
    assert isinstance(school.fix(second, revision, _misread_sheet(school, second, revision), MEANT), Ok)
    assert students() == {MEANT, OTHER}


def test_the_detail_list_and_finalizing_keep_the_fix(school: _School) -> None:
    session_id, revision = school.scan("part2", (MISREAD, OTHER))
    revision = school.grade(session_id, revision)
    sheet = _misread_sheet(school, session_id, revision)
    fixed = school.fix(session_id, revision, sheet, MEANT)
    assert isinstance(fixed, Ok)

    repository = DetailRepository(school.coordinator)
    opened = repository.open_detail(session_id, fixed.value.revision)
    assert isinstance(opened, Ok)
    handle, rows = opened.value
    by_sheet = {row.work_item_id: row for row in rows}
    assert (by_sheet[sheet].student_id, by_sheet[sheet].fixed_from) == (MEANT, MISREAD)
    assert {row.fixed_from for row in rows if row.work_item_id != sheet} == {None}
    roster = repository.read_roster(handle.handle_id)
    assert isinstance(roster, Ok) and dict(roster.value) == NAMES
    assert isinstance(repository.close_detail(handle.handle_id), Ok)

    finalized = school.grading().finalize(
        FinalizeCommand(session_id, fixed.value.revision, uuid4().hex)
    )
    assert isinstance(finalized, Ok)
    snapshot = school.snapshot(session_id, finalized.value.revision)
    assert {item.student_id for item in snapshot.shown_responses} == {MEANT, OTHER}
    (book,) = school.folder(session_id).glob("*_최종성적표_*.xlsx")
    assert _rows(load_workbook(book).worksheets[0], (2,)) == {(MEANT,), (OTHER,)}
    # A finalized exam keeps its IDs.
    refused = school.fix(session_id, finalized.value.revision, sheet, None)
    assert isinstance(refused, Err) and refused.errors[0].code == "ID_FIX_STATE"


def test_a_duplicate_is_allowed_and_flagged_until_resolved(school: _School) -> None:
    session_id, revision = school.scan("part2", (MISREAD, OTHER))
    revision = school.grade(session_id, revision)
    sheet = _misread_sheet(school, session_id, revision)

    fixed = school.fix(session_id, revision, sheet, OTHER)

    assert isinstance(fixed, Ok)
    shown = school.snapshot(session_id, fixed.value.revision).shown_responses
    assert {item.student_id_status for item in shown} == {StudentIdStatus.DUPLICATE}
    scores = _score_book(school, session_id)["채점결과"]
    assert {scores.cell(row, scores.max_column).value for row in (2, 3)} >= {"중복확인필요"}
    resolved = school.fix(session_id, fixed.value.revision, sheet, MEANT)
    assert isinstance(resolved, Ok)
    shown = school.snapshot(session_id, resolved.value.revision).shown_responses
    assert {item.student_id_status for item in shown} == {StudentIdStatus.NORMAL}


@pytest.mark.parametrize(
    ("student_id", "code"),
    (("2026001", "ID_FIX_INVALID"), ("2026000x", "ID_FIX_INVALID"), (MISREAD, "ID_FIX_NOOP"), (None, "ID_FIX_NOOP")),
)
def test_bad_fixes_are_refused_without_a_new_generation(
    school: _School, student_id: str | None, code: str
) -> None:
    session_id, revision = school.scan("part2", (MISREAD, OTHER))
    sheet = _misread_sheet(school, session_id, revision)

    refused = school.fix(session_id, revision, sheet, student_id)

    assert isinstance(refused, Err)
    assert refused.errors[0].code == code
    assert school.snapshot(session_id, revision).id_fixes == ()


def test_a_backup_carries_the_fix_to_another_install(school: _School, tmp_path: Path) -> None:
    session_id, revision = school.scan("part2", (MISREAD, OTHER))
    revision = school.grade(session_id, revision)
    fixed = school.fix(session_id, revision, _misread_sheet(school, session_id, revision), MEANT)
    assert isinstance(fixed, Ok)
    destination = SessionStore(tmp_path / "other")
    service = BackupApplicationService(
        school.coordinator, restore_publisher=destination.restore_publisher()
    )
    archive = tmp_path / "exam.omrbak"
    exported = service.export_backup(
        BackupExportRequest(
            SnapshotRequest(session_id, None, SnapshotPurpose.BACKUP),
            str(archive),
            CollisionPolicy.ERROR,
            "backup-1",
        )
    )
    assert isinstance(exported, Ok)
    validated = service.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(validated, Ok)
    restored = service.restore_backup(
        RestoreCommand(validated.value, str(destination.root), "restore-1")
    )
    assert isinstance(restored, Ok)
    reader = CommittedGradingSnapshotReader(SessionCommitCoordinator(destination))
    read = reader.read_grading_snapshot(restored.value.session_id, restored.value.revision)
    assert isinstance(read, Ok)
    assert {item.student_id for item in read.value.shown_responses} == {MEANT, OTHER}

    # The archive says format 2, which 4.4 and older refuse; a newer one is refused here.
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        wrapper_name = next(name for name in names if name.endswith("archive_manifest.json"))
        wrapper = json.loads(package.read(wrapper_name))
        payloads = {name: package.read(name) for name in names}
    assert wrapper["format_version"] == DATA_FORMAT_VERSION == 2
    newer = tmp_path / "newer.omrbak"
    with zipfile.ZipFile(newer, "w") as package:
        for name, payload in payloads.items():
            if name == wrapper_name:
                payload = json.dumps({**wrapper, "format_version": 3}).encode("utf-8")
            package.writestr(name, payload)
    refused = service.validate_backup(BackupValidateRequest(str(newer)))
    assert isinstance(refused, Err)
    assert refused.errors[0].code == "BACKUP_FORMAT_NEWER"
