"""Importing an older install: exams through backup/restore, forms, settings, safe re-runs."""

from __future__ import annotations

import json
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
from omr_grader.infrastructure.config_store import AppConfig, save_config
from omr_grader.infrastructure.data_import import import_previous_install, install_root
from omr_grader.infrastructure.form_detection import FormDetector
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.infrastructure.scan_runtime import ScanRuntime, bind_scan_runtime
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.answer_key import ANSWER_KEY_HEADERS
from tests.helpers.synthetic_omr import encode_png, render_sheet


class _Install:
    """A bootstrapped portable folder with its stores."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        outcome = bootstrap(ManagedPaths.from_root(root))
        assert not isinstance(outcome, Err) and outcome.value.write_enabled
        self.paths = outcome.value.paths
        self.token = outcome.value.capability_token
        self.profiles = ProfileStore(self.paths, self.token)
        self.store = SessionStore(self.paths)
        self.coordinator = SessionCommitCoordinator(self.store)

    def graded_exam(self, work: Path, exam_name: str) -> tuple[str, int]:
        work.mkdir(parents=True)
        pdf = work / "scans.pdf"
        document = fitz.open()
        answers = {question: (question * 7) % 5 + 1 for question in range(1, 101)}
        document.new_page(width=842, height=595).insert_image(
            fitz.Rect(0, 0, 842, 595), stream=encode_png(render_sheet(answers, "20260001", seed=1))
        )
        document.save(str(pdf))
        document.close()
        key = work / "key.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.title = "정답표"
        sheet.append(list(ANSWER_KEY_HEADERS))
        for question in range(1, 101):
            sheet.append([question, str((question * 7) % 5 + 1), 1])
        book.save(key)
        detected = FormDetector(self.profiles).detect((str(pdf),))
        assert isinstance(detected, Ok)
        profile = detected.value.profile_filename
        if detected.value.generated_profile is not None:
            saved = self.profiles.save_generated(
                detected.value.generated_profile, detected.value.suggested_filename
            )
            assert isinstance(saved, Ok)
            profile = saved.value.stored_name
        assert profile is not None
        scanned = bind_scan_runtime(ScanRuntime(self.profiles, self.store)).run_scan(
            ScanCommand(
                f"scan-{uuid4().hex}", uuid4().hex, 0, exam_name, None, ExamTerm.UNSPECIFIED,
                profile, None, ScanSource((str(pdf),)), 5, False,
            )
        )  # fmt: skip
        assert isinstance(scanned, Ok)
        graded = GradingUseCase(
            CommittedGradingSnapshotReader(self.coordinator),
            AnswerKeyWorkbookUseCase(),
            self.coordinator,
        ).regrade(
            RegradeCommand(
                scanned.value.session_id, scanned.value.revision, str(key), "정답표", uuid4().hex
            )
        )
        assert isinstance(graded, Ok)
        return graded.value.session_id, graded.value.revision

    def exam_folders(self) -> list[str]:
        return sorted(
            path.name
            for path in self.paths.data_dir.iterdir()
            if path.is_dir() and (path / "IDENTITY.json").exists()
        )


def _tree(root: Path) -> dict[str, bytes]:
    """Every file of the old folder except lock bookkeeping, to prove it is left alone."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".locks" not in path.parts and path.suffix != ".log"
    }


def test_exams_forms_and_settings_come_over_and_the_old_folder_is_untouched(tmp_path):
    old = _Install(tmp_path / "old" / "OMR Grader")
    old.graded_exam(tmp_path / "work1", "생리학 중간고사")
    trashed = old.graded_exam(tmp_path / "work2", "해부학 기말고사")
    assert isinstance(
        old.coordinator.soft_delete(SessionMutationRequest(*trashed, uuid4().hex)), Ok
    )
    assert isinstance(save_config(old.paths, AppConfig("", 7, True), old.token), Ok)
    before = _tree(old.paths.root)
    new = _Install(tmp_path / "new" / "OMR Grader")
    progress: list[tuple[int, int]] = []

    imported = import_previous_install(
        str(old.paths.root),
        new.paths,
        new.store,
        new.profiles.save_generated,
        lambda item: progress.append((item.done, item.total)),
    )

    assert isinstance(imported, Ok)
    summary = imported.value
    assert (summary.exams, summary.already_present, summary.failed) == (1, 0, ())
    assert summary.trash == 1
    # The exam deleted in the old folder waits in the new trash under its own name.
    trash = ("_휴지통", "세션")
    assert sorted(path.name for path in new.paths.data_dir.joinpath(*trash).iterdir()) == sorted(
        path.name for path in old.paths.data_dir.joinpath(*trash).iterdir()
    )
    back = new.coordinator.restore_from_trash(SessionMutationRequest(trashed[0], trashed[1], "x1"))
    assert isinstance(back, Ok)
    assert len(new.exam_folders()) == 2
    new.coordinator.soft_delete(SessionMutationRequest(trashed[0], trashed[1], "x2"))
    assert summary.profiles == 1
    assert summary.settings == AppConfig("", 7, True)
    assert progress == [(0, 2), (1, 2), (2, 2)]
    # Same date-first folder name as in the old folder, with its result workbook.
    assert new.exam_folders() == old.exam_folders()
    folder = new.paths.data_dir / new.exam_folders()[0]
    assert any(path.name.endswith("_채점결과_생리학_중간고사.xlsx") for path in folder.iterdir())
    listed = new.store.discover_active_committed_leases()
    assert isinstance(listed, Ok) and len(listed.value) == 1
    for lease in listed.value:
        lease.close()
    assert sorted(path.name for path in new.paths.profiles_dir.iterdir()) == sorted(
        path.name for path in old.paths.profiles_dir.iterdir()
    )
    assert not any(path.name.startswith(".import-") for path in new.paths.root.iterdir())
    assert _tree(old.paths.root) == before


def test_running_it_again_skips_what_is_already_there(tmp_path):
    old = _Install(tmp_path / "old")
    old.graded_exam(tmp_path / "work", "생리학 중간고사")
    new = _Install(tmp_path / "new")
    first = import_previous_install(str(old.paths.root), new.paths, new.store)
    assert isinstance(first, Ok) and first.value.exams == 1

    again = import_previous_install(str(old.paths.root), new.paths, new.store)

    assert isinstance(again, Ok)
    assert (again.value.exams, again.value.already_present) == (0, 1)
    assert len(new.exam_folders()) == 1


def test_the_data_folder_may_be_picked_and_wrong_folders_are_refused(tmp_path):
    old = _Install(tmp_path / "old")
    new = _Install(tmp_path / "new")
    assert install_root(old.paths.data_dir) == old.paths.root
    assert install_root(tmp_path) is None

    nowhere = import_previous_install(str(tmp_path), new.paths, new.store)
    itself = import_previous_install(str(new.paths.root), new.paths, new.store)

    assert isinstance(nowhere, Err) and "OMR Grader 폴더가 아닙니다" in str(
        nowhere.errors[0].context["reason"]
    )
    assert isinstance(itself, Err) and "지금 실행 중인" in str(itself.errors[0].context["reason"])


def test_data_from_a_newer_format_is_not_imported(tmp_path):
    old = _Install(tmp_path / "old")
    (old.paths.data_dir / "FORMAT.json").write_text(
        json.dumps({"data_format": 99, "written_by": "9.0.0"}), encoding="utf-8"
    )
    new = _Install(tmp_path / "new")

    refused = import_previous_install(str(old.paths.root), new.paths, new.store)

    assert isinstance(refused, Err) and refused.errors[0].code == "IMPORT_SOURCE_NEWER"
    assert new.exam_folders() == []
