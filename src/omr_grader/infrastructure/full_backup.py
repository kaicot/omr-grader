"""전체 백업 and 전체 복구: every exam as its own .omrbak in one dated folder, plus forms and
settings, and the restore of such a folder into this install.

Each exam is an ordinary per-exam backup, so one damaged file costs only that exam, and the
restore is the same validated path as '백업 복구하기'.  Exams already here are skipped, so a
restore can be run again safely.  Exams in the trash are not backed up.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import BackupValidateRequest, RestoreCommand, SnapshotRequest
from omr_grader.domain.enums import SnapshotPurpose
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import DATA_FORMAT_VERSION
from omr_grader.infrastructure.backup_archive import BackupArchive
from omr_grader.infrastructure.config_store import AppConfig, load_config
from omr_grader.infrastructure.data_import import (
    PROFILE_SUFFIX,
    SaveProfile,
    SaveSettings,
    _import_profiles,
    _open_failure_reason,
    _source_exams,
)
from omr_grader.infrastructure.disk_space import MARGIN_BYTES, folder_bytes, shortfall
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore

FOLDER_PREFIX = "OMR-Grader-backup-"
EXAMS_DIR = "exams"
PROFILES_DIR = "Profiles"
INFO_FILE = "backup_info.json"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class FullBackupProgress:
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class FullRestoreProgress:
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class FullBackupSummary:
    folder: str
    exams: int
    total: int
    # Exams that were not backed up, each with the reason in Korean.
    failed: tuple[str, ...]
    profiles: int
    # session_id -> revision of every exam backed up, for the backup reminder.
    revisions: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class FullRestoreSummary:
    exams: int
    already_present: int
    failed: tuple[str, ...]
    profiles: int
    settings: AppConfig | None
    settings_failed: bool = False


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", None, context={"reason": reason}),))


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def backup_all(
    paths: ManagedPaths,
    store: SessionStore,
    destination: str | Path,
    now: datetime,
    app_version: str,
    report: Callable[[FullBackupProgress], None] | None = None,
) -> Result[FullBackupSummary]:
    """Write ``OMR-Grader-backup-<yymmdd-HHMMSS>`` under ``destination`` with every exam."""
    parent = Path(destination)
    if _inside(parent, paths.root):
        return _error(
            "FULL_BACKUP_DESTINATION",
            "프로그램 폴더 안에는 백업할 수 없습니다. 다른 드라이브나 USB의 폴더를 고르세요.",
        )
    if not parent.is_dir():
        return _error("FULL_BACKUP_DESTINATION", "고른 폴더를 찾을 수 없습니다.")
    try:
        needed = folder_bytes(paths.data_dir, skip=("_휴지통",)) + folder_bytes(paths.profiles_dir)
    except OSError:
        needed = 0  # A folder that cannot be measured is found out by the backup itself.
    short = shortfall(parent, needed + MARGIN_BYTES)
    if short is not None:
        return _error(
            "FULL_BACKUP_SPACE",
            f"백업할 드라이브에 공간이 모자랍니다. {short}. 공간을 비우거나 다른 드라이브를 고르세요.",
        )
    try:
        exams = _source_exams(paths.data_dir, trash=False)
    except OSError:
        return _error("FULL_BACKUP_FAILED", "시험 폴더를 읽을 수 없습니다.")
    folder = parent / f"{FOLDER_PREFIX}{now:%y%m%d-%H%M%S}"
    suffix = 2
    while folder.exists():
        # Never write into an earlier backup, even one made in the same second.
        folder = parent / f"{FOLDER_PREFIX}{now:%y%m%d-%H%M%S}-{suffix}"
        suffix += 1
    coordinator = SessionCommitCoordinator(store)
    archiver = BackupArchive()
    backed: list[tuple[str, int, str]] = []
    failed: list[str] = []
    try:
        (folder / EXAMS_DIR).mkdir(parents=True)
        for index, (session_id, label, _) in enumerate(exams):
            if report is not None:
                report(FullBackupProgress(index, len(exams)))
            if session_id is None:
                failed.append(f"{label} (시험 정보를 읽을 수 없습니다)")
                continue
            opened = coordinator.open_committed_snapshot(
                SnapshotRequest(session_id, None, SnapshotPurpose.BACKUP)
            )
            if isinstance(opened, Err):
                failed.append(f"{label} ({_open_failure_reason(opened.errors)})")
                continue
            revision = opened.value.snapshot_ref.revision
            name = f"{_UNSAFE.sub('_', label)}.omrbak"
            try:
                exported = archiver.export(
                    opened.value, str(folder / EXAMS_DIR / name), replace=False
                )
            finally:
                opened.value.close()
            if isinstance(exported, Err):
                failed.append(f"{label} (백업 파일을 만들지 못했습니다)")
                continue
            backed.append((session_id, revision, name))
        if report is not None:
            report(FullBackupProgress(len(exams), len(exams)))
        profiles = 0
        if paths.profiles_dir.is_dir():
            (folder / PROFILES_DIR).mkdir()
            for item in sorted(paths.profiles_dir.glob(f"*{PROFILE_SUFFIX}")):
                shutil.copy2(item, folder / PROFILES_DIR / item.name)
                profiles += 1
        if paths.config_path.is_file():
            shutil.copy2(paths.config_path, folder / "config.json")
        (folder / INFO_FILE).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "app_version": app_version,
                    "data_format": DATA_FORMAT_VERSION,
                    "created_at": now.isoformat(timespec="seconds"),
                    "exams": [
                        {"file": f"{EXAMS_DIR}/{name}", "session_id": sid, "revision": rev}
                        for sid, rev, name in backed
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return _error(
            "FULL_BACKUP_FAILED",
            f"백업하는 중 문제가 생겼습니다({exc.strerror or exc}). USB가 빠졌거나 공간이 모자랄 수"
            f" 있습니다. 이번에 만들다 만 '{folder.name}' 폴더는 지워도 됩니다.",
        )
    return Ok(
        FullBackupSummary(
            str(folder),
            len(backed),
            len(exams),
            tuple(failed),
            profiles,
            tuple((sid, rev) for sid, rev, _ in backed),
        )
    )


def _backup_format(folder: Path) -> int | None:
    try:
        value = json.loads((folder / INFO_FILE).read_text(encoding="utf-8")).get("data_format")
    except (OSError, ValueError, AttributeError):
        return None
    return value if type(value) is int else None


def backup_folder(chosen: str | Path) -> Path | None:
    """The full-backup folder for what the user picked: the folder itself or its exams folder."""
    path = Path(chosen)
    for candidate in (path, path.parent):
        if (candidate / EXAMS_DIR).is_dir() and any((candidate / EXAMS_DIR).glob("*.omrbak")):
            return candidate
    return None


def restore_all(
    chosen: str | Path,
    target_paths: ManagedPaths,
    target_store: SessionStore,
    save_profile: SaveProfile | None = None,
    report: Callable[[FullRestoreProgress], None] | None = None,
    save_settings: SaveSettings | None = None,
) -> Result[FullRestoreSummary]:
    """Restore every exam of a full-backup folder; exams already here are skipped."""
    folder = backup_folder(chosen)
    if folder is None:
        return _error(
            "FULL_RESTORE_SOURCE",
            f"전체 백업 폴더가 아닙니다. '{FOLDER_PREFIX}…' 폴더를 고르세요.",
        )
    newer = _backup_format(folder)
    if newer is not None and newer > DATA_FORMAT_VERSION:
        return _error(
            "FULL_RESTORE_NEWER",
            "더 새 버전 OMR Grader에서 만든 전체 백업이라 이 버전에서는 복구할 수 없습니다."
            " 새 버전으로 복구하세요.",
        )
    # Settings come back only into an install without exams (a new PC or a fresh folder);
    # restoring a few exams into a working install must not change its settings.
    fresh_install = not _source_exams(target_paths.data_dir, trash=False)
    archives = sorted((folder / EXAMS_DIR).glob("*.omrbak"), key=lambda item: item.name.encode())
    importer = BackupApplicationService(
        SessionCommitCoordinator(target_store),
        restore_publisher=target_store.restore_publisher(),
    )
    restored = present = 0
    failed: list[str] = []
    for index, archive in enumerate(archives):
        if report is not None:
            report(FullRestoreProgress(index, len(archives)))
        label = archive.stem
        validated = importer.validate_backup(BackupValidateRequest(str(archive)))
        if isinstance(validated, Err):
            newer = validated.errors[0].code == "BACKUP_FORMAT_NEWER"
            failed.append(
                f"{label} ({'더 새 버전에서 만든 백업입니다' if newer else '백업 파일이 손상되었습니다'})"
            )
            continue
        result = importer.restore_backup(
            RestoreCommand(validated.value, str(target_store.root), uuid4().hex)
        )
        if isinstance(result, Err):
            if result.errors[0].code == "SESSION_ID_CONFLICT":
                present += 1
            else:
                failed.append(f"{label} (이 폴더로 복구하지 못했습니다)")
            continue
        restored += 1
    if report is not None:
        report(FullRestoreProgress(len(archives), len(archives)))
    profiles, profile_names = _import_profiles(
        folder / PROFILES_DIR, target_paths.profiles_dir, save_profile
    )
    settings: AppConfig | None = None
    settings_failed = False
    if fresh_install and (folder / "config.json").is_file():
        loaded = load_config(ManagedPaths.from_root(folder))
        if isinstance(loaded, Ok) and not loaded.warnings:
            settings = loaded.value
            default = profile_names.get(settings.default_profile)
            if default is not None:
                settings = replace(settings, default_profile=default)
            if save_settings is not None and not isinstance(save_settings(settings), Ok):
                settings, settings_failed = None, True
    return Ok(
        FullRestoreSummary(restored, present, tuple(failed), profiles, settings, settings_failed)
    )


__all__ = [
    "EXAMS_DIR",
    "FOLDER_PREFIX",
    "FullBackupProgress",
    "FullBackupSummary",
    "FullRestoreProgress",
    "FullRestoreSummary",
    "backup_all",
    "backup_folder",
    "restore_all",
]
