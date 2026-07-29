"""Application ports; public UI/worker use cases and internal coordinators."""

from __future__ import annotations

from typing import BinaryIO, Protocol

from omr_grader.domain.errors import Result
from omr_grader.domain.models import EffectiveResponse, SessionManifest

from .dto import (
    AnswerKeyRequest,
    AnswerKeyValidation,
    BackupExportRequest,
    BackupExportResult,
    BackupRestoreResult,
    BackupValidateRequest,
    CancelOperationCommand,
    CombinedReportRequest,
    CombinedReportResult,
    CommitGenerationResult,
    CorrectionBatch,
    EffectiveResponseProjection,
    FinalizeCommand,
    GenerationMutation,
    ImportResponseCommand,
    MetadataEditCommand,
    PermanentDeleteResult,
    ProfileImportRequest,
    ProfileImportResult,
    RecoveryReport,
    RecoveryRequest,
    RegradeCommand,
    ResponseBookRequest,
    ResponseBookValidation,
    RestoreCommand,
    ScanCommand,
    ScoreInput,
    ScoreSet,
    SessionCreateResult,
    SessionMutationRequest,
    SettingsSaveCommand,
    SettingsSaveResult,
    SnapshotRef,
    SnapshotRequest,
    SoftDeleteResult,
    TrashRestoreResult,
)
from .validation_token import ValidatedBackup


class ProfileUseCase(Protocol):
    def import_profile(self, request: ProfileImportRequest) -> Result[ProfileImportResult]: ...


class ScanUseCase(Protocol):
    def run_scan(self, command: ScanCommand) -> Result[SessionCreateResult]: ...

    def cancel_scan(self, command: CancelOperationCommand) -> Result[None]: ...


class ResponseImportUseCase(Protocol):
    def validate_response_book(
        self, request: ResponseBookRequest
    ) -> Result[ResponseBookValidation]: ...

    def import_response_book(
        self, command: ImportResponseCommand
    ) -> Result[SessionCreateResult]: ...


class CorrectionUseCase(Protocol):
    def save_corrections(self, batch: CorrectionBatch) -> Result[CommitGenerationResult]: ...


class GradingUseCase(Protocol):
    def regrade(self, command: RegradeCommand) -> Result[CommitGenerationResult]: ...

    def finalize(self, command: FinalizeCommand) -> Result[CommitGenerationResult]: ...


class DashboardUseCase(Protocol):
    def build_combined_report(
        self, request: CombinedReportRequest
    ) -> Result[CombinedReportResult]: ...


class BackupUseCase(Protocol):
    def export_backup(self, request: BackupExportRequest) -> Result[BackupExportResult]: ...

    def validate_backup(self, request: BackupValidateRequest) -> Result[ValidatedBackup]: ...

    def restore_backup(self, command: RestoreCommand) -> Result[BackupRestoreResult]: ...


class CommittedSnapshotLease(Protocol):
    @property
    def snapshot_ref(self) -> SnapshotRef: ...

    @property
    def manifest(self) -> SessionManifest: ...

    @property
    def root_path(self) -> str: ...

    def open_allowlisted(self, relpath: str) -> Result[BinaryIO]: ...

    def close(self) -> Result[None]: ...


class InternalSessionCoordinator(Protocol):
    def open_committed_snapshot(
        self, request: SnapshotRequest
    ) -> Result[CommittedSnapshotLease]: ...

    def commit_generation(self, mutation: GenerationMutation) -> Result[CommitGenerationResult]: ...

    def recover_sessions(self, request: RecoveryRequest) -> Result[RecoveryReport]: ...

    def soft_delete(self, request: SessionMutationRequest) -> Result[SoftDeleteResult]: ...

    def restore_from_trash(self, request: SessionMutationRequest) -> Result[TrashRestoreResult]: ...

    def permanently_delete(
        self, request: SessionMutationRequest
    ) -> Result[PermanentDeleteResult]: ...


class AnswerKeyUseCase(Protocol):
    def validate_answer_key(self, request: AnswerKeyRequest) -> Result[AnswerKeyValidation]: ...


class EffectiveResponseProjector(Protocol):
    def project_effective_responses(
        self, projection: EffectiveResponseProjection
    ) -> Result[tuple[EffectiveResponse, ...]]: ...


class ScoringService(Protocol):
    def score_effective(self, score_input: ScoreInput) -> Result[ScoreSet]: ...


class SettingsUseCase(Protocol):
    def save_settings(self, command: SettingsSaveCommand) -> Result[SettingsSaveResult]: ...


class SessionLifecycleUseCase(Protocol):
    def cancel_operation(self, command: CancelOperationCommand) -> Result[None]: ...

    def finalize(self, command: FinalizeCommand) -> Result[CommitGenerationResult]: ...

    def delete_session(self, request: SessionMutationRequest) -> Result[SoftDeleteResult]: ...

    def restore_session(self, request: SessionMutationRequest) -> Result[TrashRestoreResult]: ...

    def permanently_delete_session(
        self, request: SessionMutationRequest
    ) -> Result[PermanentDeleteResult]: ...


class MetadataUseCase(Protocol):
    def edit_metadata(self, command: MetadataEditCommand) -> Result[CommitGenerationResult]: ...


__all__ = [
    "AnswerKeyUseCase",
    "BackupUseCase",
    "CorrectionUseCase",
    "DashboardUseCase",
    "EffectiveResponseProjector",
    "GradingUseCase",
    "MetadataUseCase",
    "ProfileUseCase",
    "ResponseImportUseCase",
    "ScanUseCase",
    "ScoringService",
    "SessionLifecycleUseCase",
    "SettingsUseCase",
]
