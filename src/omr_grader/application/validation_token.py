"""Live-file validation tokens that close validate-to-use races."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import BinaryIO, NoReturn, TypeVar

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

_InspectionResult = TypeVar("_InspectionResult")


@dataclass(frozen=True, slots=True)
class SourceFileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


def _error(code: str, path: str) -> ErrorInfo:
    return ErrorInfo(
        code=code,
        message_key=f"error.{code.lower()}",
        field_path=path,
        context={},
        retryable=False,
        cause_type=None,
    )


def _identity(file_stat: os.stat_result) -> SourceFileIdentity:
    return SourceFileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        modified_ns=file_stat.st_mtime_ns,
    )


def _digest(handle: BinaryIO) -> str:
    handle.seek(0)
    digest = sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _open_read_only(path: Path) -> BinaryIO:
    if sys.platform != "win32":
        return path.open("rb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x80000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        file_descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close_handle(handle)
        raise
    try:
        return open(file_descriptor, "rb", closefd=True)
    except OSError:
        os.close(file_descriptor)
        raise


class _LiveFileToken:
    """Base class for a read-only validated file held open until consumption."""

    __slots__ = (
        "_canonical_path",
        "_handle",
        "_identity",
        "_sha256",
        "_closed",
        "_consumed",
        "_lock",
    )

    def __init__(self, canonical_path: Path, handle: BinaryIO) -> None:
        self._canonical_path = canonical_path
        self._handle = handle
        self._identity = _identity(os.fstat(handle.fileno()))
        self._sha256 = _digest(handle)
        self._closed = False
        self._consumed = False
        self._lock = RLock()

    @property
    def canonical_path(self) -> str:
        return str(self._canonical_path)

    @property
    def source_identity(self) -> SourceFileIdentity:
        return self._identity

    @property
    def source_sha256(self) -> str:
        return self._sha256

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> Result[None]:
        with self._lock:
            if self._closed:
                return Ok(None)
            try:
                self._handle.close()
            except OSError:
                return Err((_error("VALIDATION_TOKEN_CLOSE_FAILED", self.canonical_path),))
            self._closed = True
            return Ok(None)

    def __enter__(self) -> _LiveFileToken:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __reduce__(self) -> NoReturn:
        raise TypeError("live validation tokens cannot be serialized")

    def _verify_open_source(self, closed_code: str, source_changed_code: str) -> Result[None]:
        if self._closed:
            return Err((_error(closed_code, self.canonical_path),))
        try:
            handle_identity = _identity(os.fstat(self._handle.fileno()))
            path_identity = _identity(os.stat(self._canonical_path))
            if handle_identity != self._identity or path_identity != self._identity:
                return Err((_error(source_changed_code, self.canonical_path),))
            if _digest(self._handle) != self._sha256:
                return Err((_error(source_changed_code, self.canonical_path),))
            if (
                _identity(os.fstat(self._handle.fileno())) != handle_identity
                or _identity(os.stat(self._canonical_path)) != path_identity
            ):
                return Err((_error(source_changed_code, self.canonical_path),))
        except OSError:
            return Err((_error(source_changed_code, self.canonical_path),))
        return Ok(None)


class ResponseValidationToken(_LiveFileToken):
    """A response workbook validation proof consumed once by import."""

    @classmethod
    def open(cls, path: str) -> Result[ResponseValidationToken]:
        try:
            canonical_path = Path(path).resolve(strict=True)
            handle = _open_read_only(canonical_path)
            try:
                return Ok(cls(canonical_path, handle))
            except BaseException:
                handle.close()
                raise
        except OSError:
            return Err((_error("XLSX_SOURCE_CHANGED", path),))

    def consume_for_import(self) -> Result[BinaryIO]:
        """Atomically verify the held source and reserve it for one import."""
        with self._lock:
            if self._consumed:
                return Err((_error("XLSX_SOURCE_CHANGED", self.canonical_path),))
            checked = self._verify_open_source(
                "XLSX_VALIDATION_TOKEN_CLOSED", "XLSX_SOURCE_CHANGED"
            )
            if isinstance(checked, Err):
                return checked
            self._consumed = True
            self._handle.seek(0)
            return Ok(self._handle)

    def revalidate(self) -> Result[None]:
        with self._lock:
            return self._verify_open_source("XLSX_VALIDATION_TOKEN_CLOSED", "XLSX_SOURCE_CHANGED")


class BorrowedInspectionHandle:
    """A callback-scoped, read-only view that cannot close its token's source."""

    __slots__ = ("_handle", "_active", "_closed")

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._active = True
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self, size: int = -1) -> bytes:
        self._require_open()
        return self._handle.read(size)

    def readline(self, size: int = -1) -> bytes:
        self._require_open()
        return self._handle.readline(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._require_open()
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        self._require_open()
        return self._handle.tell()

    def readable(self) -> bool:
        self._require_open()
        return True

    def seekable(self) -> bool:
        self._require_open()
        return True

    def writable(self) -> bool:
        self._require_open()
        return False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> BorrowedInspectionHandle:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _invalidate(self) -> None:
        self._active = False
        self._closed = True

    def _require_open(self) -> None:
        if not self._active or self._closed:
            raise ValueError("borrowed inspection handle is closed")


class ValidatedBackup(_LiveFileToken):
    """A validated backup archive handle consumed by restore without reopening."""

    @classmethod
    def open(cls, path: str) -> Result[ValidatedBackup]:
        try:
            canonical_path = Path(path).resolve(strict=True)
            handle = _open_read_only(canonical_path)
            try:
                return Ok(cls(canonical_path, handle))
            except BaseException:
                handle.close()
                raise
        except OSError:
            return Err((_error("BACKUP_SOURCE_CHANGED", path),))

    def consume_for_restore(self) -> Result[BinaryIO]:
        with self._lock:
            if self._consumed:
                return Err((_error("BACKUP_SOURCE_CHANGED", self.canonical_path),))
            checked = self._verify_open_source("BACKUP_HANDLE_CLOSED", "BACKUP_SOURCE_CHANGED")
            if isinstance(checked, Err):
                return checked
            self._consumed = True
            self._handle.seek(0)
            return Ok(self._handle)

    def revalidate(self) -> Result[None]:
        with self._lock:
            return self._verify_open_source("BACKUP_HANDLE_CLOSED", "BACKUP_SOURCE_CHANGED")

    def inspect_revalidated(
        self, inspection: Callable[[BorrowedInspectionHandle], Result[_InspectionResult]]
    ) -> Result[_InspectionResult]:
        """Run an inspection against this token's live handle without lending it out."""
        with self._lock:
            if self._consumed:
                return Err((_error("BACKUP_SOURCE_CHANGED", self.canonical_path),))
            checked = self._verify_open_source("BACKUP_HANDLE_CLOSED", "BACKUP_SOURCE_CHANGED")
            if isinstance(checked, Err):
                return checked
            borrowed = BorrowedInspectionHandle(self._handle)
            try:
                return inspection(borrowed)
            finally:
                borrowed._invalidate()
                self._handle.seek(0)


__all__ = [
    "BorrowedInspectionHandle",
    "ResponseValidationToken",
    "SourceFileIdentity",
    "ValidatedBackup",
]
