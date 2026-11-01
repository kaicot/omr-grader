# ruff: noqa: F811 - the school fixture is imported and then requested by name.
"""전체 백업 and 전체 복구 with synthetic exams (IDs 2026000x)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import omr_grader.infrastructure.backup_archive as backup_archive
import omr_grader.infrastructure.full_backup as full_backup
from omr_grader.bootstrap import bootstrap
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.full_backup import (
    EXAMS_DIR,
    FullBackupProgress,
    backup_all,
    restore_all,
)
from omr_grader.infrastructure.grading_runtime import CommittedGradingSnapshotReader
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from tests.integration.test_student_id_fix import (  # noqa: F401 - the fixture is used by name
    MEANT,
    MISREAD,
    OTHER,
    _misread_sheet,
    _School,
    school,
)

NOW = datetime(2026, 11, 1, 10, 30, 0, tzinfo=UTC)


def _two_exams(school: _School) -> tuple[str, str]:
    first, revision = school.scan("part1", (MEANT,))
    school.grade(first, revision)
    second, revision = school.scan("part2", (MISREAD, OTHER))
    revision = school.grade(second, revision)
    fixed = school.fix(second, revision, _misread_sheet(school, second, revision), MEANT)
    assert isinstance(fixed, Ok)
    return first, second


def _fresh_install(root: Path) -> tuple[ManagedPaths, SessionStore, object]:
    root.mkdir()
    outcome = bootstrap(ManagedPaths.from_root(root))
    assert not isinstance(outcome, Err)
    paths = outcome.value.paths
    return paths, SessionStore(paths), outcome.value.capability_token


def test_a_full_backup_restores_every_exam_into_a_new_install(
    school: _School, tmp_path: Path
) -> None:
    first, second = _two_exams(school)
    usb = tmp_path / "usb"
    usb.mkdir()
    seen: list[FullBackupProgress] = []

    made = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0", seen.append)

    assert isinstance(made, Ok)
    folder = Path(made.value.folder)
    assert folder.name == "OMR-Grader-backup-261101-103000"
    assert (made.value.exams, made.value.total, made.value.failed) == (2, 2, ())
    assert len(list((folder / EXAMS_DIR).glob("*.omrbak"))) == 2
    assert made.value.profiles == len(list((folder / "Profiles").glob("*.omrtemplate"))) >= 1
    info = json.loads((folder / "backup_info.json").read_text(encoding="utf-8"))
    assert {item["session_id"] for item in info["exams"]} == {first, second}
    assert info["data_format"] == 2
    assert dict(made.value.revisions).keys() == {first, second}
    assert seen[-1] == FullBackupProgress(2, 2)

    paths, store, token = _fresh_install(tmp_path / "new")
    profiles = ProfileStore(paths, token)
    restored = restore_all(folder, paths, store, profiles.save_generated)

    assert isinstance(restored, Ok)
    assert (restored.value.exams, restored.value.already_present, restored.value.failed) == (2, 0, ())
    # A fresh install takes the backed-up settings as well.
    assert restored.value.settings is not None
    assert restored.value.profiles >= 1
    reader = CommittedGradingSnapshotReader(SessionCommitCoordinator(store))
    folders = [p for p in paths.data_dir.iterdir() if (p / "IDENTITY.json").is_file()]
    assert len(folders) == 2
    current = json.loads(
        next(p for p in folders if p.name.endswith("part2")).joinpath("CURRENT.json").read_text(
            encoding="utf-8"
        )
    )
    snapshot = reader.read_grading_snapshot(second, current["revision"])
    assert isinstance(snapshot, Ok)
    # The student-ID fix travels with the backup.
    assert {item.student_id for item in snapshot.value.shown_responses} == {MEANT, OTHER}

    again = restore_all(folder, paths, store, profiles.save_generated)
    assert isinstance(again, Ok)
    assert (again.value.exams, again.value.already_present) == (0, 2)


def test_a_drive_without_hard_links_and_a_second_backup_in_the_same_second(
    school: _School, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_exams(school)
    usb = tmp_path / "usb"
    usb.mkdir()

    def no_links(*args: object) -> None:
        raise OSError(1, "hard links are not supported")  # as on exFAT and FAT32

    monkeypatch.setattr(backup_archive.os, "link", no_links)
    first = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0")
    second = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0")

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert (first.value.exams, second.value.exams) == (2, 2)
    assert Path(second.value.folder).name == Path(first.value.folder).name + "-2"
    assert len(list((Path(first.value.folder) / EXAMS_DIR).glob("*.omrbak"))) == 2


def test_restoring_into_a_working_install_keeps_its_settings(
    school: _School, tmp_path: Path
) -> None:
    _two_exams(school)
    usb = tmp_path / "usb"
    usb.mkdir()
    made = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0")
    assert isinstance(made, Ok)
    paths, store, _ = _fresh_install(tmp_path / "new")
    only_one = tmp_path / "one"
    (only_one / EXAMS_DIR).mkdir(parents=True)
    first_archive = sorted((Path(made.value.folder) / EXAMS_DIR).glob("*.omrbak"))[0]
    (only_one / EXAMS_DIR / first_archive.name).write_bytes(first_archive.read_bytes())
    assert isinstance(restore_all(only_one, paths, store), Ok)

    restored = restore_all(made.value.folder, paths, store, save_settings=lambda settings: Ok(None))

    assert isinstance(restored, Ok)
    assert (restored.value.exams, restored.value.already_present) == (1, 1)
    assert restored.value.settings is None and not restored.value.settings_failed


def test_a_full_backup_from_a_newer_version_is_refused(school: _School, tmp_path: Path) -> None:
    _two_exams(school)
    usb = tmp_path / "usb"
    usb.mkdir()
    made = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0")
    assert isinstance(made, Ok)
    info = Path(made.value.folder) / "backup_info.json"
    info.write_text(
        json.dumps({**json.loads(info.read_text(encoding="utf-8")), "data_format": 99}),
        encoding="utf-8",
    )
    paths, store, _ = _fresh_install(tmp_path / "new")

    refused = restore_all(made.value.folder, paths, store)

    assert isinstance(refused, Err)
    assert refused.errors[0].code == "FULL_RESTORE_NEWER"


def test_a_damaged_exam_file_costs_only_that_exam(school: _School, tmp_path: Path) -> None:
    _two_exams(school)
    usb = tmp_path / "usb"
    usb.mkdir()
    made = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0")
    assert isinstance(made, Ok)
    damaged = sorted((Path(made.value.folder) / EXAMS_DIR).glob("*.omrbak"))[0]
    damaged.write_bytes(b"not a backup")

    paths, store, _ = _fresh_install(tmp_path / "new")
    restored = restore_all(made.value.folder, paths, store)

    assert isinstance(restored, Ok)
    assert restored.value.exams == 1
    assert len(restored.value.failed) == 1 and damaged.stem in restored.value.failed[0]


def test_the_backup_refuses_the_program_folder_and_a_full_drive(
    school: _School, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_exams(school)
    inside = backup_all(school.paths, SessionStore(school.paths), school.paths.root, NOW, "4.6.0")
    assert isinstance(inside, Err)
    assert "프로그램 폴더 안에는" in str(inside.errors[0].context["reason"])

    usb = tmp_path / "usb"
    usb.mkdir()
    monkeypatch.setattr(full_backup, "shortfall", lambda path, needed: "필요한 공간 약 1.0 GB / 남은 공간 10 MB")
    full = backup_all(school.paths, SessionStore(school.paths), usb, NOW, "4.6.0")
    assert isinstance(full, Err)
    assert full.errors[0].code == "FULL_BACKUP_SPACE"
    assert list(usb.iterdir()) == []


def test_a_folder_that_is_not_a_full_backup_is_refused(tmp_path: Path) -> None:
    paths, store, _ = _fresh_install(tmp_path / "new")
    (tmp_path / "random").mkdir()

    refused = restore_all(tmp_path / "random", paths, store)

    assert isinstance(refused, Err)
    assert refused.errors[0].code == "FULL_RESTORE_SOURCE"
