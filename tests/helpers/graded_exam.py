"""One real scanned and graded synthetic exam in a fresh portable root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import openpyxl

from omr_grader.application.answer_key_use_case import AnswerKeyWorkbookUseCase
from omr_grader.application.dto import RegradeCommand, ScanCommand, ScanSource
from omr_grader.application.grading_use_case import GradingUseCase
from omr_grader.bootstrap import bootstrap
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, Ok, Result
from omr_grader.infrastructure.form_detection import FormDetector
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.infrastructure.scan_runtime import ScanRuntime, bind_scan_runtime
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.answer_key import ANSWER_KEY_HEADERS
from tests.helpers.pdf_writer import write_pdf
from tests.helpers.synthetic_omr import encode_png, render_sheet


@dataclass(slots=True)
class GradedExam:
    paths: ManagedPaths
    store: SessionStore
    coordinator: SessionCommitCoordinator
    session_id: str
    revision: int
    key: Path

    @property
    def folder(self) -> Path:
        return next(
            path
            for path in self.paths.data_dir.iterdir()
            if path.is_dir() and (path / "IDENTITY.json").is_file()
        )

    def regrade(self) -> Result[object]:
        graded = GradingUseCase(
            CommittedGradingSnapshotReader(self.coordinator),
            AnswerKeyWorkbookUseCase(),
            self.coordinator,
        ).regrade(
            RegradeCommand(self.session_id, self.revision, str(self.key), "정답표", uuid4().hex)
        )
        if isinstance(graded, Ok):
            self.revision = graded.value.revision
        return graded


def graded_exam(tmp_path: Path, exam_name: str = "생리학 중간고사") -> GradedExam:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    pdf = inputs / "scans.pdf"
    answers = {question: (question * 7) % 5 + 1 for question in range(1, 101)}
    write_pdf(pdf, [encode_png(render_sheet(answers, "20260001", seed=1))])
    key = inputs / "key.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "정답표"
    sheet.append(list(ANSWER_KEY_HEADERS))
    for question in range(1, 101):
        sheet.append([question, str((question * 7) % 5 + 1), 1])
    book.save(key)

    (tmp_path / "root").mkdir()
    outcome = bootstrap(ManagedPaths.from_root(tmp_path / "root"))
    assert not isinstance(outcome, Err)
    paths, token = outcome.value.paths, outcome.value.capability_token
    profiles = ProfileStore(paths, token)
    store = SessionStore(paths)
    coordinator = SessionCommitCoordinator(store)
    detected = FormDetector(profiles).detect((str(pdf),))
    assert isinstance(detected, Ok)
    saved = profiles.save_generated(
        detected.value.generated_profile or b"", detected.value.suggested_filename
    )
    assert isinstance(saved, Ok)
    scanned = bind_scan_runtime(ScanRuntime(ProfileStore(paths, token), store)).run_scan(
        ScanCommand(
            f"scan-{uuid4().hex}", uuid4().hex, 0, exam_name, None, ExamTerm.UNSPECIFIED,
            saved.value.stored_name, None, ScanSource((str(pdf),)), 5, False,
        )
    )
    assert isinstance(scanned, Ok)
    exam = GradedExam(
        paths, store, coordinator, scanned.value.session_id, scanned.value.revision, key
    )
    assert isinstance(exam.regrade(), Ok)
    return exam
