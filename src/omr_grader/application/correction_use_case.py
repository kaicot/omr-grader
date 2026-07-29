"""Correction preview and CAS commit orchestration over committed snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from omr_grader.application.dto import (
    CommitGenerationResult,
    CorrectionBatch,
    CorrectionSemanticView,
    EffectiveResponseProjection,
    GenerationMutation,
    ScoreInput,
    ScoreSet,
    SnapshotRef,
)
from omr_grader.application.ports import CommittedSnapshotLease, InternalSessionCoordinator
from omr_grader.domain.corrections import apply_correction_batch, project_effective_responses
from omr_grader.domain.enums import OperationKind, SessionState
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.grading import score_effective
from omr_grader.domain.models import AnswerKeySnapshot, EffectiveResponse


@dataclass(frozen=True, slots=True)
class CommittedCorrectionSnapshot:
    snapshot: SnapshotRef
    lease: CommittedSnapshotLease
    state: SessionState
    responses: tuple[EffectiveResponse, ...]
    answer_key: AnswerKeySnapshot
    projection_request: EffectiveResponseProjection


@dataclass(frozen=True, slots=True)
class CorrectionPreview:
    responses: tuple[EffectiveResponse, ...]
    scores: ScoreSet


class CommittedCorrectionSnapshotReader(Protocol):
    def read_correction_snapshot(
        self, session_id: str, expected_revision: int
    ) -> Result[CommittedCorrectionSnapshot]: ...


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))


class CorrectionApplicationService:
    """Retains one pinned preview lease through its matching CAS save."""

    def __init__(
        self, snapshots: CommittedCorrectionSnapshotReader, coordinator: InternalSessionCoordinator
    ) -> None:
        self._snapshots = snapshots
        self._coordinator = coordinator
        self._retained: dict[tuple[str, int, str], CommittedCorrectionSnapshot] = {}

    def preview_corrections(self, batch: CorrectionBatch) -> Result[CorrectionPreview]:
        key = (batch.session_id, batch.expected_revision, batch.idempotency_key)
        previous = self._retained.pop(key, None)
        if previous is not None:
            cleanup = self._close_error(previous)
            if cleanup is not None:
                return Err((cleanup,))
        committed = self._snapshots.read_correction_snapshot(
            batch.session_id, batch.expected_revision
        )
        if isinstance(committed, Err):
            return committed
        preview = self._preview(committed.value, batch)
        if isinstance(preview, Err):
            cleanup = self._close_error(committed.value)
            return Err(preview.errors + (() if cleanup is None else (cleanup,)))
        self._retained[key] = committed.value
        return preview

    def _preview(
        self, snapshot: CommittedCorrectionSnapshot, batch: CorrectionBatch
    ) -> Result[CorrectionPreview]:
        if (
            snapshot.snapshot.session_id != batch.session_id
            or snapshot.snapshot.revision != batch.expected_revision
            or snapshot.state not in (SessionState.GRADED, SessionState.FINALIZED)
        ):
            return _error("SESSION_STATE_INVALID", "session_id")
        authoritative = project_effective_responses(
            snapshot.projection_request,
            session_id=batch.session_id,
            expected_base_revision=batch.expected_revision,
        )
        if isinstance(authoritative, Err):
            return authoritative
        if authoritative.value != snapshot.responses:
            return _error("CORRECTION_PROJECTION_MISMATCH", "projection_request")
        responses = apply_correction_batch(
            authoritative.value,
            batch.edits,
            session_id=batch.session_id,
            expected_base_revision=batch.expected_revision,
        )
        if isinstance(responses, Err):
            return responses
        scores = score_effective(ScoreInput(responses.value, snapshot.answer_key))
        return Ok(CorrectionPreview(responses.value, scores))

    def save_corrections(self, batch: CorrectionBatch) -> Result[CommitGenerationResult]:
        key = (batch.session_id, batch.expected_revision, batch.idempotency_key)
        snapshot = self._retained.pop(key, None)
        if snapshot is None:
            committed = self._snapshots.read_correction_snapshot(
                batch.session_id, batch.expected_revision
            )
            if isinstance(committed, Err):
                return committed
            snapshot = committed.value
        preview = self._preview(snapshot, batch)
        if isinstance(preview, Err):
            cleanup = self._close_error(snapshot)
            return Err(preview.errors + (() if cleanup is None else (cleanup,)))
        projection = replace(
            snapshot.projection_request,
            corrections=snapshot.projection_request.corrections + batch.edits,
        )
        mutation = GenerationMutation(
            batch.session_id,
            batch.operation_id,
            OperationKind.CORRECT,
            batch.expected_revision,
            SessionState.GRADED,
            CorrectionSemanticView(batch.edits, snapshot.state),
            projection,
        )
        commit_result = self._coordinator.commit_generation(mutation)
        cleanup = self._close_error(snapshot)
        if isinstance(commit_result, Err):
            return Err(commit_result.errors + (() if cleanup is None else (cleanup,)))
        warning = self._warning(cleanup)
        return Ok(
            commit_result.value,
            commit_result.warnings + (() if warning is None else (warning,)),
        )

    @staticmethod
    def _close_error(snapshot: CommittedCorrectionSnapshot) -> ErrorInfo | None:
        try:
            closed = snapshot.lease.close()
        except BaseException as error:
            return ErrorInfo(
                "CORRECTION_LEASE_CLOSE_FAILED",
                "error.correction_lease_close_failed",
                context={"reason": str(error)},
            )
        if isinstance(closed, Err):
            issue = closed.errors[0]
            return ErrorInfo(
                "CORRECTION_LEASE_CLOSE_FAILED",
                "error.correction_lease_close_failed",
                issue.field_path,
                dict(issue.context),
                issue.retryable,
                issue.cause_type,
            )
        return None

    @staticmethod
    def _warning(error: ErrorInfo | None) -> ErrorInfo | None:
        if error is None:
            return None
        return ErrorInfo(
            error.code,
            f"warning.{error.code.lower()}",
            error.field_path,
            dict(error.context),
            error.retryable,
            error.cause_type,
        )

    def close_pending(self, session_id: str, revision: int) -> tuple[ErrorInfo, ...]:
        warnings: list[ErrorInfo] = []
        for key, snapshot in tuple(self._retained.items()):
            if key[:2] != (session_id, revision):
                continue
            del self._retained[key]
            cleanup = self._close_error(snapshot)
            warning = self._warning(cleanup)
            if warning is not None:
                warnings.append(warning)
        return tuple(warnings)


__all__ = [
    "CommittedCorrectionSnapshot",
    "CommittedCorrectionSnapshotReader",
    "CorrectionApplicationService",
    "CorrectionPreview",
]
