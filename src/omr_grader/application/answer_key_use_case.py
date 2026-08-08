"""Application adapter for answer-key validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omr_grader.application.dto import AnswerKeyRequest, AnswerKeyValidation
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import AnswerKeySnapshot
from omr_grader.infrastructure.io_retry import retry_io
from omr_grader.workbooks.answer_key import import_answer_key


@dataclass(frozen=True, slots=True)
class AnswerKeyWorkbookUseCase:
    """Expose the strict workbook policy through the public AnswerKeyUseCase port."""

    loader: Callable[[str, str], Result[AnswerKeySnapshot]] = import_answer_key

    def validate_answer_key(self, request: AnswerKeyRequest) -> Result[AnswerKeyValidation]:
        snapshot = self.loader(request.path, request.sheet_name)
        if isinstance(snapshot, Err):
            return snapshot
        source = Path(request.path)
        try:
            source_bytes = retry_io(source.read_bytes)
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
