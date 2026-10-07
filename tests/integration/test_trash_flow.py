"""A real exam goes to the trash under its folder name, is listed there, and comes back."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import fitz
import openpyxl

from omr_grader.application.answer_key_use_case import AnswerKeyWorkbookUseCase
from omr_grader.application.dto import (
    RegradeCommand,
    ScanCommand,
    ScanSource,
    SessionMutationRequest,
)
from omr_grader.application.grading_use_case import GradingUseCase
from omr_grader.bootstrap import bootstrap
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.dashboard_repository import DashboardRepository, trash_lister
from omr_grader.infrastructure.form_detection import FormDetector
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.infrastructure.scan_runtime import ScanRuntime, bind_scan_runtime
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.answer_key import ANSWER_KEY_HEADERS
from tests.helpers.synthetic_omr import encode_png, render_sheet


def _graded_exam(tmp_path: Path) -> tuple[SessionStore, SessionCommitCoordinator, str, int]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    pdf = inputs / "scans.pdf"
    document = fitz.open()
    answers = {question: (question * 7) % 5 + 1 for question in range(1, 101)}
    document.new_page(width=842, height=595).insert_image(
        fitz.Rect(0, 0, 842, 595), stream=encode_png(render_sheet(answers, "20260001", seed=1))
    )
    document.save(str(pdf))
    document.close()
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
            f"scan-{uuid4().hex}", uuid4().hex, 0, "생리학 중간고사", None, ExamTerm.UNSPECIFIED,
            saved.value.stored_name, None, ScanSource((str(pdf),)), 5, False,
        )
    )
    assert isinstance(scanned, Ok)
    graded = GradingUseCase(
        CommittedGradingSnapshotReader(coordinator), AnswerKeyWorkbookUseCase(), coordinator
    ).regrade(
        RegradeCommand(
            scanned.value.session_id, scanned.value.revision, str(key), "정답표", uuid4().hex
        )
    )
    assert isinstance(graded, Ok)
    return store, coordinator, graded.value.session_id, graded.value.revision


def test_a_deleted_exam_keeps_its_folder_name_in_the_trash_and_can_come_back(tmp_path):
    store, coordinator, session_id, revision = _graded_exam(tmp_path)
    data = store._root
    folder = next(path for path in data.iterdir() if path.name.endswith("생리학_중간고사"))
    # Exam folders start with their creation time, like the workbooks inside them.
    assert folder.name[:13].replace("_", "").isdigit()
    repository = DashboardRepository(
        data / "dashboard_index.json",
        store.discover_active_committed_leases,
        list_trash_entries=trash_lister(store.discover_trash_committed_leases),
    )

    def listed() -> tuple[list[str], list[str]]:
        active = repository.list_active()
        trash = repository.list_trash()
        assert isinstance(active, Ok) and isinstance(trash, Ok)
        return (
            [entry.display_folder for entry in active.value.entries],
            [entry.display_folder for entry in trash.value.entries],
        )

    assert listed() == ([folder.name], [])

    deleted = coordinator.soft_delete(SessionMutationRequest(session_id, revision, uuid4().hex))

    assert isinstance(deleted, Ok)
    assert (data / "_휴지통" / "세션" / folder.name).is_dir()
    assert listed() == ([], [folder.name])

    restored = coordinator.restore_from_trash(
        SessionMutationRequest(session_id, revision, uuid4().hex)
    )

    assert isinstance(restored, Ok)
    assert folder.is_dir()
    assert listed() == ([folder.name], [])

    assert isinstance(
        coordinator.soft_delete(SessionMutationRequest(session_id, revision, uuid4().hex)), Ok
    )
    removed = coordinator.permanently_delete(
        SessionMutationRequest(session_id, revision, uuid4().hex)
    )

    assert isinstance(removed, Ok)
    assert listed() == ([], [])
