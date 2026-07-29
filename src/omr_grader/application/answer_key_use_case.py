"""Application adapter for answer-key validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from omr_grader.application.dto import AnswerKeyRequest, AnswerKeyValidation
from omr_grader.domain.errors import Err, Ok, Result
from omr_grader.domain.models import AnswerKeySnapshot
from omr_grader.workbooks.answer_key import import_answer_key


@dataclass(frozen=True, slots=True)
class AnswerKeyWorkbookUseCase:
    """Expose the strict workbook policy through the public AnswerKeyUseCase port."""

    loader: Callable[[str, str], Result[AnswerKeySnapshot]] = import_answer_key

    def validate_answer_key(self, request: AnswerKeyRequest) -> Result[AnswerKeyValidation]:
        snapshot = self.loader(request.path, request.sheet_name)
        if isinstance(snapshot, Err):
            return snapshot
        return Ok(AnswerKeyValidation(snapshot.value), snapshot.warnings)
