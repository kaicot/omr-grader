"""Durable same-directory atomic replacement for managed files."""

from __future__ import annotations

import ctypes
import json
import math
import os
import secrets
import stat
import sys
import unicodedata
from pathlib import Path
from typing import cast

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.io_retry import retry_io


def _error(code: str, message: str, exc: BaseException | None = None) -> Err:
    return Err(
        (
            ErrorInfo(
                code,
                f"error.{code.lower()}",
                context={"reason": message},
                cause_type=type(exc).__name__ if exc else None,
            ),
        )
    )


def _temp_path(destination: Path) -> Path:
    return destination.parent / f".{destination.name}.{secrets.token_hex(16)}.tmp"


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x0400)


def _replace_durably(temporary: Path, target: Path) -> None:
    if sys.platform == "win32":
        try:
            move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        except (AttributeError, OSError) as exc:
            raise OSError("Windows write-through replacement is unavailable") from exc
        move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move_file_ex.restype = ctypes.c_int
        flags = 0x00000001 | 0x00000008  # REPLACE_EXISTING | WRITE_THROUGH
        if not move_file_ex(str(temporary), str(target), flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return

    os.replace(temporary, target)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(target.parent, directory_flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(destination: Path, payload: bytes) -> Result[None]:
    """Write bytes through a sibling temporary file and durably replace target."""
    if not isinstance(payload, bytes):
        return _error("INVALID_WRITE_PAYLOAD", "저장할 데이터는 바이트여야 합니다.")
    try:
        parent = destination.parent.resolve(strict=True)
        if _is_reparse_point(destination):
            return _error("ATOMIC_WRITE_FAILED", "링크 또는 재분석 지점에는 저장할 수 없습니다.")
    except OSError as exc:
        return _error("ATOMIC_WRITE_FAILED", "저장 폴더를 확인할 수 없습니다.", exc)
    if not parent.is_dir():
        return _error("ATOMIC_WRITE_FAILED", "저장 경로의 상위 경로가 폴더가 아닙니다.")

    target = parent / destination.name
    temporary = _temp_path(target)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _is_reparse_point(target):
            raise OSError("refusing to replace a link or reparse point")
        retry_io(lambda: _replace_durably(temporary, target))
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return _error("ATOMIC_WRITE_FAILED", "설정 파일을 원자적으로 저장할 수 없습니다.", exc)
    return Ok(None)


def _normalize_json(value: object) -> object:
    """Return a canonical JSON value with NFC-normalized strings and keys."""
    value_type = type(value)
    if value is None or value_type in (bool, int):
        return value
    if value_type is float:
        if not math.isfinite(cast(float, value)):
            raise ValueError("JSON numbers must be finite")
        return value
    if value_type is str:
        return unicodedata.normalize("NFC", cast(str, value))
    if value_type is list:
        return [_normalize_json(item) for item in cast(list[object], value)]
    if value_type is dict:
        normalized: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("JSON object contains duplicate normalized keys")
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    raise TypeError("value is not JSON-safe")


def atomic_write_json(destination: Path, value: object) -> Result[None]:
    """Persist canonical UTF-8 JSON using the byte-level atomic writer."""
    try:
        payload = (
            json.dumps(
                _normalize_json(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (RecursionError, TypeError, ValueError) as exc:
        return _error("INVALID_JSON_VALUE", "저장할 JSON 값이 올바르지 않습니다.", exc)
    return atomic_write_bytes(destination, payload)
