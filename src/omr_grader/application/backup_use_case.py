"""Backup application service with a deliberately injected restore authority."""

from __future__ import annotations

from typing import Protocol

from omr_grader.application.dto import (
    BackupExportRequest,
    BackupExportResult,
    BackupRestoreResult,
    BackupValidateRequest,
    CollisionPolicy,
    RestoreCommand,
)
from omr_grader.application.ports import InternalSessionCoordinator
from omr_grader.application.validation_token import ValidatedBackup
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.backup_archive import BackupArchive, ExtractedBackup


class RestorePublisher(Protocol):
    """Private main-process authority for atomically publishing restored sessions."""

    def publish_restored(
        self,
        extracted: ExtractedBackup,
        target_root: str,
        operation_id: str,
    ) -> Result[BackupRestoreResult]: ...


def _error(code: str, path: str = "") -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path=path or None),))


class BackupApplicationService:
    """Concrete implementation of the frozen :class:`BackupUseCase` contract."""

    def __init__(
        self,
        coordinator: InternalSessionCoordinator,
        *,
        archive: BackupArchive | None = None,
        restore_publisher: RestorePublisher | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._archive = archive or BackupArchive()
        self._restore_publisher = restore_publisher

    def export_backup(self, request: BackupExportRequest) -> Result[BackupExportResult]:
        opened = self._coordinator.open_committed_snapshot(request.snapshot)
        if isinstance(opened, Err):
            return opened
        lease = opened.value
        try:
            exported = self._archive.export(
                lease,
                request.destination,
                replace=request.collision is CollisionPolicy.REPLACE,
            )
            if isinstance(exported, Err):
                return exported
            return Ok(
                BackupExportResult(
                    published=True,
                    destination=request.destination,
                    destination_sha256=exported.value,
                    operation_id=request.operation_id,
                )
            )
        finally:
            lease.close()

    def validate_backup(self, request: BackupValidateRequest) -> Result[ValidatedBackup]:
        opened = ValidatedBackup.open(request.source_path)
        if isinstance(opened, Err):
            return opened
        token = opened.value
        checked = self._archive.preflight(token)
        if isinstance(checked, Err):
            token.close()
            return checked
        return Ok(token)

    def restore_backup(self, command: RestoreCommand) -> Result[BackupRestoreResult]:
        token = command.validated
        publisher = self._restore_publisher
        if publisher is None:
            token.close()
            return _error("BACKUP_RESTORE_PUBLISHER_REQUIRED", command.target_root)
        extracted: ExtractedBackup | None = None

        def restore_once() -> Result[BackupRestoreResult]:
            nonlocal extracted
            consumed = token.consume_for_restore()
            if isinstance(consumed, Err):
                return consumed
            extracted_result = self._archive.extract(
                consumed.value,
                command.target_root,
                archive_sha256=token.source_sha256,
                source_identity=token.source_identity,
            )
            if isinstance(extracted_result, Err):
                return extracted_result
            extracted = extracted_result.value
            revalidated = token.revalidate()
            if isinstance(revalidated, Err):
                return revalidated
            return publisher.publish_restored(
                extracted,
                command.target_root,
                command.operation_id,
            )

        cleanup_error: Err | None = None
        try:
            outcome = restore_once()
        finally:
            if extracted is not None:
                discarded = self._archive.discard(
                    extracted.staging_root, extracted.staging_ownership
                )
                if isinstance(discarded, Err):
                    cleanup_error = discarded
            token.close()
        if cleanup_error is None:
            return outcome
        if isinstance(outcome, Err):
            return Err((*outcome.errors, *cleanup_error.errors))
        return Ok(
            outcome.value,
            (
                *outcome.warnings,
                *(
                    ErrorInfo(
                        error.code,
                        f"warning.{error.code.lower()}",
                        field_path=error.field_path,
                        context=dict(error.context),
                        retryable=error.retryable,
                        cause_type=error.cause_type,
                    )
                    for error in cleanup_error.errors
                ),
            ),
        )


BackupUseCaseService = BackupApplicationService

__all__ = ["BackupApplicationService", "BackupUseCaseService", "RestorePublisher"]
