"""Immutable on-disk session authority; CURRENT.json is the only visibility boundary."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from omr_grader.application.dto import (
    BackupRestoreResult,
    CommitGenerationResult,
    CorrectionSemanticView,
    GenerationMutation,
    GradingSemanticView,
    MetadataSemanticView,
    PermanentDeleteResult,
    RecognitionSemanticView,
    RecoveryIssue,
    RecoveryReport,
    RecoveryRequest,
    SessionCreateResult,
    SessionMutationRequest,
    SnapshotRef,
    SnapshotRequest,
    SoftDeleteResult,
    TrashRestoreResult,
)
from omr_grader.application.ports import CommittedSnapshotLease as CommittedSnapshotLeasePort
from omr_grader.domain.enums import (
    CleanupState,
    IndexState,
    LineageState,
    OperationKind,
    ProcessingStatus,
    SnapshotPurpose,
)
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import (
    AnswerKeySnapshot,
    CurrentPointer,
    DeleteTombstone,
    EffectiveResponse,
    IdentityRecord,
    ManifestSummary,
    RestoreProvenance,
    RosterSnapshot,
    SessionManifest,
    SessionRecord,
    SessionReservation,
    validate_portable_component,
)
from omr_grader.infrastructure.atomic_io import atomic_write_json
from omr_grader.infrastructure.backup_archive import ExtractedBackup, _path_identity
from omr_grader.infrastructure.generation_materializer import (
    GenerationMaterializationInput,
    GenerationMaterializer,
    StagingToken,
)
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.result_layout import (
    COORDINATE_DIR,
    OCR_IMAGE_DIR,
    REVIEW_DIR,
    SCORE_IMAGE_DIR,
    SOURCE_IMAGE_DIR,
)
from omr_grader.infrastructure.session_lease import (
    CommittedSnapshotLease,
    FileGateBackend,
    GateBackend,
    GateHandle,
)

FaultBarrier = Callable[[str], None]


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _error(code: str, reason: str, *, retryable: bool = False) -> Err:
    return Err(
        (ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason}, retryable=retryable),)
    )


def _warning(code: str, reason: str) -> ErrorInfo:
    return ErrorInfo(code, f"warning.{code.lower()}", context={"reason": reason})


def _wire_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if text in {"", "-0"} else text
    if isinstance(value, dict):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_wire_value(item) for item in value]
    return value


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return json.load(stream)


def _json_object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("JSON value must be an object")
    result: dict[str, object] = {}
    for raw_key, raw_item in value.items():
        key: object = raw_key
        item: object = raw_item
        if not isinstance(key, str):
            raise ValueError("JSON object keys must be strings")
        result[key] = item
    return result


def _read_json_object(path: Path) -> Mapping[str, object]:
    return _json_object(_read_json(path))


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & 0x0400)
    ):
        raise ValueError("restore gate is not an owned regular file")
    return metadata.st_dev, metadata.st_ino


class _WindowsVerifiedRestoreTree:
    """Pinned handles for the tree whose directory handle is published."""

    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _FILE_ADD_SUBDIRECTORY = 0x00000004
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_DELETE = 0x00000004
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_RENAME_INFO = 3
    _FILE_RENAME_INFO_EX = 22
    _FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002

    def __init__(
        self,
        prepared: Path,
        expected: Mapping[str, tuple[int, str]],
        expected_directories: set[str],
        provenance: RestoreProvenance,
        location_metadata: Mapping[str, object],
        prepared_identity: tuple[int, int],
        parent_identity: tuple[int, int],
    ) -> None:
        try:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._create_file = kernel32.CreateFileW
            self._create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            self._create_file.restype = wintypes.HANDLE
            self._close_handle = kernel32.CloseHandle
            self._close_handle.argtypes = (wintypes.HANDLE,)
            self._close_handle.restype = wintypes.BOOL
            self._file_information = kernel32.GetFileInformationByHandle
            self._read_file = kernel32.ReadFile
            self._read_file.argtypes = (
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            )
            self._read_file.restype = wintypes.BOOL
            self._set_file_information = kernel32.SetFileInformationByHandle
            self._set_file_information.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            self._set_file_information.restype = wintypes.BOOL

            class _ByHandleFileInformation(ctypes.Structure):
                _fields_ = (
                    ("attributes", wintypes.DWORD),
                    ("creation_time", wintypes.FILETIME),
                    ("last_access_time", wintypes.FILETIME),
                    ("last_write_time", wintypes.FILETIME),
                    ("volume_serial", wintypes.DWORD),
                    ("file_size_high", wintypes.DWORD),
                    ("file_size_low", wintypes.DWORD),
                    ("number_of_links", wintypes.DWORD),
                    ("file_index_high", wintypes.DWORD),
                    ("file_index_low", wintypes.DWORD),
                )

            self._file_information_type = _ByHandleFileInformation
            self._file_information.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_ByHandleFileInformation),
            )
            self._file_information.restype = wintypes.BOOL
        except (AttributeError, OSError):
            raise OSError(errno.ENOTSUP, "required Win32 handle APIs are unavailable") from None

        self._handles: list[int] = []
        self._identities: list[tuple[int, tuple[int, int], bool]] = []
        self._prepared = prepared
        self._root_handle = 0
        self._parent_handle = 0
        try:
            if (
                _path_identity(prepared) != prepared_identity
                or _path_identity(prepared.parent) != parent_identity
            ):
                raise ValueError("restored staging ownership changed before pinning")
            self._root_handle, _root_identity = self._open_directory(
                prepared, self._GENERIC_READ | self._DELETE
            )
            self._parent_handle, _parent_identity = self._open_directory(
                prepared.parent, self._GENERIC_READ
            )
            if (
                _path_identity(prepared) != prepared_identity
                or _path_identity(prepared.parent) != parent_identity
            ):
                raise ValueError("restored staging path-to-handle binding changed")
            actual_files: set[str] = set()
            actual_directories: set[str] = set()
            for parent, directories, files in os.walk(prepared, followlinks=False):
                relative_parent = Path(parent).relative_to(prepared)
                for name in directories:
                    relative = (relative_parent / name).as_posix()
                    self._open_directory(
                        prepared.joinpath(*relative.split("/")), self._GENERIC_READ
                    )
                    actual_directories.add(relative)
                for name in files:
                    relative = (relative_parent / name).as_posix()
                    handle, attributes, identity = self._open_file(
                        prepared.joinpath(*relative.split("/"))
                    )
                    if attributes & (
                        self._FILE_ATTRIBUTE_REPARSE_POINT | self._FILE_ATTRIBUTE_DIRECTORY
                    ):
                        raise ValueError("staged restore contains an unsafe file")
                    captured = relative in {"LOCATION.json", "RESTORE_PROVENANCE.json"}
                    size_read, digest, contents = self._read_digest(handle, capture=captured)
                    current_attributes, current_identity, size = self._info(handle)
                    if (
                        current_identity != identity
                        or current_attributes != attributes
                        or size != size_read
                    ):
                        raise ValueError("staged restore file identity changed")
                    actual_files.add(relative)
                    expected_entry = expected.get(relative)
                    if expected_entry is not None and (
                        size_read != expected_entry[0] or digest != expected_entry[1]
                    ):
                        raise ValueError("staged restore bytes differ from archive entries")
                    if relative == "LOCATION.json":
                        if _json_object(json.loads(contents)) != dict(location_metadata):
                            raise ValueError("staged restore location metadata changed")
                    elif relative == "RESTORE_PROVENANCE.json":
                        restored_provenance = RestoreProvenance.from_dict(
                            _json_object(json.loads(contents))
                        )
                        if restored_provenance != provenance:
                            raise ValueError("staged restore provenance changed")
            if actual_files != set(expected) | {"LOCATION.json", "RESTORE_PROVENANCE.json"} or (
                actual_directories != expected_directories
            ):
                raise ValueError("staged restore file layout is not exact")
        except BaseException:
            self.close()
            raise

    def _info(self, handle: int) -> tuple[int, tuple[int, int], int]:
        result = self._file_information_type()
        if not self._file_information(handle, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = (
            int(result.volume_serial),
            (int(result.file_index_high) << 32) | int(result.file_index_low),
        )
        size = (int(result.file_size_high) << 32) | int(result.file_size_low)
        return int(result.attributes), identity, size

    def _open_directory(
        self, path: Path, access: int, *, share_delete: bool = False
    ) -> tuple[int, tuple[int, int]]:
        handle = self._create_file(
            str(path),
            access,
            self._FILE_SHARE_READ | (self._FILE_SHARE_DELETE if share_delete else 0),
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handles.append(handle)
        attributes, identity, _size = self._info(handle)
        if attributes & self._FILE_ATTRIBUTE_REPARSE_POINT or not (
            attributes & self._FILE_ATTRIBUTE_DIRECTORY
        ):
            raise ValueError("staged restore contains a reparse point")
        self._identities.append((handle, identity, True))
        return handle, identity

    def _open_file(self, path: Path) -> tuple[int, int, tuple[int, int]]:
        handle = self._create_file(
            str(path),
            self._GENERIC_READ,
            self._FILE_SHARE_READ,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handles.append(handle)
        attributes, identity, _size = self._info(handle)
        self._identities.append((handle, identity, False))
        return handle, attributes, identity

    def _read_digest(self, handle: int, *, capture: bool) -> tuple[int, str, bytes]:
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        size = 0
        while True:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            read = ctypes.c_uint32()
            if not self._read_file(handle, buffer, len(buffer), ctypes.byref(read), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not read.value:
                return size, digest.hexdigest(), b"".join(chunks)
            chunk = buffer.raw[: read.value]
            size += len(chunk)
            digest.update(chunk)
            if capture:
                chunks.append(chunk)

    def _verify_pinned_identities(self) -> None:
        for handle, identity, directory in self._identities:
            attributes, current, _size = self._info(handle)
            if (
                current != identity
                or attributes & self._FILE_ATTRIBUTE_REPARSE_POINT
                or bool(attributes & self._FILE_ATTRIBUTE_DIRECTORY) != directory
            ):
                raise ValueError("staged restore handle identity changed")

    def publish(self, target: Path) -> tuple[ErrorInfo, ...]:
        if target.parent != self._prepared.parent:
            raise ValueError("publication source and target must share a pinned parent")
        name = str(target).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(22 + len(name))
        ctypes.memset(buffer, 0, len(buffer))
        ctypes.memmove(
            ctypes.addressof(buffer),
            ctypes.byref(ctypes.c_uint32(self._FILE_RENAME_FLAG_POSIX_SEMANTICS)),
            4,
        )
        ctypes.memmove(ctypes.addressof(buffer) + 20, name, len(name))
        ctypes.memmove(ctypes.addressof(buffer) + 16, ctypes.byref(ctypes.c_uint32(len(name))), 4)
        self._verify_pinned_identities()
        for handle in reversed(self._handles[1:]):
            self._close_handle(handle)
        self._handles = self._handles[:1]
        self._identities = self._identities[:1]
        self._parent_handle = 0
        if not self._set_file_information(
            self._root_handle, self._FILE_RENAME_INFO_EX, buffer, len(buffer)
        ):
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise FileExistsError(error, "restore target already exists", str(target))
            raise ctypes.WinError(error)
        try:
            _attributes, published_identity, _size = self._info(self._root_handle)
            _published_handle, reopened_identity = self._open_directory(
                target, self._GENERIC_READ, share_delete=True
            )
            if reopened_identity != published_identity:
                raise ValueError("published restore identity changed")
        except (OSError, ValueError) as exc:
            return (
                _warning(
                    "POSTCOMMIT_RECOVERY_REQUIRED",
                    f"restore 게시 후 확인 작업을 완료하지 못했습니다: {exc}",
                ),
            )
        return ()

    def close(self) -> None:
        self._identities.clear()
        while self._handles:
            self._close_handle(self._handles.pop())
        self._root_handle = 0
        self._parent_handle = 0

    def __enter__(self) -> _WindowsVerifiedRestoreTree:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _move_directory_no_replace(source: Path, target: Path) -> None:
    """Move an internal staging directory before it becomes restore authority."""
    if source.parent != target.parent:
        raise ValueError("publication source and target must share a pinned parent")
    if os.name == "nt":
        if not ctypes.windll.kernel32.MoveFileW(str(source), str(target)):
            raise ctypes.WinError()
        return
    _publish_directory_no_replace(source, target)


def _publish_directory_no_replace(
    source: Path, target: Path, verified_tree: _WindowsVerifiedRestoreTree | None = None
) -> tuple[ErrorInfo, ...]:
    """Atomically publish a verified directory only while its destination name is absent."""
    if source.parent != target.parent:
        raise ValueError("publication source and target must share a pinned parent")
    if os.name == "nt":
        if verified_tree is None:
            raise OSError(errno.ENOTSUP, "Win32 verified-tree publication is unavailable")
        return verified_tree.publish(target)
    parent_fd = os.open(
        source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "renameat2(RENAME_NOREPLACE) is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if renameat2(parent_fd, os.fsencode(source.name), parent_fd, os.fsencode(target.name), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
    finally:
        os.close(parent_fd)
    return ()


def _verify_restored_staging(
    extracted: ExtractedBackup,
    prepared: Path,
    provenance: RestoreProvenance,
    expected_location: Mapping[str, object],
    prepared_identity: tuple[int, int],
    prepared_parent_identity: tuple[int, int],
    *,
    pin_for_publication: bool = False,
) -> _WindowsVerifiedRestoreTree | None:
    """Bind the exact staged bytes and durable provenance to inspected archive entries."""
    if (
        _path_identity(prepared) != prepared_identity
        or _path_identity(prepared.parent) != prepared_parent_identity
    ):
        raise ValueError("restored staging ownership changed")
    if (
        extracted.source_identity is None
        or provenance.session_id != extracted.identity.session_id
        or provenance.archive_sha256 != extracted.archive_sha256
        or provenance.boundary_revision != extracted.manifest.revision
        or provenance.boundary_generation_id != extracted.manifest.generation_id
        or provenance.boundary_manifest_sha256 != extracted.current.manifest_sha256
        or provenance.omitted_parent != extracted.archive_manifest.omitted_parent
    ):
        raise ValueError("restore provenance does not bind the inspected archive")
    expected: dict[str, tuple[int, str]] = {}
    for entry in extracted.archive_manifest.entries:
        prefix = "omrbak-v1/"
        if not entry.path.startswith(prefix):
            raise ValueError("archive entry path is invalid")
        expected[entry.path.removeprefix(prefix)] = (entry.size, entry.sha256)
    if len(expected) != len(extracted.archive_manifest.entries):
        raise ValueError("archive entry paths are not unique")
    expected_files = set(expected)
    expected_files.update({"LOCATION.json", "RESTORE_PROVENANCE.json"})
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    expected_directories = {
        parent.as_posix()
        for relative in expected_files
        for parent in Path(relative).parents
        if parent != Path(".")
    }
    for parent, directories, files in os.walk(prepared, followlinks=False):
        relative_parent = Path(parent).relative_to(prepared)
        if any(_is_reparse_point(Path(parent) / name) for name in directories + files):
            raise ValueError("staged restore contains a reparse point")
        actual_directories.update((relative_parent / name).as_posix() for name in directories)
        actual_files.update((relative_parent / name).as_posix() for name in files)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("staged restore file layout is not exact")
    for relative, (size, digest) in expected.items():
        path = prepared.joinpath(*relative.split("/"))
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != size
            or _sha(path) != digest
        ):
            raise ValueError("staged restore bytes differ from archive entries")
    root_names = {path.name for path in prepared.iterdir()}
    required_root_names = {
        "IDENTITY.json",
        "CURRENT.json",
        "LOCATION.json",
        "RESTORE_PROVENANCE.json",
        "generations",
    }
    if not required_root_names.issubset(root_names) or any(
        name not in required_root_names
        and name not in _RESULT_VIEW_NAMES
        and not name.startswith(_RESULT_VIEW_PREFIXES)
        for name in root_names
    ):
        raise ValueError("staged restore root layout is not exact")
    if (
        RestoreProvenance.from_dict(_read_json_object(prepared / "RESTORE_PROVENANCE.json"))
        != provenance
    ):
        raise ValueError("staged restore provenance changed")
    if _read_json_object(prepared / "LOCATION.json") != dict(expected_location):
        raise ValueError("staged restore location metadata changed")

    if (
        _path_identity(prepared) != prepared_identity
        or _path_identity(prepared.parent) != prepared_parent_identity
    ):
        raise ValueError("restored staging ownership changed")
    if os.name == "nt" and pin_for_publication:
        return _WindowsVerifiedRestoreTree(
            prepared,
            expected,
            expected_directories,
            provenance,
            expected_location,
            prepared_identity,
            prepared_parent_identity,
        )
    return None


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x0400
    )


def _semantic_sha(
    value: RecognitionSemanticView
    | CorrectionSemanticView
    | GradingSemanticView
    | MetadataSemanticView
    | AnswerKeySnapshot
    | RosterSnapshot,
) -> str:
    payload = json.dumps(
        _wire_value(asdict(value)), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_LIFECYCLE_OPERATIONS = frozenset(
    (OperationKind.CORRECT, OperationKind.REGRADE, OperationKind.FINALIZE)
)


_CONTROL_FILES = frozenset(("session.json", "manifest.json"))
_RESULT_VIEW_DIRS = (
    ("images", SOURCE_IMAGE_DIR),
    (OCR_IMAGE_DIR, OCR_IMAGE_DIR),
    (SCORE_IMAGE_DIR, SCORE_IMAGE_DIR),
    (COORDINATE_DIR, COORDINATE_DIR),
    (REVIEW_DIR, REVIEW_DIR),
)
_RESULT_VIEW_NAMES = frozenset(target for _, target in _RESULT_VIEW_DIRS)
_RESULT_VIEW_PREFIXES = ("01_ocr_", "02_score_", "03_final_")


def _is_control_file(path: str) -> bool:
    return path in _CONTROL_FILES


def _refresh_result_view(session: Path, generation: Path) -> None:
    """Publish browse-friendly hard links for the immutable current generation."""
    for _, target_name in _RESULT_VIEW_DIRS:
        target = session / target_name
        if target.exists():
            shutil.rmtree(target)
    for child in tuple(session.iterdir()):
        if child.is_file() and child.name.startswith(_RESULT_VIEW_PREFIXES):
            child.unlink()
    for source_name, target_name in _RESULT_VIEW_DIRS:
        source = generation / source_name
        if source.is_dir():
            shutil.copytree(source, session / target_name, copy_function=os.link)
    for child in generation.iterdir():
        if child.is_file() and child.name.startswith(_RESULT_VIEW_PREFIXES):
            os.link(child, session / child.name)


def _preserved_artifact(path: str, operation: OperationKind) -> bool:
    """Carry only compact, user-recoverable inputs into a lifecycle generation."""
    if operation not in _LIFECYCLE_OPERATIONS:
        return True
    if path.startswith("images/") or path.startswith("01_ocr_"):
        return True
    return operation is OperationKind.FINALIZE and path.startswith(
        "02_score_"
    )


class SessionStore:
    """Main-process store.  Call through :class:`SessionCommitCoordinator` for writes."""

    def __init__(
        self,
        paths: ManagedPaths | Path,
        *,
        gate_backend: GateBackend | None = None,
        fault_barrier: FaultBarrier | None = None,
        materializer: GenerationMaterializer | None = None,
    ) -> None:
        self._root = paths.data_dir if isinstance(paths, ManagedPaths) else Path(paths)
        self._gates = gate_backend or FileGateBackend()
        self._barrier = fault_barrier or (lambda _name: None)
        self._materializer = materializer or GenerationMaterializer()

    @property
    def root(self) -> Path:
        return self._root

    def restore_publisher(self) -> _SessionStoreRestorePublisher:
        """Return the only authority permitted to publish a restored session."""
        return _SessionStoreRestorePublisher(self)

    def _active(self) -> Path:
        return self._root

    def _trash(self) -> Path:
        return self._root / "_휴지통" / "세션"

    def _locks(self) -> Path:
        return self._root / ".locks"

    def _gate_path(self, session_id: str, generation_id: str) -> Path:
        return self._locks() / "lifetime" / session_id / f"{generation_id}.gate"

    def _reservation(self, session_id: str) -> Path:
        return self._root / ".reservations" / f"{session_id}.json"

    def _deleting(self) -> Path:
        return self._root / ".deleting"

    def _location_metadata(
        self, session_id: str, display_name: str, operation_id: str
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "display_name": display_name,
            "operation_id": operation_id,
        }

    def _display_path(self, display_name: str) -> Path:
        """Resolve one display component without permitting managed-root escapes."""
        validate_portable_component(display_name)
        root = self._root.resolve(strict=False)
        if self._root.exists() and _is_reparse_point(self._root):
            raise ValueError("session root may not be a reparse point")
        candidate = self._root / display_name
        if candidate.exists() and _is_reparse_point(candidate):
            raise ValueError("session display location may not be a reparse point")
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise ValueError("session display location escapes the managed root") from exc
        return candidate

    def _display_name(self, session: Path, session_id: str) -> str:
        payload = _read_json_object(session / "LOCATION.json")
        display_name = payload.get("display_name")
        if (
            payload.get("schema_version") != 1
            or payload.get("session_id") != session_id
            or not isinstance(display_name, str)
        ):
            raise ValueError("durable session display location is invalid")
        self._display_path(display_name)
        return display_name

    def _root_lock(self) -> Path:
        return self._locks() / "root-identity.lock"

    def _writer_lock(self, session_id: str) -> Path:
        return self._locks() / "session" / f"{session_id}.lock"

    def _mkdirs(self) -> None:
        for directory in (
            self._root,
            self._trash(),
            self._locks() / "lifetime",
            self._locks() / "session",
            self._root / ".reservations",
            self._deleting(),
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._root_lock().touch(exist_ok=True)

    def _lock(self, path: Path, *, exclusive: bool, busy: str) -> Result[GateHandle]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        handle = self._gates.acquire(path, exclusive=exclusive, blocking=False)
        if handle is None:
            return _error(busy, "다른 작업이 같은 세션을 사용 중입니다.", retryable=True)
        return Ok(handle)

    def _existing_lock(self, path: Path, *, exclusive: bool, busy: str) -> Result[GateHandle]:
        if not path.is_file() or path.is_symlink():
            return _error("GENERATION_GATE_MISSING", "generation gate가 없습니다.")
        handle = self._gates.acquire(path, exclusive=exclusive, blocking=False)
        if handle is None:
            if not path.is_file() or path.is_symlink():
                return _error("GENERATION_GATE_MISSING", "generation gate가 없습니다.")
            return _error(busy, "다른 작업이 같은 세션을 사용 중입니다.", retryable=True)
        return Ok(handle)

    def _locate(self, session_id: str, *, trash: bool | None = None) -> Path | None:
        locations: list[Path] = []
        if trash is not True:
            locations.extend(
                path
                for path in self._active().iterdir()
                if path.is_dir() and not path.name.startswith(".") and path.name != "_휴지통"
            ) if self._root.exists() else []
        if trash is not False:
            locations.append(self._trash() / session_id)
        found: list[Path] = []
        for path in locations:
            identity = path / "IDENTITY.json"
            try:
                if (
                    identity.exists()
                    and IdentityRecord.from_dict(_read_json_object(identity)).session_id
                    == session_id
                ):
                    found.append(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        if len(found) > 1:
            raise ValueError("session exists in active and trash locations")
        return found[0] if found else None

    def _manifest(self, generation: Path) -> tuple[SessionManifest, str]:
        manifest_path = generation / "manifest.json"
        payload = manifest_path.read_bytes()
        return SessionManifest.from_dict(_json_object(json.loads(payload))), hashlib.sha256(
            payload
        ).hexdigest()

    def _pointer(self, session: Path) -> CurrentPointer:
        return CurrentPointer.from_dict(_read_json_object(session / "CURRENT.json"))

    def _validate_current(
        self, session: Path, requested_revision: int | None = None
    ) -> tuple[CurrentPointer, SessionManifest, Path]:
        identity = IdentityRecord.from_dict(_read_json_object(session / "IDENTITY.json"))
        pointer = self._pointer(session)
        if identity.session_id != pointer.session_id or (
            requested_revision is not None and pointer.revision != requested_revision
        ):
            raise ValueError("pointer identity or requested revision mismatch")
        generation = session.joinpath(*pointer.generation_relpath.split("/"))
        if generation.parent != session / "generations" or not generation.is_dir():
            raise ValueError("pointer generation path invalid")
        manifest, digest = self._manifest(generation)
        record = SessionRecord.from_dict(_read_json_object(generation / "session.json"))
        if (
            digest != pointer.manifest_sha256
            or manifest.session_id != pointer.session_id
            or manifest.revision != pointer.revision
            or manifest.generation_id != pointer.generation_id
            or record.session_id != identity.session_id
            or record.revision != pointer.revision
            or record.state is not manifest.state
        ):
            raise ValueError(
                "pointer, manifest, and session record must share identity, revision, and state"
            )
        # The manifest is the complete allowlist; control files are intentionally excluded.
        if any(_is_control_file(entry.path) for entry in manifest.files):
            raise ValueError("manifest must not allowlist generation control files")
        for entry in manifest.files:
            file = generation.joinpath(*entry.path.split("/"))
            if (
                not file.is_file()
                or file.is_symlink()
                or file.stat().st_size != entry.size
                or _sha(file) != entry.sha256
            ):
                raise ValueError("manifest file validation failed")
        expected_files = {entry.path for entry in manifest.files}
        expected_dirs = {
            parent.as_posix()
            for path in expected_files | {"manifest.json", "session.json"}
            for parent in Path(path).parents
            if parent != Path(".")
        }
        actual_files: set[str] = set()
        actual_dirs: set[str] = set()
        for item in generation.rglob("*"):
            relative = item.relative_to(generation).as_posix()
            if _is_reparse_point(item):
                raise ValueError("generation tree contains a reparse point")
            if item.is_file():
                actual_files.add(relative)
            elif item.is_dir():
                actual_dirs.add(relative)
            else:
                raise ValueError("generation tree contains an unsupported entry")
        if (
            actual_files != expected_files | {"manifest.json", "session.json"}
            or actual_dirs != expected_dirs
        ):
            raise ValueError("generation tree does not exactly match its manifest")
        self._validate_lineage(session, manifest)
        return pointer, manifest, generation

    def _validate_lineage(self, session: Path, current: SessionManifest) -> None:
        """Require every locally retained parent manifest to authenticate the current head."""
        generations = session / "generations"
        by_id: dict[str, tuple[SessionManifest, str]] = {}
        for candidate in generations.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                raise ValueError("published generation path invalid")
            manifest, digest = self._manifest(candidate)
            if manifest.session_id != current.session_id or manifest.generation_id in by_id:
                raise ValueError("published generation lineage identity invalid")
            by_id[manifest.generation_id] = (manifest, digest)

        manifest = current
        while manifest.revision > 1:
            parent_id = manifest.parent_generation_id
            parent_revision = manifest.parent_revision
            parent_digest = manifest.parent_manifest_sha256
            if parent_id is None or parent_revision is None or parent_digest is None:
                raise ValueError("generation parent lineage is incomplete")
            parent = by_id.get(parent_id)
            if parent is None:
                if not (
                    self._is_authenticated_restore_boundary(
                        session, manifest, parent_id, parent_digest
                    )
                    or self._is_authenticated_retention_boundary(
                        session, manifest, parent_id, parent_digest
                    )
                ):
                    raise ValueError(
                        "generation parent lineage is truncated without an authenticated boundary"
                    )
                return
            parent_manifest, digest = parent
            if (
                digest != parent_digest
                or parent_manifest.revision != parent_revision
                or parent_manifest.revision != manifest.revision - 1
            ):
                raise ValueError("generation parent lineage does not authenticate")
            manifest = parent_manifest
        if manifest.parent_revision is not None:
            raise ValueError("generation one lineage boundary is invalid")

    def _is_authenticated_restore_boundary(
        self, session: Path, boundary: SessionManifest, parent_id: str, parent_digest: str
    ) -> bool:
        try:
            provenance = RestoreProvenance.from_dict(
                _read_json_object(session / "RESTORE_PROVENANCE.json")
            )
            _, boundary_digest = self._manifest(
                session / "generations" / f"g{boundary.revision:08d}_{boundary.generation_id}"
            )
            omitted = provenance.omitted_parent
            return (
                provenance.session_id == boundary.session_id
                and provenance.boundary_revision == boundary.revision
                and provenance.boundary_generation_id == boundary.generation_id
                and provenance.boundary_manifest_sha256 == boundary_digest
                and omitted is not None
                and omitted.generation_id == parent_id
                and omitted.revision == boundary.parent_revision
                and omitted.manifest_sha256 == parent_digest
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _is_authenticated_retention_boundary(
        self, session: Path, boundary: SessionManifest, parent_id: str, parent_digest: str
    ) -> bool:
        try:
            payload = _read_json_object(session / "RETENTION.json")
            omitted = payload["omitted_parent"]
            if not isinstance(omitted, Mapping):
                return False
            _, boundary_digest = self._manifest(
                session / "generations" / f"g{boundary.revision:08d}_{boundary.generation_id}"
            )
            return (
                set(payload)
                == {
                    "schema_version",
                    "session_id",
                    "boundary_revision",
                    "boundary_generation_id",
                    "boundary_manifest_sha256",
                    "omitted_parent",
                    "retained_at",
                }
                and payload["schema_version"] == 1
                and payload["session_id"] == boundary.session_id
                and payload["boundary_revision"] == boundary.revision
                and payload["boundary_generation_id"] == boundary.generation_id
                and payload["boundary_manifest_sha256"] == boundary_digest
                and omitted.get("generation_id") == parent_id
                and omitted.get("revision") == boundary.parent_revision
                and omitted.get("manifest_sha256") == parent_digest
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def open_committed_snapshot(self, request: SnapshotRequest) -> Result[CommittedSnapshotLease]:
        self._mkdirs()
        for _ in range(3):
            try:
                session = self._locate(request.session_id, trash=False)
                self._locate(request.session_id)
            except ValueError as exc:
                return _error("SESSION_LOCATION_AMBIGUOUS", str(exc))
            if session is None:
                return _error("SESSION_NOT_FOUND", "활성 세션을 찾을 수 없습니다.")
            try:
                pointer = self._pointer(session)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return _error("SESSION_POINTER_INVALID", str(exc))
            if request.revision is not None and request.revision != pointer.revision:
                return _error(
                    "SESSION_REVISION_NOT_FOUND", "요청한 revision이 현재 revision이 아닙니다."
                )
            gate = self._existing_lock(
                self._gate_path(pointer.session_id, pointer.generation_id),
                exclusive=False,
                busy="SNAPSHOT_MUTATION_IN_PROGRESS",
            )
            if isinstance(gate, Err):
                return gate
            try:
                pinned, manifest, generation = self._validate_current(session, request.revision)
                if pinned != pointer:
                    gate.value.close()
                    continue
                return Ok(
                    CommittedSnapshotLease(
                        SnapshotRef(
                            pinned.session_id,
                            pinned.revision,
                            pinned.generation_id,
                            pinned.manifest_sha256,
                        ),
                        manifest,
                        generation,
                        gate.value,
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                gate.value.close()
                return _error("SESSION_COMMITTED_GENERATION_INVALID", str(exc))
        return _error("SNAPSHOT_MUTATION_IN_PROGRESS", "스냅샷이 변경 중입니다.", retryable=True)

    def discover_active_committed_leases(
        self,
    ) -> Result[tuple[CommittedSnapshotLeasePort, ...]]:
        """Open one pinned CURRENT lease per active session for disposable projections."""
        self._mkdirs()
        root = self._lock(self._root_lock(), exclusive=False, busy="SESSION_DISCOVERY_IN_PROGRESS")
        if isinstance(root, Err):
            return root
        leases: list[CommittedSnapshotLeasePort] = []
        completed = False
        try:
            candidates = sorted(
                (
                    path
                    for path in self._active().iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and not path.name.startswith(".")
                    and path.name != "_휴지통"
                ),
                key=lambda path: path.name.encode("utf-8"),
            )
            for session in candidates:
                try:
                    identity = IdentityRecord.from_dict(
                        _read_json_object(session / "IDENTITY.json")
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    return _error("SESSION_DISCOVERY_INVALID", str(exc))
                reader = self._lock(
                    self._writer_lock(identity.session_id),
                    exclusive=False,
                    busy="SESSION_DISCOVERY_IN_PROGRESS",
                )
                if isinstance(reader, Err):
                    return reader
                try:
                    opened = self.open_committed_snapshot(
                        SnapshotRequest(identity.session_id, None, SnapshotPurpose.COMBINED)
                    )
                    if isinstance(opened, Err):
                        return opened
                    leases.append(opened.value)
                finally:
                    reader.value.close()
            completed = True
            return Ok(tuple(leases))
        except OSError as exc:
            return _error("SESSION_DISCOVERY_FAILED", str(exc))
        finally:
            root.value.close()
            if not completed:
                for lease in reversed(leases):
                    lease.close()

    def _all_gates_exclusive(self, session: Path) -> Result[list[GateHandle]]:
        try:
            pointer, _, _ = self._validate_current(session)
            published: list[tuple[str, Path]] = []
            for generation in (session / "generations").iterdir():
                if not generation.is_dir() or generation.is_symlink():
                    raise ValueError("published generation path invalid")
                manifest, _ = self._manifest(generation)
                record = SessionRecord.from_dict(_read_json_object(generation / "session.json"))
                if (
                    manifest.session_id != pointer.session_id
                    or record.session_id != pointer.session_id
                    or record.revision != manifest.revision
                    or record.state is not manifest.state
                ):
                    raise ValueError("published generation identity, revision, or state invalid")
                published.append((manifest.generation_id, generation))
            if not published:
                raise ValueError("session has no published generations")
            if len({generation_id for generation_id, _ in published}) != len(published):
                raise ValueError("published generation IDs must be unique")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _error("SESSION_POINTER_INVALID", str(exc))
        handles: list[GateHandle] = []
        for generation_id, _ in sorted(published):
            gate = self._existing_lock(
                self._gate_path(pointer.session_id, generation_id),
                exclusive=True,
                busy="SESSION_BUSY_READERS",
            )
            if isinstance(gate, Err):
                for handle in reversed(handles):
                    handle.close()
                return gate
            handles.append(gate.value)
        return Ok(handles)

    def _derive_generation(
        self,
        parent: SessionManifest,
        old: SessionRecord,
        mutation: GenerationMutation,
        revision: int,
    ) -> tuple[
        SessionRecord,
        tuple[
            tuple[str, ...],
            str | None,
            str,
            str,
            str | None,
            str | None,
            ManifestSummary,
        ],
    ]:
        """Validate the closed semantic view and derive all next-generation truth."""
        semantic = mutation.semantic_inputs
        updated_at = _utc()
        graded_at = old.graded_at
        if graded_at is None and mutation.target_state.value in {"graded", "finalized"}:
            graded_at = updated_at
        record = SessionRecord(
            1,
            old.session_id,
            revision,
            mutation.target_state,
            old.exam_name,
            old.exam_year,
            old.exam_term,
            old.created_at,
            graded_at,
            updated_at,
        )
        fields: tuple[
            tuple[str, ...],
            str | None,
            str,
            str,
            str | None,
            str | None,
            ManifestSummary,
        ]
        if mutation.operation_kind is OperationKind.RECOGNIZE:
            if type(semantic) is not RecognitionSemanticView or semantic.state is not parent.state:
                raise ValueError("recognition semantic view or source state mismatch")
            if any(page.page_ref.session_id != mutation.session_id for page in semantic.pages):
                raise ValueError("recognition page belongs to another session")
            base_ids = tuple(
                sorted(
                    {
                        page.page_ref.work_item_id
                        for page in semantic.pages
                        if page.processing_status
                        not in {ProcessingStatus.FAILED, ProcessingStatus.UNPROCESSABLE}
                    },
                    key=str.encode,
                )
            )
            processable = sum(
                page.processing_status is ProcessingStatus.PROCESSED for page in semantic.pages
            )
            manual = sum(
                page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW
                for page in semantic.pages
            )
            fields = (
                base_ids,
                semantic.profile_sha256,
                _semantic_sha(semantic.roster),
                parent.key_sha256,
                parent.threshold_version,
                parent.threshold_sha256,
                ManifestSummary(len(base_ids), processable, manual, None),
            )
        elif mutation.operation_kind is OperationKind.CORRECT:
            if type(semantic) is not CorrectionSemanticView or semantic.state is not parent.state:
                raise ValueError("correction semantic view or source state mismatch")
            if any(
                item.work_item_id not in parent.base_response_ids for item in semantic.corrections
            ):
                raise ValueError("correction target is not a committed base response")
            fields = (
                parent.base_response_ids,
                parent.profile_sha256,
                parent.roster_sha256,
                parent.key_sha256,
                parent.threshold_version,
                parent.threshold_sha256,
                parent.summary,
            )
        elif mutation.operation_kind is OperationKind.REGRADE:
            if type(semantic) is not GradingSemanticView or semantic.state is not parent.state:
                raise ValueError("regrade semantic view or source state mismatch")
            fields = self._grading_manifest_fields(parent, semantic)
        elif mutation.operation_kind is OperationKind.FINALIZE:
            if type(semantic) is not GradingSemanticView or semantic.state is not parent.state:
                raise ValueError("finalize semantic view or source state mismatch")
            fields = self._grading_manifest_fields(parent, semantic)
        elif mutation.operation_kind is OperationKind.METADATA_EDIT:
            if (
                type(semantic) is not MetadataSemanticView
                or semantic.session.state is not parent.state
                or semantic.session.created_at != old.created_at
                or semantic.session.graded_at != old.graded_at
            ):
                raise ValueError("metadata semantic view or source state mismatch")
            record = semantic.session
            fields = (
                parent.base_response_ids,
                parent.profile_sha256,
                parent.roster_sha256,
                parent.key_sha256,
                parent.threshold_version,
                parent.threshold_sha256,
                parent.summary,
            )
        else:
            raise ValueError("unsupported generation operation")
        if (
            record.session_id != mutation.session_id
            or record.revision != revision
            or record.state is not mutation.target_state
        ):
            raise ValueError("semantic session record identity, revision, or state mismatch")
        return record, fields

    def _grading_manifest_fields(
        self, parent: SessionManifest, semantic: GradingSemanticView
    ) -> tuple[
        tuple[str, ...],
        str | None,
        str,
        str,
        str | None,
        str | None,
        ManifestSummary,
    ]:
        if semantic.scores is None:
            summary = parent.summary
        else:
            scored = sum(row.score is not None for row in semantic.scores.rows)
            summary = ManifestSummary(
                len(semantic.scores.rows),
                scored,
                len(semantic.scores.rows) - scored,
                format(semantic.scores.maximum_score, "f"),
            )
        return (
            parent.base_response_ids,
            parent.profile_sha256,
            parent.roster_sha256,
            _semantic_sha(semantic.answer_key),
            parent.threshold_version,
            parent.threshold_sha256,
            summary,
        )

    def _canonical_manifest_fields(
        self, parent: SessionManifest, generation: Path
    ) -> tuple[tuple[str, ...], str | None, str, str, str | None, str | None, ManifestSummary]:
        """Derive manifest authority from the canonical staged semantic envelope."""
        payload = _read_json_object(generation / "semantic_inputs.json")
        combined = _json_object(payload.get("combined"))
        responses = combined.get("responses")
        scores = _json_object(combined.get("scores"))
        answer_key = AnswerKeySnapshot.from_dict(_json_object(combined.get("answer_key")))
        if not isinstance(responses, list):
            raise ValueError("canonical responses are absent")
        response_ids = tuple(
            EffectiveResponse.from_dict(_json_object(value)).work_item_id for value in responses
        )
        if tuple(sorted(response_ids, key=str.encode)) != parent.base_response_ids:
            raise ValueError("canonical responses do not match committed base responses")
        rows = scores.get("rows")
        maximum = scores.get("maximum_score")
        if not isinstance(rows, list) or not isinstance(maximum, str):
            raise ValueError("canonical scores are absent")
        try:
            maximum_score = Decimal(maximum)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("canonical maximum score is invalid") from error
        scored = 0
        score_ids: list[str] = []
        for row in rows:
            value = _json_object(row)
            work_item_id = value.get("work_item_id")
            score = value.get("score")
            if not isinstance(work_item_id, str) or (
                score is not None and not isinstance(score, str)
            ):
                raise ValueError("canonical score row is invalid")
            if score is not None:
                try:
                    Decimal(score)
                except InvalidOperation as error:
                    raise ValueError("canonical score is invalid") from error
                scored += 1
            score_ids.append(work_item_id)
        if tuple(sorted(score_ids, key=str.encode)) != parent.base_response_ids:
            raise ValueError("canonical score rows do not match committed base responses")
        return (
            parent.base_response_ids,
            parent.profile_sha256,
            parent.roster_sha256,
            _semantic_sha(answer_key),
            parent.threshold_version,
            parent.threshold_sha256,
            ManifestSummary(
                len(response_ids),
                scored,
                len(response_ids) - scored,
                format(maximum_score, "f"),
            ),
        )

    def _identity_exists(self, session_id: str) -> bool:
        return (
            self._locate(session_id) is not None
            or self._reservation(session_id).exists()
            or any(
                path.is_dir()
                and (path / "DELETE.json").exists()
                and self._delete_id(path) == session_id
                for path in self._deleting().glob("*")
            )
        )

    def _delete_id(self, path: Path) -> str | None:
        try:
            return DeleteTombstone.from_dict(_read_json_object(path / "DELETE.json")).session_id
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def create_initial_generation(
        self,
        *,
        identity: IdentityRecord,
        manifest: SessionManifest,
        session: SessionRecord,
        display_name: str,
    ) -> Result[SessionCreateResult]:
        """Publish a fully prepared generation one; caller supplies validated immutable records."""
        return self._create_initial_generation(
            identity=identity,
            manifest=manifest,
            session=session,
            display_name=display_name,
            artifacts={},
        )

    def _create_initial_generation(
        self,
        *,
        identity: IdentityRecord,
        manifest: SessionManifest,
        session: SessionRecord,
        display_name: str,
        artifacts: Mapping[str, bytes],
    ) -> Result[SessionCreateResult]:
        """Store-owned creation transaction with a closed, manifest-validated artifact seam."""
        if any(
            not isinstance(path, str)
            or not path
            or _is_control_file(path)
            or type(payload) is not bytes
            or not payload
            for path, payload in artifacts.items()
        ):
            return _error("SESSION_CREATE_INVALID", "초기 generation artifact가 올바르지 않습니다.")
        try:
            self._display_path(display_name)
        except ValueError as exc:
            return _error("SESSION_DISPLAY_NAME_INVALID", str(exc))
        if (
            manifest.revision != 1
            or manifest.session_id != identity.session_id
            or session.session_id != identity.session_id
            or any(_is_control_file(entry.path) for entry in manifest.files)
        ):
            return _error(
                "SESSION_CREATE_INVALID",
                "초기 generation의 identity 또는 revision이 올바르지 않습니다.",
            )
        self._mkdirs()
        root_lock = self._lock(self._root_lock(), exclusive=True, busy="SESSION_WRITE_LOCKED")
        if isinstance(root_lock, Err):
            return root_lock
        reservation: Path | None = None
        gate_created: Path | None = None
        try:
            if self._identity_exists(identity.session_id):
                return _error("SESSION_ID_CONFLICT", "session_id가 이미 존재합니다.")
            reservation = self._reservation(identity.session_id)
            reservation.write_text(
                json.dumps(
                    SessionReservation(
                        1,
                        identity.session_id,
                        manifest.operation_id,
                        identity.creation_kind,
                        identity.created_at,
                        display_name,
                    ).to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            self._barrier("after_reservation")
            staging = self._root / f".{display_name}.{manifest.operation_id}.staging"
            staging.mkdir()
            (staging / "generations").mkdir()
            atomic_write_json(staging / "IDENTITY.json", identity.to_dict())
            atomic_write_json(
                staging / "LOCATION.json",
                self._location_metadata(
                    identity.session_id,
                    display_name,
                    manifest.operation_id,
                ),
            )
            generation_name = f"g{manifest.revision:08d}_{manifest.generation_id}"
            generation = staging / "generations" / generation_name
            generation.mkdir()
            atomic_write_json(generation / "session.json", session.to_dict())
            for relative, payload in artifacts.items():
                target = generation.joinpath(*relative.split("/"))
                if target.parent != generation and generation not in target.parents:
                    raise ValueError("artifact path escapes generation")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            atomic_write_json(generation / "manifest.json", manifest.to_dict())
            digest = _sha(generation / "manifest.json")
            pointer = CurrentPointer(
                1,
                identity.session_id,
                1,
                manifest.generation_id,
                f"generations/{generation_name}",
                digest,
                _utc(),
            )
            atomic_write_json(staging / "CURRENT.json", pointer.to_dict())
            _refresh_result_view(staging, generation)
            self._validate_current(staging)
            gate_created = self._gate_path(identity.session_id, manifest.generation_id)
            gate_created.parent.mkdir(parents=True, exist_ok=True)
            with gate_created.open("x", encoding="ascii"):
                pass
            self._barrier("after_gate_create")
            if self._identity_exists(identity.session_id) and reservation != self._reservation(
                identity.session_id
            ):
                return _error("SESSION_ID_CONFLICT", "session_id가 이미 존재합니다.")
            os.replace(staging, self._display_path(display_name))
            result = SessionCreateResult(
                True,
                identity.session_id,
                1,
                manifest.generation_id,
                IndexState.STALE,
                manifest.operation_id,
            )
            try:
                self._barrier("after_session_rename")
                reservation.unlink(missing_ok=True)
            except OSError as exc:
                return Ok(
                    result,
                    (
                        _warning(
                            "POSTCOMMIT_RECOVERY_REQUIRED",
                            f"create 이후 정리 작업을 완료하지 못했습니다: {exc}",
                        ),
                    ),
                )
            return Ok(result)
        except (OSError, ValueError, TypeError) as exc:
            return _error("SESSION_CREATE_FAILED", str(exc))
        finally:
            root_lock.value.close()

    def commit_generation(self, mutation: GenerationMutation) -> Result[CommitGenerationResult]:
        """Copy prior immutable truth into a staged next generation, then replace CURRENT."""
        self._mkdirs()
        try:
            validate_portable_component(mutation.operation_id)
        except ValueError as exc:
            return _error("SESSION_OPERATION_INVALID", str(exc))
        try:
            session = self._locate(mutation.session_id, trash=False)
            self._locate(mutation.session_id)
        except ValueError as exc:
            return _error("SESSION_LOCATION_AMBIGUOUS", str(exc))
        if session is None:
            return _error("SESSION_NOT_FOUND", "활성 세션을 찾을 수 없습니다.")
        writer = self._lock(
            self._writer_lock(mutation.session_id), exclusive=True, busy="SESSION_WRITE_LOCKED"
        )
        if isinstance(writer, Err):
            return writer
        staging: Path | None = None
        final: Path | None = None
        gate: Path | None = None
        published = False
        try:
            current, parent, parent_dir = self._validate_current(session)
            if current.revision != mutation.expected_revision:
                return _error(
                    "SESSION_REVISION_CONFLICT", "expected revision이 현재 revision과 다릅니다."
                )
            revision = current.revision + 1
            try:
                old = SessionRecord.from_dict(_read_json_object(parent_dir / "session.json"))
                (
                    record,
                    (
                        base_response_ids,
                        profile_sha256,
                        roster_sha256,
                        key_sha256,
                        threshold_version,
                        threshold_sha256,
                        summary,
                    ),
                ) = self._derive_generation(parent, old, mutation, revision)
            except (ValueError, TypeError, AttributeError):
                return _error(
                    "SESSION_SEMANTIC_MISMATCH",
                    "semantic input, session record, and manifest provenance must share "
                    "committed identity, revision, and state.",
                )
            generation_id = uuid.uuid4().hex
            name = f"g{revision:08d}_{generation_id}"
            staging = session / ".staging" / mutation.operation_id / name
            if staging.exists():
                return _error(
                    "SESSION_STAGING_CONFLICT", "동일 operation staging이 이미 존재합니다."
                )
            staging.mkdir(parents=True)
            for entry in parent.files:
                if not _preserved_artifact(entry.path, mutation.operation_kind):
                    continue
                source = parent_dir.joinpath(*entry.path.split("/"))
                destination = staging.joinpath(*entry.path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            written = atomic_write_json(staging / "session.json", record.to_dict())
            if isinstance(written, Err):
                return written
            self._materializer.materialize(
                GenerationMaterializationInput(
                    StagingToken(staging),
                    mutation,
                    record,
                    parent,
                    current.manifest_sha256,
                    parent_dir,
                    generation_id,
                )
            )
            if mutation.operation_kind in _LIFECYCLE_OPERATIONS:
                (
                    base_response_ids,
                    profile_sha256,
                    roster_sha256,
                    key_sha256,
                    threshold_version,
                    threshold_sha256,
                    summary,
                ) = self._canonical_manifest_fields(parent, staging)
            from omr_grader.domain.models import ManifestFile

            files = tuple(
                ManifestFile(
                    item.relative_to(staging).as_posix(),
                    item.stat().st_size,
                    _sha(item),
                    "application/json"
                    if item.suffix in {".json", ".jsonl"}
                    else "application/octet-stream",
                )
                for item in sorted(
                    (
                        path
                        for path in staging.rglob("*")
                        if path.is_file()
                        and not _is_control_file(path.relative_to(staging).as_posix())
                    ),
                    key=lambda path: path.relative_to(staging).as_posix().encode("utf-8"),
                )
            )
            manifest = SessionManifest(
                1,
                mutation.session_id,
                revision,
                generation_id,
                parent.revision,
                parent.generation_id,
                current.manifest_sha256,
                mutation.operation_id,
                mutation.operation_kind,
                parent.app_version,
                _utc(),
                mutation.target_state,
                base_response_ids,
                profile_sha256,
                roster_sha256,
                key_sha256,
                threshold_version,
                threshold_sha256,
                files,
                summary,
            )
            written = atomic_write_json(staging / "manifest.json", manifest.to_dict())
            if isinstance(written, Err):
                return written
            self._manifest(staging)
            gate = self._gate_path(mutation.session_id, generation_id)
            gate.parent.mkdir(parents=True, exist_ok=True)
            with gate.open("x", encoding="ascii"):
                pass
            self._barrier("after_gate_create")
            current_again, _, _ = self._validate_current(session)
            if (
                current_again.revision != mutation.expected_revision
                or current_again.manifest_sha256 != current.manifest_sha256
            ):
                return _error("SESSION_REVISION_CONFLICT", "pointer가 staging 중 변경되었습니다.")
            target = session / "generations" / name
            if target.exists():
                return _error("SESSION_GENERATION_CONFLICT", "generation ID가 이미 존재합니다.")
            os.replace(staging, target)
            final = target
            self._barrier("after_generation_rename")
            pointer = CurrentPointer(
                1,
                mutation.session_id,
                revision,
                generation_id,
                f"generations/{name}",
                _sha(final / "manifest.json"),
                _utc(),
            )
            written = atomic_write_json(session / "CURRENT.json", pointer.to_dict())
            if isinstance(written, Err):
                try:
                    if self._pointer(session) != pointer:
                        return written
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    return written
            _refresh_result_view(session, final)
            published = True
            result = CommitGenerationResult(
                True,
                mutation.session_id,
                revision,
                generation_id,
                IndexState.STALE,
                mutation.operation_id,
            )
            try:
                self._barrier("after_pointer_replace")
            except OSError as exc:
                return Ok(
                    result,
                    (
                        _warning(
                            "POSTCOMMIT_RECOVERY_REQUIRED",
                            f"commit 이후 정리 작업을 완료하지 못했습니다: {exc}",
                        ),
                    ),
                )
            try:
                retained = atomic_write_json(
                    session / "RETENTION.json",
                    {
                        "schema_version": 1,
                        "session_id": manifest.session_id,
                        "boundary_revision": manifest.revision,
                        "boundary_generation_id": manifest.generation_id,
                        "boundary_manifest_sha256": pointer.manifest_sha256,
                        "omitted_parent": {
                            "revision": manifest.parent_revision,
                            "generation_id": manifest.parent_generation_id,
                            "manifest_sha256": manifest.parent_manifest_sha256,
                        },
                        "retained_at": _utc(),
                    },
                )
                if isinstance(retained, Err):
                    raise OSError("retention boundary를 기록하지 못했습니다.")
                self._barrier("before_generation_prune")
                self._prune_superseded_generations(session, final)
            except OSError as exc:
                return Ok(
                    result,
                    (
                        _warning(
                            "GENERATION_PRUNE_RETRY_REQUIRED",
                            f"현재 generation 게시 후 이전 자료를 정리하지 못했습니다: {exc}",
                        ),
                    ),
                )
            return Ok(result)
        except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            return _error("SESSION_COMMIT_FAILED", str(exc))
        finally:
            if not published:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                    operation_staging = staging.parent
                    try:
                        operation_staging.rmdir()
                        operation_staging.parent.rmdir()
                    except OSError:
                        pass
                if final is not None:
                    shutil.rmtree(final, ignore_errors=True)
                if gate is not None:
                    gate.unlink(missing_ok=True)
            writer.value.close()

    def _prune_superseded_generations(self, session: Path, current: Path) -> None:
        generations = session / "generations"
        candidates = tuple(
            path
            for path in sorted(generations.iterdir(), key=lambda item: item.name.encode("utf-8"))
            if path.is_dir() and path != current
        )
        gates: list[tuple[Path, GateHandle]] = []
        try:
            for candidate in candidates:
                manifest, _ = self._manifest(candidate)
                gate_path = self._gate_path(manifest.session_id, manifest.generation_id)
                locked = self._existing_lock(
                    gate_path,
                    exclusive=True,
                    busy="GENERATION_PRUNE_RETRY_REQUIRED",
                )
                if isinstance(locked, Err):
                    raise OSError(f"{candidate.name} generation이 사용 중입니다.")
                gates.append((gate_path, locked.value))
            prune_root = session / ".staging" / "prune"
            prune_root.mkdir(parents=True, exist_ok=True)
            moved: list[Path] = []
            for candidate in candidates:
                target = prune_root / candidate.name
                if target.exists():
                    shutil.rmtree(target)
                os.replace(candidate, target)
                moved.append(target)
            for target in moved:
                shutil.rmtree(target)
            try:
                prune_root.rmdir()
                prune_root.parent.rmdir()
            except OSError:
                pass
        finally:
            for gate_path, gate in reversed(gates):
                gate.close()
                gate_path.unlink(missing_ok=True)

    def soft_delete(self, request: SessionMutationRequest) -> Result[SoftDeleteResult]:
        return self._move_session(request, to_trash=True)

    def restore_from_trash(self, request: SessionMutationRequest) -> Result[TrashRestoreResult]:
        result = self._move_session(request, to_trash=False)
        if isinstance(result, Err):
            return result
        return Ok(
            TrashRestoreResult(
                True, result.value.location, result.value.index_state, request.operation_id
            ),
            result.warnings,
        )

    def _move_session(
        self, request: SessionMutationRequest, *, to_trash: bool
    ) -> Result[SoftDeleteResult]:
        self._mkdirs()
        root = self._lock(self._root_lock(), exclusive=True, busy="SESSION_WRITE_LOCKED")
        if isinstance(root, Err):
            return root
        writer: GateHandle | None = None
        gates: list[GateHandle] = []
        try:
            try:
                source = self._locate(request.session_id, trash=not to_trash)
                self._locate(request.session_id)
            except ValueError as exc:
                return _error("SESSION_LOCATION_AMBIGUOUS", str(exc))
            if source is None:
                return _error("SESSION_NOT_FOUND", "세션을 찾을 수 없습니다.")
            pointer = self._pointer(source)
            if pointer.revision != request.expected_revision:
                return _error(
                    "SESSION_REVISION_CONFLICT", "expected revision이 현재 revision과 다릅니다."
                )
            acquired = self._lock(
                self._writer_lock(request.session_id), exclusive=True, busy="SESSION_WRITE_LOCKED"
            )
            if isinstance(acquired, Err):
                return acquired
            writer = acquired.value
            locked = self._all_gates_exclusive(source)
            if isinstance(locked, Err):
                return locked
            gates = locked.value
            if to_trash:
                destination = self._trash() / request.session_id
                written = atomic_write_json(
                    source / "LOCATION.json",
                    self._location_metadata(request.session_id, source.name, request.operation_id),
                )
                if isinstance(written, Err):
                    return written
            else:
                destination = self._display_path(self._display_name(source, request.session_id))
            if destination.exists():
                return _error("SESSION_ID_CONFLICT", "대상 세션 위치가 이미 존재합니다.")
            self._barrier("before_directory_rename")
            os.replace(source, destination)
            result = SoftDeleteResult(
                True,
                "trash" if to_trash else "active",
                IndexState.STALE,
                request.operation_id,
            )
            try:
                self._barrier("after_directory_rename")
            except OSError as exc:
                return Ok(
                    result,
                    (
                        _warning(
                            "POSTCOMMIT_RECOVERY_REQUIRED",
                            f"move 이후 정리 작업을 완료하지 못했습니다: {exc}",
                        ),
                    ),
                )
            return Ok(result)
        except OSError as exc:
            return _error("SESSION_MOVE_FAILED", str(exc))
        finally:
            for gate in reversed(gates):
                gate.close()
            if writer:
                writer.close()
            root.value.close()

    def permanently_delete(self, request: SessionMutationRequest) -> Result[PermanentDeleteResult]:
        self._mkdirs()
        root = self._lock(self._root_lock(), exclusive=True, busy="SESSION_WRITE_LOCKED")
        if isinstance(root, Err):
            return root
        writer: GateHandle | None = None
        gates: list[GateHandle] = []
        try:
            try:
                source = self._locate(request.session_id, trash=True)
                self._locate(request.session_id)
            except ValueError as exc:
                return _error("SESSION_LOCATION_AMBIGUOUS", str(exc))
            if source is None:
                return _error("SESSION_NOT_FOUND", "휴지통 세션을 찾을 수 없습니다.")
            if self._pointer(source).revision != request.expected_revision:
                return _error(
                    "SESSION_REVISION_CONFLICT", "expected revision이 현재 revision과 다릅니다."
                )
            acquired = self._lock(
                self._writer_lock(request.session_id), exclusive=True, busy="SESSION_WRITE_LOCKED"
            )
            if isinstance(acquired, Err):
                return acquired
            writer = acquired.value
            locked = self._all_gates_exclusive(source)
            if isinstance(locked, Err):
                return locked
            gates = locked.value
            tomb = self._deleting() / request.operation_id
            if tomb.exists():
                return _error("DELETE_OPERATION_CONFLICT", "삭제 operation_id가 이미 존재합니다.")
            generation_ids = tuple(
                manifest.generation_id
                for manifest, _ in sorted(
                    (
                        self._manifest(generation)
                        for generation in (source / "generations").iterdir()
                        if generation.is_dir() and not generation.is_symlink()
                    ),
                    key=lambda item: item[0].generation_id,
                )
            )
            written = atomic_write_json(
                source / "DELETE.json",
                DeleteTombstone(
                    1, request.session_id, request.operation_id, _utc(), generation_ids
                ).to_dict(),
            )
            if isinstance(written, Err):
                return written
            os.replace(source, tomb)
            warnings: tuple[ErrorInfo, ...] = ()
            cleanup = CleanupState.COMPLETE
            try:
                self._barrier("before_delete_cleanup")
                shutil.rmtree(tomb)
                for generation_id in generation_ids:
                    self._gate_path(request.session_id, generation_id).unlink(missing_ok=True)
            except OSError as exc:
                cleanup = CleanupState.PENDING
                warnings = (_warning("DELETE_CLEANUP_PENDING", str(exc)),)
            return Ok(
                PermanentDeleteResult(True, IndexState.STALE, cleanup, request.operation_id),
                warnings,
            )
        except OSError as exc:
            return _error("SESSION_DELETE_FAILED", str(exc))
        finally:
            for gate in reversed(gates):
                gate.close()
            if writer:
                writer.close()
            root.value.close()

    def _recover_session(
        self,
        path: Path,
        *,
        cleanup_orphans: bool,
        quarantined: list[RecoveryIssue],
        cleaned: list[RecoveryIssue],
    ) -> str | None:
        try:
            pointer, _, _ = self._validate_current(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            quarantined.append(
                RecoveryIssue(
                    None,
                    "SESSION_POINTER_INVALID",
                    f"{path.name}의 CURRENT.json을 신뢰할 수 없습니다.",
                )
            )
            return None
        writer = self._lock(
            self._writer_lock(pointer.session_id), exclusive=True, busy="SESSION_WRITE_LOCKED"
        )
        if isinstance(writer, Err):
            quarantined.append(
                RecoveryIssue(pointer.session_id, "SESSION_RECOVERY_BLOCKED", path.name)
            )
            return None
        gates: list[GateHandle] = []
        try:
            generation_ids: list[str] = []
            for candidate in (path / "generations").iterdir():
                try:
                    manifest, _ = self._manifest(candidate)
                    if (
                        not candidate.is_dir()
                        or _is_reparse_point(candidate)
                        or manifest.session_id != pointer.session_id
                    ):
                        raise ValueError("published generation identity invalid")
                    generation_ids.append(manifest.generation_id)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
            for generation_id in sorted(set(generation_ids)):
                gate_path = self._gate_path(pointer.session_id, generation_id)
                if not gate_path.exists():
                    continue
                locked = self._existing_lock(gate_path, exclusive=True, busy="SESSION_BUSY_READERS")
                if isinstance(locked, Err):
                    quarantined.append(
                        RecoveryIssue(pointer.session_id, "SESSION_RECOVERY_BLOCKED", path.name)
                    )
                    return None
                gates.append(locked.value)

            # Locks are now held in L0 -> L1 -> sorted L2 order; never classify stale bytes.
            pointer, _, _ = self._validate_current(path)
            for candidate in (path / "generations").iterdir():
                try:
                    manifest, _ = self._manifest(candidate)
                    record = SessionRecord.from_dict(_read_json_object(candidate / "session.json"))
                    if (
                        not candidate.is_dir()
                        or _is_reparse_point(candidate)
                        or manifest.session_id != pointer.session_id
                        or record.session_id != pointer.session_id
                        or record.revision != manifest.revision
                        or record.state is not manifest.state
                    ):
                        raise ValueError("published generation identity invalid")
                    if not self._gate_path(pointer.session_id, manifest.generation_id).is_file():
                        quarantined.append(
                            RecoveryIssue(
                                pointer.session_id,
                                "GENERATION_GATE_MISSING",
                                "generation gate가 없습니다.",
                            )
                        )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    quarantined.append(
                        RecoveryIssue(
                            pointer.session_id, "SESSION_GENERATION_ORPHAN", candidate.name
                        )
                    )
            staging_root = path / ".staging"
            if staging_root.exists():
                for staging in sorted(staging_root.glob("*/*"), key=lambda item: str(item)):
                    issue = RecoveryIssue(
                        pointer.session_id,
                        "SESSION_STAGING_ORPHAN",
                        str(staging.relative_to(path)),
                    )
                    if cleanup_orphans:
                        try:
                            shutil.rmtree(staging)
                            cleaned.append(issue)
                        except OSError:
                            quarantined.append(issue)
                    else:
                        quarantined.append(issue)
                if cleanup_orphans and staging_root.is_dir() and not staging_root.is_symlink():
                    for operation_root in sorted(
                        staging_root.iterdir(), key=lambda item: item.name
                    ):
                        if (
                            operation_root.is_dir()
                            and not operation_root.is_symlink()
                            and not any(operation_root.iterdir())
                        ):
                            operation_root.rmdir()
                    if not any(staging_root.iterdir()):
                        staging_root.rmdir()
            return pointer.session_id
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            quarantined.append(
                RecoveryIssue(pointer.session_id, "SESSION_POINTER_INVALID", path.name)
            )
            return None
        finally:
            for gate in reversed(gates):
                gate.close()
            writer.value.close()

    def recover_sessions(self, request: RecoveryRequest) -> Result[RecoveryReport]:
        """Conservatively classify residue; published generations are never auto-collected."""
        self._mkdirs()
        root = self._lock(self._root_lock(), exclusive=True, busy="SESSION_WRITE_LOCKED")
        if isinstance(root, Err):
            return root
        quarantined: list[RecoveryIssue] = []
        cleaned: list[RecoveryIssue] = []
        count = 0
        revalidated_locations: dict[str, list[Path]] = {}
        known_gate_sessions: set[str] = set()
        try:
            active = [
                path
                for path in self._root.iterdir()
                if (
                    path.is_dir()
                    and not _is_reparse_point(path)
                    and not path.name.startswith(".")
                    and path.name != "_휴지통"
                )
            ]
            trash = [
                path
                for path in self._trash().iterdir()
                if path.is_dir() and not _is_reparse_point(path)
            ]
            for session in sorted((*active, *trash), key=lambda item: str(item)):
                count += 1
                owner = self._recover_session(
                    session,
                    cleanup_orphans=request.cleanup_orphans,
                    quarantined=quarantined,
                    cleaned=cleaned,
                )
                if owner is not None:
                    revalidated_locations.setdefault(owner, []).append(session)
            for session_id, locations in revalidated_locations.items():
                if len(locations) == 1:
                    known_gate_sessions.add(session_id)
                else:
                    quarantined.append(
                        RecoveryIssue(
                            session_id,
                            "SESSION_LOCATION_AMBIGUOUS",
                            ", ".join(sorted(path.name for path in locations)),
                        )
                    )

            for staging in sorted(self._root.glob(".*.staging"), key=lambda item: item.name):
                issue = RecoveryIssue(None, "SESSION_STAGING_ORPHAN", staging.name)
                if request.cleanup_orphans:
                    try:
                        shutil.rmtree(staging)
                        cleaned.append(issue)
                    except OSError:
                        quarantined.append(issue)
                else:
                    quarantined.append(issue)

            for reservation in sorted((self._root / ".reservations").glob("*.json")):
                try:
                    value = SessionReservation.from_dict(_read_json_object(reservation))
                    quarantined.append(
                        RecoveryIssue(
                            value.session_id, "SESSION_RESERVATION_ORPHAN", reservation.name
                        )
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    quarantined.append(
                        RecoveryIssue(None, "SESSION_RESERVATION_INVALID", reservation.name)
                    )

            for deleting in sorted(self._deleting().glob("*"), key=lambda item: item.name):
                try:
                    if not deleting.is_dir() or _is_reparse_point(deleting):
                        raise ValueError("delete residue path invalid")
                    tombstone = DeleteTombstone.from_dict(
                        _read_json_object(deleting / "DELETE.json")
                    )
                    writer = self._lock(
                        self._writer_lock(tombstone.session_id),
                        exclusive=True,
                        busy="SESSION_WRITE_LOCKED",
                    )
                    if isinstance(writer, Err):
                        quarantined.append(
                            RecoveryIssue(
                                tombstone.session_id, "DELETE_CLEANUP_BLOCKED", deleting.name
                            )
                        )
                        continue
                    gates: list[GateHandle] = []
                    try:
                        for generation_id in sorted(tombstone.generation_ids):
                            locked = self._existing_lock(
                                self._gate_path(tombstone.session_id, generation_id),
                                exclusive=True,
                                busy="SESSION_BUSY_READERS",
                            )
                            if isinstance(locked, Err):
                                quarantined.append(
                                    RecoveryIssue(
                                        tombstone.session_id,
                                        "DELETE_CLEANUP_BLOCKED",
                                        deleting.name,
                                    )
                                )
                                break
                            gates.append(locked.value)
                        else:
                            known_gate_sessions.add(tombstone.session_id)
                            if request.cleanup_orphans:
                                shutil.rmtree(deleting)
                                for generation_id in tombstone.generation_ids:
                                    self._gate_path(tombstone.session_id, generation_id).unlink(
                                        missing_ok=True
                                    )
                                cleaned.append(
                                    RecoveryIssue(
                                        tombstone.session_id,
                                        "DELETE_CLEANUP_COMPLETED",
                                        deleting.name,
                                    )
                                )
                            else:
                                quarantined.append(
                                    RecoveryIssue(
                                        tombstone.session_id,
                                        "DELETE_CLEANUP_PENDING",
                                        deleting.name,
                                    )
                                )
                    finally:
                        for gate in reversed(gates):
                            gate.close()
                        writer.value.close()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    quarantined.append(RecoveryIssue(None, "DELETE_RESIDUE_INVALID", deleting.name))

            for namespace in sorted(
                (self._locks() / "lifetime").glob("*"), key=lambda item: item.name
            ):
                if namespace.is_dir() and namespace.name not in known_gate_sessions:
                    quarantined.append(
                        RecoveryIssue(namespace.name, "GENERATION_GATE_ORPHAN", namespace.name)
                    )
            return Ok(RecoveryReport(count, IndexState.STALE, tuple(quarantined), tuple(cleaned)))
        finally:
            root.value.close()


class _SessionStoreRestorePublisher:
    """Private restore transaction owned by :class:`SessionStore`."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        self._cleanup_errors: tuple[ErrorInfo, ...] = ()

    def publish_restored(
        self, extracted: ExtractedBackup, target_root: str, operation_id: str
    ) -> Result[BackupRestoreResult]:
        self._cleanup_errors = ()
        outcome = self._publish_restored(extracted, target_root, operation_id)
        if not self._cleanup_errors:
            return outcome
        if isinstance(outcome, Err):
            return Err((*outcome.errors, *self._cleanup_errors))
        return Ok(
            outcome.value,
            (
                *outcome.warnings,
                *(
                    _warning(error.code, f"restore cleanup requires recovery: {error.field_path}")
                    for error in self._cleanup_errors
                ),
            ),
        )

    def _publish_restored(
        self, extracted: ExtractedBackup, target_root: str, operation_id: str
    ) -> Result[BackupRestoreResult]:
        store = self._store
        staging = Path(extracted.staging_root)
        gate_path: Path | None = None
        prepared: Path | None = None
        prepared_owned = False
        prepared_identity: tuple[int, int] | None = None
        prepared_parent_identity: tuple[int, int] | None = None
        gate_created = False
        gate_identity: tuple[int, int] | None = None
        published = False
        try:
            validate_portable_component(operation_id)
            if Path(target_root).resolve(strict=True) != store.root.resolve(strict=True):
                return _error(
                    "BACKUP_RESTORE_TARGET_INVALID", "restore target is not the session store"
                )
            if staging.parent.resolve(strict=True) != store.root.resolve(strict=True):
                return _error(
                    "BACKUP_RESTORE_STAGING_INVALID", "restore staging is not store-owned"
                )
            if (
                _is_reparse_point(staging)
                or not extracted.archive_sha256
                or extracted.source_identity is None
                or extracted.staging_ownership is None
            ):
                return _error("BACKUP_RESTORE_INVALID", "restore proof is incomplete")
            if _path_identity(staging) != (
                extracted.staging_ownership.tree_device,
                extracted.staging_ownership.tree_inode,
            ) or _path_identity(staging.parent) != (
                extracted.staging_ownership.parent_device,
                extracted.staging_ownership.parent_inode,
            ):
                return _error("BACKUP_RESTORE_OWNERSHIP_LOST", "restore staging changed")
            store._mkdirs()
            root = store._lock(store._root_lock(), exclusive=True, busy="SESSION_WRITE_LOCKED")
            if isinstance(root, Err):
                return root
            writer: GateHandle | None = None
            try:
                if store._identity_exists(extracted.identity.session_id):
                    return _error("SESSION_ID_CONFLICT", "session_id가 이미 존재합니다.")
                writer_result = store._lock(
                    store._writer_lock(extracted.identity.session_id),
                    exclusive=True,
                    busy="SESSION_WRITE_LOCKED",
                )
                if isinstance(writer_result, Err):
                    return writer_result
                writer = writer_result.value
                name = f"g{extracted.manifest.revision:08d}_{extracted.manifest.generation_id}"
                if extracted.current.generation_relpath != f"generations/{name}":
                    return _error("BACKUP_RESTORE_INVALID", "current generation path mismatch")
                prepared = (
                    store.root / f".restore-{extracted.identity.session_id}-{operation_id}.staging"
                )
                if prepared.exists():
                    return _error("SESSION_STAGING_CONFLICT", "restore staging already exists")
                _move_directory_no_replace(staging, prepared)
                prepared_owned = True
                prepared_identity = _path_identity(prepared)
                prepared_parent_identity = _path_identity(prepared.parent)
                location_metadata = store._location_metadata(
                    extracted.identity.session_id, extracted.identity.session_id, operation_id
                )
                written = atomic_write_json(
                    prepared / "LOCATION.json", location_metadata
                )
                if isinstance(written, Err):
                    return written
                provenance = RestoreProvenance(
                    1,
                    extracted.identity.session_id,
                    extracted.archive_sha256,
                    extracted.manifest.revision,
                    extracted.manifest.generation_id,
                    extracted.current.manifest_sha256,
                    extracted.archive_manifest.omitted_parent,
                    _utc(),
                    LineageState.VALID_TRUNCATED_ANCESTOR,
                )
                written = atomic_write_json(
                    prepared / "RESTORE_PROVENANCE.json", provenance.to_dict()
                )
                if isinstance(written, Err):
                    return written
                store._validate_current(prepared)
                gate_path = store._gate_path(
                    extracted.identity.session_id, extracted.manifest.generation_id
                )
                gate_path.parent.mkdir(parents=True, exist_ok=True)
                with gate_path.open("x", encoding="ascii"):
                    pass
                gate_created = True
                gate_identity = _file_identity(gate_path)
                store._barrier("after_restore_gate_create")
                if store._identity_exists(extracted.identity.session_id):
                    return _error("SESSION_ID_CONFLICT", "session_id가 이미 존재합니다.")
                target = store._display_path(extracted.identity.session_id)
                target_parent_identity = _path_identity(target.parent)
                if prepared_identity is None or prepared_parent_identity is None:
                    return _error("BACKUP_RESTORE_OWNERSHIP_LOST", "restore staging changed")
                _verify_restored_staging(
                    extracted,
                    prepared,
                    provenance,
                    location_metadata,
                    prepared_identity,
                    prepared_parent_identity,
                )
                store._barrier("after_restore_verification")
                verified_tree = _verify_restored_staging(
                    extracted,
                    prepared,
                    provenance,
                    location_metadata,
                    prepared_identity,
                    prepared_parent_identity,
                    pin_for_publication=True,
                )
                if os.name != "nt" and _path_identity(target.parent) != target_parent_identity:
                    if verified_tree is not None:
                        verified_tree.close()
                    return _error("BACKUP_RESTORE_OWNERSHIP_LOST", "restore target parent changed")
                result = BackupRestoreResult(
                    True,
                    extracted.identity.session_id,
                    extracted.manifest.revision,
                    extracted.manifest.generation_id,
                    IndexState.STALE,
                    operation_id,
                )
                publication_warnings: tuple[ErrorInfo, ...] = ()
                try:
                    publication_warnings = _publish_directory_no_replace(
                        prepared, target, verified_tree
                    )
                    published = True
                finally:
                    if verified_tree is not None:
                        verified_tree.close()
                postcommit_warnings: list[ErrorInfo] = list(publication_warnings)
                try:
                    if _path_identity(target) != prepared_identity:
                        raise ValueError("published restore changed")
                    store._barrier("after_restore_session_rename")
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    postcommit_warnings.append(
                        _warning(
                            "POSTCOMMIT_RECOVERY_REQUIRED",
                            f"restore 이후 확인 작업을 완료하지 못했습니다: {exc}",
                        )
                    )
                return Ok(result, tuple(postcommit_warnings))
            finally:
                if writer is not None:
                    writer.close()
                root.value.close()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _error("BACKUP_RESTORE_FAILED", str(exc))
        finally:
            cleanup_errors: list[ErrorInfo] = []
            if not published:
                if prepared is not None and prepared_owned:
                    cleanup_path = prepared
                    try:
                        if (
                            prepared_identity is None
                            or _path_identity(prepared) != prepared_identity
                        ):
                            raise OSError("restore staging path ownership changed")
                        quarantine = prepared.with_name(f".restore-quarantine-{uuid.uuid4().hex}")
                        _move_directory_no_replace(prepared, quarantine)
                        cleanup_path = quarantine
                        if _path_identity(quarantine) != prepared_identity:
                            raise OSError("restore quarantine ownership changed")
                        shutil.rmtree(quarantine)
                    except FileNotFoundError:
                        cleanup_errors.append(
                            ErrorInfo(
                                "BACKUP_RESTORE_OWNERSHIP_LOST",
                                "error.backup_restore_ownership_lost",
                                field_path=str(cleanup_path),
                            )
                        )
                    except OSError:
                        cleanup_errors.append(
                            ErrorInfo(
                                "BACKUP_RESTORE_CLEANUP_REQUIRED",
                                "error.backup_restore_cleanup_required",
                                field_path=str(cleanup_path),
                            )
                        )
                if gate_created and gate_path is not None:
                    try:
                        if gate_identity is None or _file_identity(gate_path) != gate_identity:
                            raise OSError("restore gate ownership changed")
                        gate_path.unlink()
                    except FileNotFoundError:
                        cleanup_errors.append(
                            ErrorInfo(
                                "BACKUP_RESTORE_OWNERSHIP_LOST",
                                "error.backup_restore_ownership_lost",
                                field_path=str(gate_path),
                            )
                        )
                    except OSError:
                        cleanup_errors.append(
                            ErrorInfo(
                                "BACKUP_RESTORE_CLEANUP_REQUIRED",
                                "error.backup_restore_cleanup_required",
                                field_path=str(gate_path),
                            )
                        )
            self._cleanup_errors = tuple(cleanup_errors)


class SessionCommitCoordinator:
    """Internal single-writer façade; UI and workers must not receive SessionStore directly."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def open_committed_snapshot(
        self, request: SnapshotRequest
    ) -> Result[CommittedSnapshotLeasePort]:
        opened = self._store.open_committed_snapshot(request)
        if isinstance(opened, Err):
            return opened
        return Ok(opened.value, opened.warnings)

    def commit_generation(self, mutation: GenerationMutation) -> Result[CommitGenerationResult]:
        return self._store.commit_generation(mutation)

    def recover_sessions(self, request: RecoveryRequest) -> Result[RecoveryReport]:
        return self._store.recover_sessions(request)

    def soft_delete(self, request: SessionMutationRequest) -> Result[SoftDeleteResult]:
        return self._store.soft_delete(request)

    def restore_from_trash(self, request: SessionMutationRequest) -> Result[TrashRestoreResult]:
        return self._store.restore_from_trash(request)

    def permanently_delete(self, request: SessionMutationRequest) -> Result[PermanentDeleteResult]:
        return self._store.permanently_delete(request)
