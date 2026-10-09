"""Exam folders survive what Explorer, Excel and partial copies do to them."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from omr_grader.application.dto import SessionMutationRequest
from omr_grader.domain.enums import CleanupState
from omr_grader.domain.errors import Ok
from omr_grader.infrastructure.dashboard_repository import (
    DashboardListing,
    DashboardRepository,
    trash_lister,
)
from omr_grader.infrastructure.result_layout import (
    ANSWER_KEY_SOURCE_DIR,
    SCORE_IMAGE_DIR,
    SOURCE_IMAGE_DIR,
    result_workbook_kind,
)
from omr_grader.infrastructure.session_store import SessionStore, _refresh_result_view
from tests.helpers.graded_exam import GradedExam, graded_exam

_VIEW_DIRS = (SOURCE_IMAGE_DIR, SCORE_IMAGE_DIR, ANSWER_KEY_SOURCE_DIR)


def _repository(store: SessionStore, *, index_writable: bool = True) -> DashboardRepository:
    return DashboardRepository(
        store.root / "dashboard_index.json",
        store.discover_active_committed_leases,
        list_trash_entries=trash_lister(store.discover_trash_committed_leases),
        index_writable=index_writable,
    )


def _active(store: SessionStore, **options: bool) -> DashboardListing:
    listed = _repository(store, **options).list_active()
    assert isinstance(listed, Ok), listed
    return listed.value


def _trash(store: SessionStore) -> DashboardListing:
    listed = _repository(store).list_trash()
    assert isinstance(listed, Ok), listed
    return listed.value


def _reasons(listing: DashboardListing) -> str:
    return "\n".join(str(warning.context.get("reason", "")) for warning in listing.warnings)


def _book(folder: Path) -> Path:
    return next(
        child
        for child in folder.iterdir()
        if child.is_file() and result_workbook_kind(child.name) == "채점결과"
    )


def _current_generation(folder: Path) -> Path:
    pointer = json.loads((folder / "CURRENT.json").read_text(encoding="utf-8"))
    return folder.joinpath(*pointer["generation_relpath"].split("/"))


def _view_files(folder: Path) -> dict[str, int]:
    return {
        path.relative_to(folder).as_posix(): path.stat().st_size
        for name in _VIEW_DIRS
        for path in (folder / name).rglob("*")
        if path.is_file()
    }


def _assert_externalized_view(exam: GradedExam) -> None:
    folder = exam.folder
    generation = _current_generation(folder)
    assert os.path.samefile(_book(folder), generation / _book(folder).name)
    for name in ("images", "sources", SCORE_IMAGE_DIR):
        assert not (generation / name).exists()
    assert all((folder / name).is_dir() for name in (SOURCE_IMAGE_DIR, SCORE_IMAGE_DIR))


@pytest.fixture
def exam(tmp_path: Path) -> GradedExam:
    return graded_exam(tmp_path)


def test_regrading_with_the_result_book_open_keeps_the_folder_and_repairs_it_later(exam):
    folder = exam.folder
    before = _view_files(folder)
    assert before
    # Excel keeps the book open without delete sharing, as Python's open() does.
    with _book(folder).open("rb"):
        graded = exam.regrade()
        assert isinstance(graded, Ok)
        assert [warning.code for warning in graded.warnings] == ["POSTCOMMIT_RECOVERY_REQUIRED"]
        assert "Excel" in str(graded.warnings[0].context["reason"])
        assert "WinError" not in str(graded.warnings[0].context["reason"])
        # Nothing else in the folder changed while the book was held.
        assert _view_files(folder) == before
        held = _active(exam.store)
        assert [entry.session_id for entry in held.entries] == [exam.session_id]
        assert "열려" in _reasons(held)

    listing = _active(exam.store)

    assert [entry.revision for entry in listing.entries] == [exam.revision]
    assert not [w for w in listing.warnings if w.code != "DASHBOARD_INDEX_STALE"]
    _assert_externalized_view(exam)
    assert set(_view_files(folder)) == set(before)


def test_a_book_saved_over_by_excel_is_kept_as_the_users_copy_on_regrade(exam):
    folder = exam.folder
    book = _book(folder)
    # Excel saves into a new file and renames it over the old one, ending the hard link.
    replacement = folder / "excel-save.tmp"
    replacement.write_bytes(b"edited by the user")
    os.replace(replacement, book)

    listing = _active(exam.store)
    assert [entry.session_id for entry in listing.entries] == [exam.session_id]
    # Listing leaves the user's edit where it is.
    assert book.read_bytes() == b"edited by the user"

    assert isinstance(exam.regrade(), Ok)

    kept = folder / f"{book.stem}_사용자수정본.xlsx"
    assert kept.read_bytes() == b"edited by the user"
    assert os.path.samefile(_book(folder), _current_generation(folder) / book.name)
    assert isinstance(exam.regrade(), Ok)
    assert kept.read_bytes() == b"edited by the user"
    assert not (folder / f"{book.stem}_사용자수정본_사용자수정본.xlsx").exists()


def test_missing_lock_folder_is_rebuilt_so_exams_list_and_delete(exam):
    shutil.rmtree(exam.store.root / ".locks")

    listing = _active(exam.store)

    assert [entry.session_id for entry in listing.entries] == [exam.session_id]
    deleted = exam.coordinator.soft_delete(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )
    assert isinstance(deleted, Ok)
    assert [entry.session_id for entry in _trash(exam.store).entries] == [exam.session_id]


def test_stray_and_copied_folders_do_not_hide_the_other_exams(exam):
    data = exam.store.root
    original = exam.folder
    (data / "새 폴더").mkdir()
    copy = data / f"{original.name} - 복사본"
    shutil.copytree(original, copy)
    broken = data / "250101_090000_손상된시험"
    broken.mkdir()
    (broken / "IDENTITY.json").write_text("{", encoding="utf-8")

    listing = _active(exam.store)

    assert [entry.display_folder for entry in listing.entries] == [original.name]
    reasons = _reasons(listing)
    assert copy.name in reasons and broken.name in reasons
    assert "새 폴더" not in reasons
    # The extra copy is never removed by the program.
    assert copy.is_dir()
    deleted = exam.coordinator.soft_delete(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )
    assert isinstance(deleted, Ok)
    assert (data / "_휴지통" / "세션" / original.name).is_dir()


def test_a_damaged_exam_is_listed_out_with_its_folder_name(exam, tmp_path):
    folder = exam.folder
    image = next((folder / SCORE_IMAGE_DIR).rglob("*.jpg"))
    image.write_bytes(b"rotated in an image viewer")

    listing = _active(exam.store)

    assert listing.entries == ()
    assert folder.name in _reasons(listing)


def test_stray_folder_in_the_trash_does_not_empty_the_trash_list(exam):
    trash = exam.store.root / "_휴지통" / "세션"
    deleted = exam.coordinator.soft_delete(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )
    assert isinstance(deleted, Ok)
    stray = trash / "잡동사니"
    stray.mkdir()
    (stray / "IDENTITY.json").write_text("not json", encoding="utf-8")

    listing = _trash(exam.store)

    assert [entry.session_id for entry in listing.entries] == [exam.session_id]
    assert stray.name in _reasons(listing)


def test_a_folder_renamed_in_explorer_stays_listed_and_keeps_its_new_name(exam):
    renamed = exam.folder.with_name("251010_101010_이름을바꾼시험")
    exam.folder.rename(renamed)

    listing = _active(exam.store)

    assert [entry.display_folder for entry in listing.entries] == [renamed.name]
    deleted = exam.coordinator.soft_delete(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )
    assert isinstance(deleted, Ok)
    assert (exam.store.root / "_휴지통" / "세션" / renamed.name).is_dir()
    assert [entry.display_folder for entry in _trash(exam.store).entries] == [renamed.name]
    restored = exam.coordinator.restore_from_trash(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )
    assert isinstance(restored, Ok)
    assert renamed.is_dir()


def test_a_read_only_store_lists_exams_without_writing(exam):
    shutil.rmtree(exam.store.root / ".locks")
    (exam.store.root / "dashboard_index.json").unlink(missing_ok=True)
    before = sorted(path.as_posix() for path in exam.store.root.rglob("*"))
    store = SessionStore(exam.paths, read_only=True)

    listing = _active(store, index_writable=False)

    assert [entry.session_id for entry in listing.entries] == [exam.session_id]
    assert not [w for w in listing.warnings if w.code != "DASHBOARD_INDEX_STALE"]
    assert sorted(path.as_posix() for path in exam.store.root.rglob("*")) == before
    denied = SessionStore(exam.paths, read_only=True).soft_delete(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )
    assert not isinstance(denied, Ok)
    assert exam.folder.is_dir()


def _current_user_sid() -> str:
    output = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="mbcs",
    ).stdout
    return output.strip().rsplit(",", 1)[-1].strip('"')


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_exams_list_when_the_data_folder_only_allows_reading(exam):
    data = exam.store.root
    sid = _current_user_sid()
    # Simple rights such as W and D also deny SYNCHRONIZE, which blocks reading too.
    denied = f"*{sid}:(OI)(CI)(WD,AD,WEA,WA,DE,DC)"
    subprocess.run(["icacls", str(data), "/deny", denied], check=True, capture_output=True)
    try:
        listing = _active(SessionStore(exam.paths, read_only=True), index_writable=False)
    finally:
        subprocess.run(
            ["icacls", str(data), "/remove:d", f"*{sid}"], check=True, capture_output=True
        )

    assert [entry.session_id for entry in listing.entries] == [exam.session_id]


def test_permanent_delete_clears_read_only_files_and_finishes_interrupted_deletes(exam):
    data = exam.store.root
    image = next((exam.folder / SOURCE_IMAGE_DIR).rglob("*"))
    os.chmod(image, stat.S_IREAD)
    assert isinstance(
        exam.coordinator.soft_delete(
            SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
        ),
        Ok,
    )

    removed = exam.coordinator.permanently_delete(
        SessionMutationRequest(exam.session_id, exam.revision, uuid4().hex)
    )

    assert isinstance(removed, Ok)
    assert removed.value.cleanup_state is CleanupState.COMPLETE
    assert list((data / ".deleting").iterdir()) == []

    # A delete an open file interrupted earlier is finished on the next listing.
    tomb = data / ".deleting" / uuid4().hex
    (tomb / "generations").mkdir(parents=True)
    (tomb / "DELETE.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": uuid4().hex,
                "operation_id": tomb.name,
                "committed_at": "2026-10-09T00:00:00.000000Z",
                "generation_ids": [uuid4().hex],
            }
        ),
        encoding="utf-8",
    )
    _active(exam.store)
    assert not tomb.exists()


def test_books_from_before_4_0_3_are_shown_in_the_exam_folder(tmp_path):
    session = tmp_path / "exam"
    generation = session / "generations" / "g00000001_x"
    generation.mkdir(parents=True)
    legacy = "02_score_시험_260101_120000_채점결과.xlsx"
    (generation / legacy).write_bytes(b"book")
    (generation / "01_ocr_시험_260101_120000_응답결과.xlsx").write_bytes(b"responses")

    _refresh_result_view(session, generation)

    assert os.path.samefile(session / legacy, generation / legacy)
    assert not (session / "01_ocr_시험_260101_120000_응답결과.xlsx").exists()


def test_startup_drops_import_work_left_by_a_closed_program(tmp_path):
    from omr_grader.bootstrap import bootstrap
    from omr_grader.infrastructure.paths import ManagedPaths

    root = tmp_path / "root"
    old = root / ".import-old"
    fresh = root / ".import-fresh"
    for folder in (old, fresh):
        (folder / "exam").mkdir(parents=True)
    os.utime(old, (0, 0))

    assert isinstance(bootstrap(ManagedPaths.from_root(root)), Ok)

    assert not old.exists()
    assert fresh.is_dir()
