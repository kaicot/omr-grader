from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from omr_grader.domain.errors import Ok
from omr_grader.infrastructure.logging_setup import (
    configure_logging,
    daily_log_path,
    install_global_exception_hooks,
    redact_log_text,
)
from omr_grader.infrastructure.paths import ManagedPaths


def _close_application_handlers(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_redact_log_text_removes_every_eight_digit_identifier() -> None:
    identifiers = ("20240101", "12345678", "87654321", "00000000")
    text = "학생 " + ", ".join(identifiers) + "을 처리했습니다."

    redacted = redact_log_text(text)

    for identifier in identifiers:
        assert identifier not in redacted
    assert redacted.count("[비공개]") == len(identifiers)


def test_configure_logging_writes_only_under_resolved_portable_data_root_and_redacts_ids(
    tmp_path: Path,
) -> None:
    first = configure_logging()
    assert isinstance(first, Ok)
    logger = first.value
    root = tmp_path / "portable"
    root.mkdir()
    paths = ManagedPaths.from_root(root)
    paths.data_dir.mkdir()
    log_target = paths.data_path("omr-grader.log")
    assert isinstance(log_target, Ok)
    try:
        assert not any(
            isinstance(handler._delegate, RotatingFileHandler)  # type: ignore[attr-defined]
            for handler in logger.handlers
        )

        configured = configure_logging(log_target.value)

        assert isinstance(configured, Ok)
        file_handlers = [
            handler
            for handler in configured.value.handlers
            if isinstance(handler._delegate, RotatingFileHandler)  # type: ignore[attr-defined]
        ]
        assert len(file_handlers) == 1
        delegate = file_handlers[0]._delegate  # type: ignore[attr-defined]
        assert Path(delegate.baseFilename).resolve().is_relative_to(paths.data_dir.resolve())
        configured.value.info("학번=20240101 로그 파일을 준비했습니다.")
        delegate.flush()
        contents = log_target.value.read_text(encoding="utf-8")
        assert "20240101" not in contents
        assert "[비공개]" in contents
    finally:
        _close_application_handlers(logger)


def test_daily_log_path_and_global_exception_hook_record_traceback(tmp_path: Path) -> None:
    log_path = daily_log_path(tmp_path, datetime(2026, 7, 30, 10, 54))
    assert log_path == tmp_path / "logs" / "app_20260730.log"
    configured = configure_logging(log_path)
    assert isinstance(configured, Ok)
    previous = sys.excepthook
    try:
        install_global_exception_hooks(configured.value)
        try:
            raise RuntimeError("전역 예외 테스트")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        for handler in configured.value.handlers:
            handler.flush()
        contents = log_path.read_text(encoding="utf-8")
        assert "Unhandled exception" in contents
        assert "RuntimeError: 전역 예외 테스트" in contents
        assert "Traceback (most recent call last)" in contents
    finally:
        sys.excepthook = previous
        _close_application_handlers(configured.value)


def test_daily_log_directory_is_outside_session_storage(tmp_path: Path) -> None:
    paths = ManagedPaths.from_root(tmp_path)

    assert daily_log_path(paths.root).parent == paths.logs_dir
    assert not daily_log_path(paths.root).is_relative_to(paths.data_dir)
