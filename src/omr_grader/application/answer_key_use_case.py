"""Application adapter for answer-key validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omr_grader.application.dto import AnswerKeyRequest, AnswerKeyValidation
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import AnswerKeySnapshot
from omr_grader.infrastructure.io_retry import retry_io
from omr_grader.workbooks.answer_key import import_answer_key_bytes


@dataclass(frozen=True, slots=True)
class AnswerKeyWorkbookUseCase:
    """Expose the strict workbook policy through the public AnswerKeyUseCase port."""

    loader: Callable[[bytes, str, str], Result[AnswerKeySnapshot]] = import_answer_key_bytes
    reader: Callable[[Path], bytes] = lambda path: retry_io(path.read_bytes)

    def validate_answer_key(self, request: AnswerKeyRequest) -> Result[AnswerKeyValidation]:
        source = Path(request.path)
        try:
            source_bytes = self.reader(source)
            snapshot = self.loader(source_bytes, source.name, request.sheet_name)
            if isinstance(snapshot, Err):
                return snapshot
            validation = AnswerKeyValidation(snapshot.value, source.name, source_bytes)
        except (OSError, ValueError) as error:
            return Err(
                (
                    ErrorInfo(
                        "ANSWER_KEY_SOURCE_READ_FAILED",
                        "error.answer_key_source_read_failed",
                        "path",
                        context={"reason": str(error)},
                        cause_type=type(error).__name__,
                    ),
                )
            )
        return Ok(validation, snapshot.warnings)
