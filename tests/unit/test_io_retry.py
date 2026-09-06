from __future__ import annotations

import errno

import pytest

from omr_grader.infrastructure.io_retry import retry_io


def test_retry_io_retries_access_denied_with_bounded_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise PermissionError(errno.EACCES, "locked by synchronizer")
        return "committed"

    result = retry_io(operation, attempts=4, initial_delay=0.01, sleeper=delays.append)

    assert result == "committed"
    assert attempts == 4
    assert delays == [0.01, 0.02, 0.04]


def test_retry_io_does_not_retry_nontransient_failure() -> None:
    attempts = 0

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise FileNotFoundError(errno.ENOENT, "missing source")

    with pytest.raises(FileNotFoundError):
        retry_io(operation, attempts=4, sleeper=lambda _delay: None)

    assert attempts == 1
