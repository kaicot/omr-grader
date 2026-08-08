"""Committed-snapshot grading and finalization orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from omr_grader.domain.enums import OperationKind, SessionState
from omr_grader.domain.errors import Err, ErrorInfo, Result
from omr_grader.domain.grading import score_effective
from omr_grader.domain.models import AnswerKeySnapshot, EffectiveResponse

from .dto import (
    AnswerKeyRequest,
    CommitGenerationResult,
    EffectiveResponseProjection,
    FinalizeCommand,
    GenerationMutation,
    GradingSemanticView,
    RegradeCommand,
    ScoreInput,
    ScoreSet,
)
from .ports import AnswerKeyUseCase, InternalSessionCoordinator


@dataclass(frozen=True, slots=True)
class CommittedGradingSnapshot:
    """The only grading inputs a use case may consume for a committed revision."""

    state: SessionState
    answer_key: AnswerKeySnapshot
    responses: tuple[EffectiveResponse, ...]
    projection_request: EffectiveResponseProjection | None
    scores: ScoreSet | None


class CommittedGradingSnapshotReader(Protocol):
    """Adapter backed by a frozen generation, never by a projection workbook."""

    def read_grading_snapshot(
        self, session_id: str, expected_revision: int
    ) -> Result[CommittedGradingSnapshot]: ...


class GradingUseCase:
    def __init__(
        self,
        snapshots: CommittedGradingSnapshotReader,
        answer_keys: AnswerKeyUseCase,
        coordinator: InternalSessionCoordinator,
    ) -> None:
        self._snapshots = snapshots
        self._answer_keys = answer_keys
        self._coordinator = coordinator

    def regrade(
        self,
        command: RegradeCommand,
        progress: Callable[[int, int], None] | None = None,
    ) -> Result[CommitGenerationResult]:
        committed = self._snapshots.read_grading_snapshot(
            command.session_id, command.expected_revision
        )
        if isinstance(committed, Err):
            return committed
        validated = self._answer_keys.validate_answer_key(
            AnswerKeyRequest(command.answer_key_path, command.answer_key_sheet)
        )
        if isinstance(validated, Err):
            return validated
        snapshot = committed.value
        scores = score_effective(
            ScoreInput(snapshot.responses, validated.value.snapshot), progress
        )
        source_artifacts = (
            (
                (
                    f"sources/answer_keys/{validated.value.source_name}",
                    validated.value.source_bytes,
                ),
            )
            if validated.value.source_name is not None
            and validated.value.source_bytes is not None
            else ()
        )
        mutation = GenerationMutation(
            command.session_id,
            command.operation_id,
            OperationKind.REGRADE,
            command.expected_revision,
            SessionState.GRADED,
            GradingSemanticView(validated.value.snapshot, snapshot.state, scores),
            None,
            source_artifacts,
        )
        return self._coordinator.commit_generation(mutation)

    def finalize(self, command: FinalizeCommand) -> Result[CommitGenerationResult]:
        committed = self._snapshots.read_grading_snapshot(
            command.session_id, command.expected_revision
        )
        if isinstance(committed, Err):
            return committed
        snapshot = committed.value
        if snapshot.state is not SessionState.GRADED:
            return _error("SESSION_STATE_INVALID", "session_id")
        scores = snapshot.scores or score_effective(
            ScoreInput(snapshot.responses, snapshot.answer_key)
        )
        mutation = GenerationMutation(
            command.session_id,
            command.operation_id,
            OperationKind.FINALIZE,
            command.expected_revision,
            SessionState.FINALIZED,
            GradingSemanticView(snapshot.answer_key, snapshot.state, scores),
            None,
        )
        return self._coordinator.commit_generation(mutation)


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))


__all__ = ["CommittedGradingSnapshot", "CommittedGradingSnapshotReader", "GradingUseCase"]
