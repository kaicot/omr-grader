"""Bring exams, answer-sheet forms and settings over from an older OMR Grader folder.

Updating means unpacking the new version into a new folder; this copies what the old folder
holds. Each exam goes through the same backup and restore path as '백업하기' and '백업
복구하기', so it is validated end to end and laid out exactly like a freshly made one. The old
folder is never changed (only its lock files are touched while exams are read), so the user can
always go back to the old version.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import (
    BackupValidateRequest,
    RestoreCommand,
    SessionMutationRequest,
    SnapshotRequest,
)
from omr_grader.domain.enums import SnapshotPurpose
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.backup_archive import BackupArchive
from omr_grader.infrastructure.config_store import AppConfig, load_config
from omr_grader.infrastructure.data_format import DATA_FORMAT, read_data_format
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore

PROFILE_SUFFIX = ".omrtemplate"


@dataclass(frozen=True, slots=True)
class ImportSummary:
    exams: int
    already_present: int
    failed: tuple[str, ...]
    trash: int
    profiles: int
    settings: AppConfig | None


@dataclass(frozen=True, slots=True)
class ImportProgress:
    done: int
    total: int


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", None, context={"reason": reason}),))


def install_root(chosen: str | Path) -> Path | None:
    """The OMR Grader folder for what the user picked: the folder itself or its Data folder."""
    path = Path(chosen)
    for candidate in (path, path.parent):
        if (candidate / "Data").is_dir() and (
            (candidate / "config.json").is_file()
            or (candidate / "OMR Grader.exe").is_file()
            or (candidate / "Profiles").is_dir()
        ):
            return candidate
    return None


def _same_folder(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _exam_label(lease: object) -> str:
    """The exam folder name; leases point at a generation inside ``generations``."""
    root = getattr(lease, "root_path", None)
    if not root:
        return "이름 없는 시험"
    path = Path(str(root))
    return path.parent.parent.name if path.parent.name == "generations" else path.name


SaveProfile = Callable[[bytes, str], object]


def _import_profiles(source: Path, target: Path, save: SaveProfile | None) -> int:
    """Copy forms the new folder lacks; the same file name with the same bytes is skipped."""
    if save is None or not source.is_dir():
        return 0
    copied = 0
    for item in sorted(source.glob(f"*{PROFILE_SUFFIX}")):
        try:
            payload = item.read_bytes()
        except OSError:
            continue
        existing = target / item.name
        if existing.is_file():
            try:
                if existing.read_bytes() == payload:
                    continue
            except OSError:
                continue
        if isinstance(save(payload, item.name), Ok):
            copied += 1
    return copied


def import_previous_install(
    chosen: str | Path,
    target_paths: ManagedPaths,
    target_store: SessionStore,
    save_profile: SaveProfile | None = None,
    report: Callable[[ImportProgress], None] | None = None,
) -> Result[ImportSummary]:
    """Copy the old folder's exams and forms into this one and read its settings.

    Exams already here (same internal id, e.g. from an earlier import) are skipped, so running it
    again after a partial failure is safe. Exams in the old trash land in this folder's trash. The
    old settings are returned for the caller to save through the normal settings path.
    """
    source_root = install_root(chosen)
    if source_root is None:
        return _error(
            "IMPORT_SOURCE_INVALID",
            "OMR Grader 폴더가 아닙니다. 예전 OMR Grader.exe가 있는 폴더를 고르세요.",
        )
    if _same_folder(source_root, target_paths.root):
        return _error("IMPORT_SOURCE_INVALID", "지금 실행 중인 프로그램 폴더입니다. 예전 폴더를 고르세요.")
    marker = read_data_format(source_root / "Data")
    if isinstance(marker, Err):
        return marker
    if marker.value is not None and marker.value.data_format > DATA_FORMAT:
        return _error(
            "IMPORT_SOURCE_NEWER",
            "더 새 버전에서 만든 자료입니다. 이 프로그램보다 새 버전에서 가져오세요.",
        )

    source_paths = ManagedPaths.from_root(source_root)
    source_store = SessionStore(source_paths)
    active = source_store.discover_active_committed_leases()
    if isinstance(active, Err):
        return _error(
            "IMPORT_SOURCE_BUSY",
            "예전 폴더의 시험을 읽을 수 없습니다. 예전 OMR Grader가 켜져 있으면 끈 뒤 다시"
            " 시도하세요.",
        )
    # (snapshot, folder name, came from the old trash)
    exams: list[tuple[object, str, bool]] = []
    for lease in active.value:
        exams.append((lease.snapshot_ref, _exam_label(lease), False))
        lease.close()
    trash = source_store.discover_trash_committed_leases()
    if isinstance(trash, Ok):
        for lease in trash.value:
            exams.append((lease.snapshot_ref, _exam_label(lease), True))
            lease.close()

    archiver = BackupArchive()
    importer = BackupApplicationService(
        SessionCommitCoordinator(target_store),
        restore_publisher=target_store.restore_publisher(),
    )
    work = target_paths.root / f".import-{uuid4().hex}"
    imported = present = trashed = 0
    failed: list[str] = []
    target = SessionCommitCoordinator(target_store)
    try:
        work.mkdir()
        for index, (ref_object, label, in_trash) in enumerate(exams):
            ref: Any = ref_object
            if report is not None:
                report(ImportProgress(index, len(exams)))
            archive = work / f"{index:04d}.omrbak"
            snapshot = SnapshotRequest(ref.session_id, ref.revision, SnapshotPurpose.BACKUP)
            opened = (
                source_store.open_trash_snapshot(snapshot)
                if in_trash
                else source_store.open_committed_snapshot(snapshot)
            )
            if isinstance(opened, Err):
                failed.append(label)
                continue
            try:
                exported = archiver.export(opened.value, str(archive), replace=False)
            finally:
                opened.value.close()
            if isinstance(exported, Err):
                failed.append(label)
                continue
            validated = importer.validate_backup(BackupValidateRequest(str(archive)))
            if isinstance(validated, Err):
                failed.append(label)
                continue
            restored = importer.restore_backup(
                RestoreCommand(validated.value, str(target_store.root), uuid4().hex)
            )
            archive.unlink(missing_ok=True)
            if isinstance(restored, Err):
                if restored.errors[0].code == "SESSION_ID_CONFLICT":
                    present += 1
                else:
                    failed.append(label)
                continue
            if in_trash:
                # An exam deleted in the old folder stays in the trash here, still restorable.
                moved = target.soft_delete(
                    SessionMutationRequest(ref.session_id, ref.revision, uuid4().hex)
                )
                if isinstance(moved, Err):
                    failed.append(label)
                    continue
                trashed += 1
            else:
                imported += 1
        if report is not None:
            report(ImportProgress(len(exams), len(exams)))
    except OSError as exc:
        return _error("IMPORT_FAILED", f"가져오는 중 문제가 생겼습니다: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    profiles = _import_profiles(source_paths.profiles_dir, target_paths.profiles_dir, save_profile)
    settings: AppConfig | None = None
    if source_paths.config_path.is_file():
        loaded = load_config(source_paths)
        if isinstance(loaded, Ok) and not loaded.warnings:
            settings = loaded.value
    return Ok(ImportSummary(imported, present, tuple(failed), trashed, profiles, settings))


__all__ = [
    "ImportProgress",
    "ImportSummary",
    "import_previous_install",
    "install_root",
]
