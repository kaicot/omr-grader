"""Importing an older install: exams through backup/restore, forms, settings, safe re-runs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import openpyxl
import pytest

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
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.infrastructure.config_store import AppConfig, save_config
from omr_grader.infrastructure.data_import import import_previous_install, install_root
from omr_grader.infrastructure.form_detection import FormDetector
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.infrastructure.scan_runtime import ScanRuntime, bind_scan_runtime
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.answer_key import ANSWER_KEY_HEADERS
from tests.helpers.pdf_writer import write_pdf
from tests.helpers.synthetic_omr import encode_png, render_sheet


class _Install:
    """A bootstrapped portable folder with its stores."""

    def __init__(self, root: Path, copy_from: Path | None = None) -> None:
        if copy_from is None:
            root.mkdir(parents=True)
        else:
            shutil.copytree(copy_from, root)
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
        answers = {question: (question * 7) % 5 + 1 for question in range(1, 101)}
        write_pdf(pdf, [encode_png(render_sheet(answers, "20260001", seed=1))])
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


@pytest.fixture(scope="module")
def old_template(tmp_path_factory):
    """An old install with one active exam, one trashed exam, a form and settings."""
    base = tmp_path_factory.mktemp("old-template")
    old = _Install(base / "OMR Grader")
    old.graded_exam(base / "work1", "생리학 중간고사")
    trashed = old.graded_exam(base / "work2", "해부학 기말고사")
    assert isinstance(
        old.coordinator.soft_delete(SessionMutationRequest(*trashed, uuid4().hex)), Ok
    )
    form = sorted(path.name for path in old.paths.profiles_dir.glob("*.omrtemplate"))[0]
    assert isinstance(save_config(old.paths, AppConfig(form, 8, True), old.token), Ok)
    return old.paths.root, trashed, form


def _failure() -> Err:
    return Err((ErrorInfo("TEST_FAILED", "error.test_failed", None, context={"reason": "x"}),))


def _everything(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under ``root`` (folders map to None), locks included."""
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
        if path.suffix != ".log"
    }


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


def test_the_old_folder_gets_no_new_files_or_folders_even_without_its_bookkeeping(
    tmp_path, old_template
):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    for name in (".reservations", ".deleting", ".locks"):
        shutil.rmtree(old.paths.data_dir / name, ignore_errors=True)
    before = _everything(old.paths.root)
    new = _Install(tmp_path / "new")

    imported = import_previous_install(str(old.paths.root), new.paths, new.store)

    assert isinstance(imported, Ok)
    assert (imported.value.exams, imported.value.trash, imported.value.failed) == (1, 1, ())
    assert _everything(old.paths.root) == before


def test_a_read_only_old_folder_is_read_without_any_write(tmp_path, old_template, monkeypatch):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    new = _Install(tmp_path / "new")
    old_root = str(old.paths.root.resolve()).lower()
    before = _everything(old.paths.root)
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    real_open, real_mkdir, real_touch = os.open, Path.mkdir, Path.touch

    def inside_old(path: object) -> bool:
        return str(Path(str(path)).resolve()).lower().startswith(old_root)

    def guarded_open(path, flags, *args, **kwargs):
        if flags & write_flags and inside_old(path):
            raise PermissionError(13, "read-only", str(path))
        return real_open(path, flags, *args, **kwargs)

    def guarded_mkdir(self, *args, **kwargs):
        if inside_old(self):
            raise PermissionError(13, "read-only", str(self))
        return real_mkdir(self, *args, **kwargs)

    def guarded_touch(self, *args, **kwargs):
        if inside_old(self):
            raise PermissionError(13, "read-only", str(self))
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(Path, "touch", guarded_touch)

    imported = import_previous_install(str(old.paths.root), new.paths, new.store)

    monkeypatch.undo()
    assert isinstance(imported, Ok)
    assert (imported.value.exams, imported.value.trash, imported.value.failed) == (1, 1, ())
    assert _everything(old.paths.root) == before


def test_one_unreadable_exam_is_skipped_with_a_reason_and_the_rest_come_over(
    tmp_path, old_template
):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    (old.paths.data_dir / "새 폴더").mkdir()
    broken = old.paths.data_dir / "260101_090000_깨진 시험"
    broken.mkdir()
    (broken / "IDENTITY.json").write_text(json.dumps({"session_id": uuid4().hex}), encoding="utf-8")
    (broken / "CURRENT.json").write_text("{not json", encoding="utf-8")
    nameless = old.paths.data_dir / "260101_090001_정보 없음"
    nameless.mkdir()
    (nameless / "IDENTITY.json").write_text("[]", encoding="utf-8")
    new = _Install(tmp_path / "new")

    imported = import_previous_install(str(old.paths.root), new.paths, new.store)

    assert isinstance(imported, Ok)
    summary = imported.value
    assert (summary.exams, summary.trash) == (1, 1)
    assert len(summary.failed) == 2
    assert any(item.startswith("260101_090000_깨진 시험 (") for item in summary.failed)
    assert any(item.startswith("260101_090001_정보 없음 (") for item in summary.failed)
    assert not any("새 폴더" in item for item in summary.failed)
    assert len(new.exam_folders()) == 1


def test_a_trash_exam_that_cannot_be_moved_is_reported_not_silently_kept(
    tmp_path, old_template, monkeypatch
):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    new = _Install(tmp_path / "new")
    real = SessionCommitCoordinator.soft_delete
    calls = []

    def failing(self, request):
        calls.append(request)
        return _failure()

    monkeypatch.setattr(SessionCommitCoordinator, "soft_delete", failing)

    first = import_previous_install(str(old.paths.root), new.paths, new.store)

    assert isinstance(first, Ok) and calls
    assert (first.value.exams, first.value.trash) == (1, 0)
    assert len(first.value.failed) == 1 and "휴지통으로 옮기지 못해" in first.value.failed[0]
    monkeypatch.setattr(SessionCommitCoordinator, "soft_delete", real)

    again = import_previous_install(str(old.paths.root), new.paths, new.store)

    # The exam is here (and may have been restored on purpose), so it is never moved again.
    assert isinstance(again, Ok)
    assert (again.value.exams, again.value.already_present, again.value.trash) == (0, 2, 0)
    assert len(new.exam_folders()) == 2


def test_forms_with_the_same_content_are_not_copied_again_and_the_default_follows(
    tmp_path, old_template
):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    form = old_template[2]
    new = _Install(tmp_path / "new")
    other_name = "내가_바꾼_이름.omrtemplate"
    shutil.copyfile(old.paths.profiles_dir / form, new.paths.profiles_dir / other_name)
    saved: list[AppConfig] = []

    def save_settings(config):
        saved.append(config)
        return Ok(config)

    for _ in range(2):
        imported = import_previous_install(
            str(old.paths.root),
            new.paths,
            new.store,
            new.profiles.save_generated,
            save_settings=save_settings,
        )
        assert isinstance(imported, Ok)
        assert imported.value.profiles == 0
        assert imported.value.settings == AppConfig(other_name, 8, True)

    assert sorted(path.name for path in new.paths.profiles_dir.iterdir()) == [other_name]
    assert saved == [AppConfig(other_name, 8, True)] * 2


def test_a_form_is_copied_once_and_a_failed_settings_save_is_not_reported_as_imported(
    tmp_path, old_template
):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    new = _Install(tmp_path / "new")

    def failing_save(config):
        return _failure()

    results = [
        import_previous_install(
            str(old.paths.root),
            new.paths,
            new.store,
            new.profiles.save_generated,
            save_settings=failing_save,
        )
        for _ in range(3)
    ]

    summaries = [item.value for item in results if isinstance(item, Ok)]
    assert len(summaries) == 3
    assert [item.profiles for item in summaries] == [1, 0, 0]
    assert all(item.settings is None and item.settings_failed for item in summaries)
    assert len(list(new.paths.profiles_dir.glob("*.omrtemplate"))) == 1


def test_settings_are_saved_through_the_given_saver(tmp_path, old_template):
    old = _Install(tmp_path / "old", copy_from=old_template[0])
    new = _Install(tmp_path / "new")
    seen = []

    imported = import_previous_install(
        str(old.paths.root),
        new.paths,
        new.store,
        new.profiles.save_generated,
        save_settings=lambda config: seen.append(config) or Ok(config),
    )

    assert isinstance(imported, Ok)
    assert imported.value.settings == seen[0] == AppConfig(old_template[2], 8, True)
    assert imported.value.settings_failed is False
