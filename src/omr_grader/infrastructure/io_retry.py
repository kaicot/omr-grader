"""Bounded retries for transient Windows file-lock failures."""

from __future__ import annotations

import errno
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

_TRANSIENT_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.EPERM})
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})


def _transient(error: OSError) -> bool:
    return error.errno in _TRANSIENT_ERRNOS or getattr(error, "winerror", None) in (
        _TRANSIENT_WINERRORS
    )


def retry_io(
    operation: Callable[[], T],
    *,
    attempts: int = 4,
    initial_delay: float = 0.05,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    """Retry transient access-denied and sharing violations with bounded backoff."""
    if attempts < 3:
        raise ValueError("attempts must be at least 3")
    if initial_delay < 0:
        raise ValueError("initial_delay must be nonnegative")
    for attempt in range(attempts):
        try:
            return operation()
        except OSError as error:
            if not _transient(error) or attempt + 1 >= attempts:
                raise
            sleeper(initial_delay * (2**attempt))
    raise AssertionError("retry loop must return or raise")


def retry_mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
    retry_io(lambda: path.mkdir(parents=parents, exist_ok=exist_ok))


def retry_copy2(source: Path, destination: Path) -> None:
    retry_io(lambda: shutil.copy2(source, destination))


def retry_replace(source: Path, destination: Path) -> None:
    retry_io(lambda: os.replace(source, destination))


def retry_unlink(path: Path, *, missing_ok: bool = False) -> None:
    retry_io(lambda: path.unlink(missing_ok=missing_ok))


def retry_touch(path: Path, *, exist_ok: bool = True) -> None:
    retry_io(lambda: path.touch(exist_ok=exist_ok))


__all__ = [
    "retry_copy2",
    "retry_io",
    "retry_mkdir",
    "retry_replace",
    "retry_touch",
    "retry_unlink",
]
