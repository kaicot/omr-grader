"""Bring exams, answer-sheet forms and settings over from an older OMR Grader folder.

Updating means unpacking the new version into a new folder; this copies what the old folder
holds. Each exam goes through the same backup and restore path as '백업하기' and '백업
복구하기', so it is validated end to end and laid out exactly like a freshly made one. The old
folder is never changed, so the user can always go back to the old version: exams are read
through a read-only store, which takes the old folder's lock files only where they exist and
creates nothing (the folder may even be read-only). A change an old program still running makes
meanwhile shows up as a failed validation of that one exam, never as a damaged copy.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    # Exams that did not come over, each with the reason in Korean.
    failed: tuple[str, ...]
    trash: int
    profiles: int
    settings: AppConfig | None
    # The old settings were read but could not be saved (only when a saver was given).
    settings_failed: bool = False


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


# (session id, or None when IDENTITY.json is unreadable; folder name; came from the old trash)
_SourceExam = tuple[str | None, str, bool]


def _source_exams(folder: Path, *, trash: bool) -> list[_SourceExam]:
    """The exam folders in ``folder``; a folder without an IDENTITY.json is not an exam."""
    if not folder.is_dir():
        return []
    found: list[_SourceExam] = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if (
            not path.is_dir()
            or path.is_symlink()
            or path.name.startswith(".")
            or path.name == "_휴지통"
        ):
            continue
        identity = path / "IDENTITY.json"
        if not identity.is_file():
            continue
        try:
            session_id = json.loads(identity.read_bytes()).get("session_id")
        except (OSError, ValueError, AttributeError):
            session_id = None
        usable = isinstance(session_id, str) and bool(session_id)
        found.append((session_id if usable else None, path.name, trash))
    return found


def _open_failure_reason(errors: tuple[ErrorInfo, ...]) -> str:
    code = errors[0].code if errors else ""
    if code == "SESSION_LOCATION_AMBIGUOUS":
        return "같은 시험이 폴더 두 곳에 있어 가져오지 않았습니다"
    if code in {"SNAPSHOT_MUTATION_IN_PROGRESS", "SESSION_BUSY_READERS"}:
        return "예전 프로그램이 쓰는 중입니다. 예전 프로그램을 끄고 다시 시도하세요"
    return "시험 자료가 손상되었거나 읽을 수 없습니다"


SaveProfile = Callable[[bytes, str], object]
SaveSettings = Callable[[AppConfig], object]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _import_profiles(
    source: Path, target: Path, save: SaveProfile | None
) -> tuple[int, dict[str, str]]:
    """Copy forms the new folder lacks; a form whose bytes are already here is skipped.

    Returns how many were copied and, per old file name, the file here that holds the same form,
    so the default form can follow it.
    """
    names: dict[str, str] = {}
    if save is None or not source.is_dir():
        return 0, names
    present: dict[str, str] = {}
    if target.is_dir():
        for existing in sorted(target.glob(f"*{PROFILE_SUFFIX}")):
            try:
                present.setdefault(_sha256(existing.read_bytes()), existing.name)
            except OSError:
                continue
    copied = 0
    for item in sorted(source.glob(f"*{PROFILE_SUFFIX}")):
        try:
            payload = item.read_bytes()
        except OSError:
            continue
        digest = _sha256(payload)
        if digest in present:
            names[item.name] = present[digest]
            continue
        saved = save(payload, item.name)
        if isinstance(saved, Ok):
            present[digest] = names[item.name] = str(getattr(saved.value, "stored_name", item.name))
            copied += 1
    return copied, names


def import_previous_install(
    chosen: str | Path,
    target_paths: ManagedPaths,
    target_store: SessionStore,
    save_profile: SaveProfile | None = None,
    report: Callable[[ImportProgress], None] | None = None,
    save_settings: SaveSettings | None = None,
) -> Result[ImportSummary]:
    """Copy the old folder's exams and forms into this one and read its settings.

    Exams already here (same internal id, e.g. from an earlier import) are skipped, so running it
    again after a partial failure is safe; so are forms whose content is already here. Exams in
    the old trash land in this folder's trash. One unreadable exam is skipped with its reason and
    the rest still come over. The old settings are saved through ``save_settings`` when given
    (and reported as imported only if that worked); otherwise they are returned for the caller.
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
    try:
        exams = _source_exams(source_paths.data_dir, trash=False)
        exams += _source_exams(source_paths.data_dir / "_휴지통" / "세션", trash=True)
    except OSError:
        return _error(
            "IMPORT_SOURCE_UNREADABLE",
            "예전 폴더를 읽을 수 없습니다. 폴더 권한을 확인하고, 예전 OMR Grader가 켜져 있으면"
            " 끈 뒤 다시 시도하세요.",
        )

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
        source_store = SessionStore(source_paths, read_only=True)
        for index, (session_id, label, in_trash) in enumerate(exams):
            if report is not None:
                report(ImportProgress(index, len(exams)))
            if session_id is None:
                failed.append(f"{label} (시험 정보를 읽을 수 없습니다)")
                continue
            archive = work / f"{index:04d}.omrbak"
            snapshot = SnapshotRequest(session_id, None, SnapshotPurpose.BACKUP)
            opened = (
                source_store.open_trash_snapshot(snapshot)
                if in_trash
                else source_store.open_committed_snapshot(snapshot)
            )
            if isinstance(opened, Err):
                failed.append(f"{label} ({_open_failure_reason(opened.errors)})")
                continue
            ref: Any = opened.value.snapshot_ref
            try:
                exported = archiver.export(opened.value, str(archive), replace=False)
            finally:
                opened.value.close()
            if isinstance(exported, Err):
                failed.append(f"{label} (백업 파일을 만들지 못했습니다)")
                continue
            validated = importer.validate_backup(BackupValidateRequest(str(archive)))
            if isinstance(validated, Err):
                failed.append(f"{label} (예전 시험 자료가 올바르지 않습니다)")
                continue
            restored = importer.restore_backup(
                RestoreCommand(validated.value, str(target_store.root), uuid4().hex)
            )
            archive.unlink(missing_ok=True)
            if isinstance(restored, Err):
                if restored.errors[0].code == "SESSION_ID_CONFLICT":
                    present += 1
                else:
                    failed.append(f"{label} (이 폴더로 복사하지 못했습니다)")
                continue
            if in_trash:
                # An exam deleted in the old folder stays in the trash here, still restorable.
                moved = target.soft_delete(
                    SessionMutationRequest(ref.session_id, ref.revision, uuid4().hex)
                )
                if isinstance(moved, Err):
                    # It is here and usable, but in the exam list instead of the trash. A re-run
                    # cannot tell this from an exam the user restored on purpose, so it is
                    # reported now instead of being moved again later.
                    failed.append(
                        f"{label} (가져왔지만 휴지통으로 옮기지 못해 시험 목록에 있습니다)"
                    )
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

    profiles, profile_names = _import_profiles(
        source_paths.profiles_dir, target_paths.profiles_dir, save_profile
    )
    settings: AppConfig | None = None
    settings_failed = False
    if source_paths.config_path.is_file():
        loaded = load_config(source_paths)
        if isinstance(loaded, Ok) and not loaded.warnings:
            settings = loaded.value
            # The same form may be stored here under another name.
            default = profile_names.get(settings.default_profile)
            if default is not None:
                settings = replace(settings, default_profile=default)
            if save_settings is not None and not isinstance(save_settings(settings), Ok):
                settings, settings_failed = None, True
    return Ok(
        ImportSummary(imported, present, tuple(failed), trashed, profiles, settings, settings_failed)
    )


__all__ = [
    "ImportProgress",
    "ImportSummary",
    "import_previous_install",
    "install_root",
]
