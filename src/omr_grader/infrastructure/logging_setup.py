"""Privacy-preserving application logging configured after portable bootstrap."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from omr_grader.domain.errors import ErrorInfo, Ok, Result

_LOGGER_NAME = "omr_grader"
_PRIVATE_VALUE = "[비공개]"
_STUDENT_ID = re.compile(r"(?<!\d)\d{8}(?!\d)")
_SENSITIVE_FIELD = re.compile(
    r"(?i)\b(?:name|student_name|student_id|cell|cell_value)\s*[:=]\s*[^,;\n]+"
)
_KOREAN_SENSITIVE_FIELD = re.compile(r"(?:이름|학번|셀\s*값?)\s*[:=]\s*[^,;\n]+")


def redact_log_text(value: object) -> str:
    """Remove probable student identifiers and labeled personal/cell data from logs."""
    text = str(value)
    text = _STUDENT_ID.sub(_PRIVATE_VALUE, text)
    text = _SENSITIVE_FIELD.sub(_PRIVATE_VALUE, text)
    return _KOREAN_SENSITIVE_FIELD.sub(_PRIVATE_VALUE, text)


class PrivacyFilter(logging.Filter):
    """Collapse formatting arguments and exception details before a handler sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_text(record.msg)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class _OMRGraderHandler(logging.Handler):
    """Application-owned handler that delegates to a concrete output handler."""

    def __init__(self, stream_or_path: Path | TextIO) -> None:
        super().__init__()
        if isinstance(stream_or_path, Path):
            self._delegate: logging.Handler = RotatingFileHandler(
                stream_or_path, encoding="utf-8", maxBytes=1_048_576, backupCount=3
            )
        else:
            self._delegate = logging.StreamHandler(stream_or_path)

    def emit(self, record: logging.LogRecord) -> None:
        self._delegate.emit(record)

    def close(self) -> None:
        try:
            self._delegate.close()
        finally:
            super().close()


def _handler(stream_or_path: Path | TextIO) -> _OMRGraderHandler:
    handler = _OMRGraderHandler(stream_or_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(PrivacyFilter())
    handler._delegate.setFormatter(handler.formatter)
    return handler


def configure_logging(log_path: Path | None = None) -> Result[logging.Logger]:
    """Configure stderr logging and add a portable rotating log only when supplied."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if isinstance(handler, _OMRGraderHandler):
            logger.removeHandler(handler)
            handler.close()

    logger.addHandler(_handler(sys.stderr))
    if log_path is None:
        return Ok(logger)
    try:
        logger.addHandler(_handler(log_path))
    except OSError as exc:
        warning = ErrorInfo(
            "LOGGING_SETUP_FAILED",
            "warning.logging_setup_failed",
            context={"reason": type(exc).__name__},
            cause_type=type(exc).__name__,
        )
        return Ok(logger, (warning,))
    return Ok(logger)


__all__ = ["PrivacyFilter", "configure_logging", "redact_log_text"]
