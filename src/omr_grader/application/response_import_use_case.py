"""Pinned response-workbook validation and new-session import orchestration."""

from __future__ import annotations

from typing import Protocol

from omr_grader.application.dto import (
    ImportResponseCommand,
    ResponseBookRequest,
    ResponseBookValidation,
    SessionCreateResult,
)
from omr_grader.application.validation_token import ResponseValidationToken
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import ImportedResponseRef
from omr_grader.workbooks.response_import import parse_response_book


class ResponseImportCommitCoordinator(Protocol):
    """Authority boundary: only the session store may publish an import session."""

    def commit_imported_responses(
        self,
        command: ImportResponseCommand,
        *,
        exam_name: str,
        exam_year: int | None,
        exam_term: ExamTerm,
        source_sha256: str,
        sheet_name: str,
        rows: tuple[ImportedResponseRef, ...],
    ) -> Result[SessionCreateResult]: ...


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))


class ResponseImportUseCase:
    """Validate a held source, then consume it once for an atomic authority commit."""

    def __init__(self, coordinator: ResponseImportCommitCoordinator) -> None:
        self._coordinator = coordinator
        self._validated: dict[int, ResponseBookRequest] = {}

    def validate_response_book(
        self, request: ResponseBookRequest
    ) -> Result[ResponseBookValidation]:
        opened = ResponseValidationToken.open(request.path)
        if not isinstance(opened, Ok):
            return opened
        token = opened.value
        parsed = parse_response_book(
            token._handle,  # The live token owns this pinned handle; no path is reopened.
            sheet_name=request.sheet_name,
            session_id="response-validation",
            source_sha256=token.source_sha256,
        )
        if not isinstance(parsed, Ok):
            token.close()
            return parsed
        self._validated[id(token)] = request
        return Ok(
            ResponseBookValidation(token.source_sha256, len(parsed.value), parsed.value, token)
        )

    def import_response_book(self, command: ImportResponseCommand) -> Result[SessionCreateResult]:
        request = self._validated.pop(id(command.validation_token), None)
        if request is None:
            return _error("XLSX_SOURCE_CHANGED", "validation_token")
        consumed = command.validation_token.consume_for_import()
        if not isinstance(consumed, Ok):
            command.validation_token.close()
            return consumed
        try:
            parsed = parse_response_book(
                consumed.value,
                sheet_name=request.sheet_name,
                session_id=command.session_id,
                source_sha256=command.validation_token.source_sha256,
            )
            if not isinstance(parsed, Ok):
                return parsed
            return self._coordinator.commit_imported_responses(
                command,
                exam_name=request.exam_name,
                exam_year=request.exam_year,
                exam_term=request.exam_term,
                source_sha256=command.validation_token.source_sha256,
                sheet_name=request.sheet_name,
                rows=parsed.value,
            )
        finally:
            command.validation_token.close()


__all__ = ["ResponseImportCommitCoordinator", "ResponseImportUseCase"]
