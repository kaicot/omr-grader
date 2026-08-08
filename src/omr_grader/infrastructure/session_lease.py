"""Generation gates and pinned, committed snapshot leases."""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import msvcrt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from omr_grader.application.dto import SnapshotRef
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import ManifestFile, SessionManifest
from omr_grader.infrastructure.result_layout import external_artifact_relpath


def _error(code: str, reason: str, *, retryable: bool = False) -> Err:
    return Err(
        (ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason}, retryable=retryable),)
    )


class GateHandle(Protocol):
    def close(self) -> None: ...


class GateBackend(Protocol):
    """Injectable OS-lock backend used by race/fault tests."""

    def acquire(self, path: Path, *, exclusive: bool, blocking: bool) -> GateHandle | None: ...


@runtime_checkable
class _Flock(Protocol):
    def __call__(self, descriptor: int, operation: int) -> None: ...


def _posix_lock(descriptor: int, *, exclusive: bool, blocking: bool) -> None:
    module = importlib.import_module("fcntl")
    flock: object = getattr(module, "flock", None)
    lock_ex: object = getattr(module, "LOCK_EX", None)
    lock_sh: object = getattr(module, "LOCK_SH", None)
    lock_nb: object = getattr(module, "LOCK_NB", None)
    if (
        not isinstance(flock, _Flock)
        or not isinstance(lock_ex, int)
        or not isinstance(lock_sh, int)
        or not isinstance(lock_nb, int)
    ):
        raise OSError("POSIX file locking is unavailable")
    flags = lock_ex if exclusive else lock_sh
    if not blocking:
        flags |= lock_nb
    flock(descriptor, flags)


def _posix_unlock(descriptor: int) -> None:
    module = importlib.import_module("fcntl")
    flock: object = getattr(module, "flock", None)
    lock_un: object = getattr(module, "LOCK_UN", None)
    if not isinstance(flock, _Flock) or not isinstance(lock_un, int):
        raise OSError("POSIX file unlocking is unavailable")
    flock(descriptor, lock_un)


class _FileGate:
    def __init__(self, descriptor: int, *, exclusive: bool) -> None:
        self._descriptor = descriptor
        self._exclusive = exclusive

    def close(self) -> None:
        if self._descriptor < 0:
            return
        try:
            if os.name == "nt":
                overlapped = _OVERLAPPED()
                handle = msvcrt.get_osfhandle(self._descriptor)
                if not _kernel32().UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                _posix_unlock(self._descriptor)
        finally:
            os.close(self._descriptor)
            self._descriptor = -1


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


def _kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    dword = ctypes.c_uint32
    overlapped = ctypes.POINTER(_OVERLAPPED)
    kernel32.LockFileEx.argtypes = (handle, dword, dword, dword, dword, overlapped)
    kernel32.LockFileEx.restype = ctypes.c_int
    kernel32.UnlockFileEx.argtypes = (handle, dword, dword, dword, overlapped)
    kernel32.UnlockFileEx.restype = ctypes.c_int
    return kernel32


class FileGateBackend:
    """Shared reader/exclusive writer byte-range lock backend."""

    def acquire(self, path: Path, *, exclusive: bool, blocking: bool) -> GateHandle | None:
        try:
            descriptor = os.open(
                path, os.O_RDWR | os.O_BINARY if hasattr(os, "O_BINARY") else os.O_RDWR
            )
        except OSError:
            return None
        try:
            if os.name == "nt":
                flags = (0x00000002 if exclusive else 0) | (0 if blocking else 0x00000001)
                overlapped = _OVERLAPPED()
                handle = msvcrt.get_osfhandle(descriptor)
                if not _kernel32().LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    if not blocking and error in {32, 33}:
                        os.close(descriptor)
                        return None
                    raise ctypes.WinError(error)
            else:
                try:
                    _posix_lock(descriptor, exclusive=exclusive, blocking=blocking)
                except BlockingIOError:
                    os.close(descriptor)
                    return None
        except OSError:
            os.close(descriptor)
            return None
        return _FileGate(descriptor, exclusive=exclusive)


@dataclass(slots=True)
class CommittedSnapshotLease:
    """A non-picklable shared generation gate plus an immutable manifest allowlist."""

    _snapshot_ref: SnapshotRef
    _manifest: SessionManifest
    _root: Path
    _gate: GateHandle
    _closed: bool = False

    @property
    def snapshot_ref(self) -> SnapshotRef:
        return self._snapshot_ref

    @property
    def manifest(self) -> SessionManifest:
        return self._manifest

    @property
    def root_path(self) -> str:
        return str(self._root)

    def __getstate__(self) -> object:
        raise TypeError("CommittedSnapshotLease cannot be pickled")

    def open_allowlisted(self, relpath: str) -> Result[BinaryIO]:
        if self._closed:
            return _error("SESSION_LEASE_CLOSED", "닫힌 스냅샷 lease는 사용할 수 없습니다.")
        entry = next((item for item in self._manifest.files if item.path == relpath), None)
        if entry is None:
            return _error("SNAPSHOT_PATH_FORBIDDEN", "manifest allowlist 밖의 파일입니다.")
        candidate = self._root.joinpath(*relpath.split("/"))
        containment_root = self._root
        if not candidate.is_file():
            external = external_artifact_relpath(relpath)
            if external is not None:
                containment_root = self._root.parent.parent
                candidate = containment_root.joinpath(*external.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(containment_root.resolve(strict=True))
            if not resolved.is_file() or resolved.is_symlink():
                return _error("SNAPSHOT_FILE_INVALID", "스냅샷 파일이 안전하지 않습니다.")
            stream = resolved.open("rb")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry.sha256 or resolved.stat().st_size != entry.size:
                stream.close()
                return _error("SNAPSHOT_FILE_INVALID", "스냅샷 파일 무결성이 일치하지 않습니다.")
            stream.seek(0)
            return Ok(stream)
        except (OSError, ValueError) as exc:
            return _error("SNAPSHOT_FILE_INVALID", str(exc))

    def close(self) -> Result[None]:
        if self._closed:
            return Ok(None)
        try:
            self._gate.close()
            self._closed = True
        except OSError as exc:
            return _error("SESSION_LEASE_CLOSE_FAILED", str(exc))
        return Ok(None)

    def __enter__(self) -> CommittedSnapshotLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def manifest_file_map(manifest: SessionManifest) -> dict[str, ManifestFile]:
    return {item.path: item for item in manifest.files}
