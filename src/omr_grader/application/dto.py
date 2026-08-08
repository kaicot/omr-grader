"""Validated application-boundary request and payload contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import cast

from omr_grader.domain.enums import (
    CleanupState,
    ExamTerm,
    IndexState,
    OperationKind,
    SessionState,
    SnapshotPurpose,
)
from omr_grader.domain.models import (
    AnswerKeySnapshot,
    AutomaticPage,
    CorrectionDraft,
    EffectiveResponse,
    ImportedResponseRef,
    RosterSnapshot,
    SessionRecord,
    validate_portable_component,
)

from .validation_token import ResponseValidationToken, ValidatedBackup

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")


def _identifier(value: str, name: str) -> None:
    _text(value, name)


def _session_id(value: str) -> None:
    _text(value, "session_id")
    validate_portable_component(value)


def _revision(value: int, name: str = "expected_revision") -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _creation_revision(value: int, name: str = "expected_revision") -> None:
    if type(value) is not int or value != 0:
        raise ValueError(f"{name} must be exactly 0")


def _nonnegative_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative Decimal")


def _year(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 2000 <= value <= 2100):
        raise ValueError("exam_year must be between 2000 and 2100")


def _hash(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _count(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _operation(operation_id: str) -> None:
    _identifier(operation_id, "operation_id")


def _committed(value: bool) -> None:
    if value is not True:
        raise ValueError("successful result must be committed")


def _published(value: bool) -> None:
    if value is not True:
        raise ValueError("successful result must be published")


class CollisionPolicy(StrEnum):
    ERROR = "error"
    REPLACE = "replace"
    RENAME = "rename"


def _default_profile(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("default_profile must be a safe .omrtemplate basename or empty")
    if value == "":
        return
    try:
        validate_portable_component(value)
    except ValueError as error:
        raise ValueError("default_profile must be a safe .omrtemplate basename or empty") from error
    if not value.lower().endswith(".omrtemplate") or value.lower() == ".omrtemplate":
        raise ValueError("default_profile must be a safe .omrtemplate basename or empty")


def _non_renaming_collision(value: CollisionPolicy) -> None:
    if not isinstance(value, CollisionPolicy):
        raise TypeError("collision must be CollisionPolicy")
    if value is CollisionPolicy.RENAME:
        raise ValueError("collision must be ERROR or REPLACE")


@dataclass(frozen=True, slots=True)
class ProfileImportRequest:
    source_path: str
    collision: CollisionPolicy
    new_name: str | None
    capability_token: str

    def __post_init__(self) -> None:
        _text(self.source_path, "source_path")
        if not isinstance(self.collision, CollisionPolicy):
            raise TypeError("collision must be CollisionPolicy")
        if self.new_name is not None:
            _text(self.new_name, "new_name")
        _text(self.capability_token, "capability_token")


@dataclass(frozen=True, slots=True)
class ProfileImportResult:
    stored_name: str
    profile_sha256: str

    def __post_init__(self) -> None:
        _text(self.stored_name, "stored_name")
        _hash(self.profile_sha256, "profile_sha256")


@dataclass(frozen=True, slots=True)
class ScanSource:
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.paths:
            raise ValueError("paths must be nonempty")
        for path in self.paths:
            _text(path, "source path")


@dataclass(frozen=True, slots=True)
class ScanCommand:
    session_id: str
    operation_id: str
    expected_revision: int
    exam_name: str
    exam_year: int | None
    exam_term: ExamTerm
    profile_path: str
    roster_path: str | None
    source: ScanSource
    sensitivity: int
    multiprocessing: bool

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _operation(self.operation_id)
        _creation_revision(self.expected_revision)
        _text(self.exam_name, "exam_name")
        _year(self.exam_year)
        if not isinstance(self.exam_term, ExamTerm):
            raise TypeError("exam_term must be ExamTerm")
        _text(self.profile_path, "profile_path")
        if self.roster_path is not None:
            _text(self.roster_path, "roster_path")
        if not isinstance(self.source, ScanSource):
            raise TypeError("source must be ScanSource")
        if type(self.sensitivity) is not int or not 1 <= self.sensitivity <= 10:
            raise ValueError("sensitivity must be an integer from 1 through 10")
        if type(self.multiprocessing) is not bool:
            raise TypeError("multiprocessing must be bool")


@dataclass(frozen=True, slots=True)
class ScanProgress:
    completed: int
    total: int
    failed: int
    elapsed_ms: int
    eta_ms: int | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.completed, "completed"),
            (self.total, "total"),
            (self.failed, "failed"),
            (self.elapsed_ms, "elapsed_ms"),
        ):
            _count(value, name)
        if self.completed + self.failed > self.total:
            raise ValueError("completed and failed counts exceed total")
        if self.eta_ms is not None:
            _count(self.eta_ms, "eta_ms")


@dataclass(frozen=True, slots=True)
class CancelOperationCommand:
    operation_id: str

    def __post_init__(self) -> None:
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class SessionCreateResult:
    committed: bool
    session_id: str
    revision: int
    generation_id: str
    index_state: IndexState
    operation_id: str

    def __post_init__(self) -> None:
        _committed(self.committed)
        _session_id(self.session_id)
        _revision(self.revision, "revision")
        _identifier(self.generation_id, "generation_id")
        if not isinstance(self.index_state, IndexState):
            raise TypeError("index_state must be IndexState")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class ResponseBookRequest:
    path: str
    sheet_name: str
    exam_name: str
    exam_year: int | None
    exam_term: ExamTerm

    def __post_init__(self) -> None:
        _text(self.path, "path")
        _text(self.sheet_name, "sheet_name")
        _text(self.exam_name, "exam_name")
        _year(self.exam_year)
        if not isinstance(self.exam_term, ExamTerm):
            raise TypeError("exam_term must be ExamTerm")


@dataclass(frozen=True, slots=True)
class ResponseBookValidation:
    source_sha256: str
    row_count: int
    normalized_rows: tuple[ImportedResponseRef, ...]
    validation_token: ResponseValidationToken

    def __post_init__(self) -> None:
        _hash(self.source_sha256, "source_sha256")
        _count(self.row_count, "row_count")
        if self.row_count != len(self.normalized_rows) or not all(
            isinstance(row, ImportedResponseRef) for row in self.normalized_rows
        ):
            raise ValueError("normalized rows must match row_count")
        if not isinstance(self.validation_token, ResponseValidationToken):
            raise TypeError("validation_token must be ResponseValidationToken")


@dataclass(frozen=True, slots=True)
class ImportResponseCommand:
    validation_token: ResponseValidationToken
    session_id: str
    operation_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.validation_token, ResponseValidationToken):
            raise TypeError("validation_token must be ResponseValidationToken")
        _session_id(self.session_id)
        _operation(self.operation_id)
        _creation_revision(self.expected_revision)


@dataclass(frozen=True, slots=True)
class CorrectionBatch:
    session_id: str
    expected_revision: int
    idempotency_key: str
    operation_id: str
    edits: tuple[CorrectionDraft, ...]

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _revision(self.expected_revision)
        _text(self.idempotency_key, "idempotency_key")
        _operation(self.operation_id)
        if not self.edits or not all(isinstance(edit, CorrectionDraft) for edit in self.edits):
            raise ValueError("edits must be a nonempty tuple of CorrectionDraft")


@dataclass(frozen=True, slots=True)
class RegradeCommand:
    session_id: str
    expected_revision: int
    answer_key_path: str
    answer_key_sheet: str
    operation_id: str

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _revision(self.expected_revision)
        _text(self.answer_key_path, "answer_key_path")
        _text(self.answer_key_sheet, "answer_key_sheet")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class FinalizeCommand:
    session_id: str
    expected_revision: int
    operation_id: str

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _revision(self.expected_revision)
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class CommitGenerationResult:
    committed: bool
    session_id: str
    revision: int
    generation_id: str
    index_state: IndexState
    operation_id: str

    def __post_init__(self) -> None:
        SessionCreateResult(
            self.committed,
            self.session_id,
            self.revision,
            self.generation_id,
            self.index_state,
            self.operation_id,
        )


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    session_id: str
    revision: int | None
    purpose: SnapshotPurpose

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        if self.revision is not None:
            _revision(self.revision, "revision")
        if not isinstance(self.purpose, SnapshotPurpose):
            raise TypeError("purpose must be SnapshotPurpose")


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    session_id: str
    revision: int
    generation_id: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _revision(self.revision, "revision")
        _identifier(self.generation_id, "generation_id")
        _hash(self.manifest_sha256, "manifest_sha256")


@dataclass(frozen=True, slots=True)
class CombinedReportRequest:
    session_ids: tuple[str, ...]
    graded_only: bool
    destination: str
    collision: CollisionPolicy
    operation_id: str

    def __post_init__(self) -> None:
        if not self.session_ids:
            raise ValueError("session_ids must be nonempty")
        for session_id in self.session_ids:
            _identifier(session_id, "session_id")
        if type(self.graded_only) is not bool:
            raise TypeError("graded_only must be bool")
        _text(self.destination, "destination")
        _non_renaming_collision(self.collision)
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class CombinedReportResult:
    published: bool
    path: str
    sha256: str
    frozen: tuple[SnapshotRef, ...]
    operation_id: str

    def __post_init__(self) -> None:
        _published(self.published)
        _text(self.path, "path")
        _hash(self.sha256, "sha256")
        if not self.frozen or not all(
            isinstance(snapshot, SnapshotRef) for snapshot in self.frozen
        ):
            raise ValueError("frozen must be nonempty SnapshotRef values")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class BackupExportRequest:
    snapshot: SnapshotRequest
    destination: str
    collision: CollisionPolicy
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, SnapshotRequest):
            raise TypeError("snapshot must be SnapshotRequest")
        _text(self.destination, "destination")
        _non_renaming_collision(self.collision)
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class BackupExportResult:
    published: bool
    destination: str
    destination_sha256: str
    operation_id: str

    def __post_init__(self) -> None:
        _published(self.published)
        _text(self.destination, "destination")
        _hash(self.destination_sha256, "destination_sha256")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class BackupValidateRequest:
    source_path: str

    def __post_init__(self) -> None:
        _text(self.source_path, "source_path")


@dataclass(frozen=True, slots=True)
class RestoreCommand:
    validated: ValidatedBackup
    target_root: str
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.validated, ValidatedBackup):
            raise TypeError("validated must be ValidatedBackup")
        _text(self.target_root, "target_root")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class BackupRestoreResult:
    committed: bool
    session_id: str
    revision: int
    generation_id: str
    index_state: IndexState
    operation_id: str

    def __post_init__(self) -> None:
        SessionCreateResult(
            self.committed,
            self.session_id,
            self.revision,
            self.generation_id,
            self.index_state,
            self.operation_id,
        )


@dataclass(frozen=True, slots=True)
class RecognitionSemanticView:
    profile_sha256: str
    roster: RosterSnapshot
    pages: tuple[AutomaticPage, ...]
    state: SessionState

    def __post_init__(self) -> None:
        _hash(self.profile_sha256, "profile_sha256")
        if not isinstance(self.roster, RosterSnapshot) or not all(
            isinstance(page, AutomaticPage) for page in self.pages
        ):
            raise TypeError("recognition view must contain exact domain snapshots")
        if not isinstance(self.state, SessionState):
            raise TypeError("state must be SessionState")


@dataclass(frozen=True, slots=True)
class ImportSemanticView:
    responses: tuple[ImportedResponseRef, ...]
    roster: RosterSnapshot
    state: SessionState

    def __post_init__(self) -> None:
        if not self.responses or not all(
            isinstance(row, ImportedResponseRef) for row in self.responses
        ):
            raise ValueError("responses must be nonempty ImportedResponseRef values")
        if not isinstance(self.roster, RosterSnapshot) or not isinstance(self.state, SessionState):
            raise TypeError("import view must contain exact domain snapshots and state")


@dataclass(frozen=True, slots=True)
class CorrectionSemanticView:
    corrections: tuple[CorrectionDraft, ...]
    state: SessionState

    def __post_init__(self) -> None:
        if not self.corrections or not all(
            isinstance(item, CorrectionDraft) for item in self.corrections
        ):
            raise ValueError("corrections must be nonempty CorrectionDraft values")
        if not isinstance(self.state, SessionState):
            raise TypeError("state must be SessionState")


@dataclass(frozen=True, slots=True)
class GradingSemanticView:
    answer_key: AnswerKeySnapshot
    state: SessionState
    scores: ScoreSet | None

    def __post_init__(self) -> None:
        if not isinstance(self.answer_key, AnswerKeySnapshot) or not isinstance(
            self.state, SessionState
        ):
            raise TypeError("grading view must contain exact domain snapshots and state")
        if self.scores is not None and not isinstance(self.scores, ScoreSet):
            raise TypeError("scores must be ScoreSet or None")


@dataclass(frozen=True, slots=True)
class MetadataSemanticView:
    session: SessionRecord

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionRecord):
            raise TypeError("metadata view must contain a SessionRecord")


type GenerationSemanticInputs = (
    RecognitionSemanticView
    | ImportSemanticView
    | CorrectionSemanticView
    | GradingSemanticView
    | MetadataSemanticView
)


@dataclass(frozen=True, slots=True)
class EffectiveResponseProjection:
    automatic_pages: tuple[AutomaticPage, ...]
    imported_responses: tuple[ImportedResponseRef, ...]
    corrections: tuple[CorrectionDraft, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(page, AutomaticPage) for page in self.automatic_pages):
            raise TypeError("automatic_pages must be AutomaticPage snapshots")
        if not all(isinstance(row, ImportedResponseRef) for row in self.imported_responses):
            raise TypeError("imported_responses must be ImportedResponseRef snapshots")
        if not all(isinstance(item, CorrectionDraft) for item in self.corrections):
            raise TypeError("corrections must be CorrectionDraft values")


@dataclass(frozen=True, slots=True)
class GenerationMutation:
    session_id: str
    operation_id: str
    operation_kind: OperationKind
    expected_revision: int
    target_state: SessionState
    semantic_inputs: GenerationSemanticInputs
    projection_request: EffectiveResponseProjection | None
    source_artifacts: tuple[tuple[str, bytes], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _operation(self.operation_id)
        _revision(self.expected_revision)
        if not isinstance(self.operation_kind, OperationKind):
            raise TypeError("operation_kind must be OperationKind")
        if self.operation_kind is OperationKind.CREATE:
            raise ValueError("GenerationMutation does not support CREATE")
        if self.operation_kind is OperationKind.IMPORT_RESPONSES:
            raise ValueError("IMPORT_RESPONSES is a revision-zero session creation operation")
        paths: set[str] = set()
        for path, payload in self.source_artifacts:
            parts = path.split("/")
            if not path or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("source artifact path is invalid")
            for part in parts:
                validate_portable_component(part)
            if path in paths or type(payload) is not bytes or not payload:
                raise ValueError("source artifacts must contain unique pinned bytes")
            paths.add(path)
        if not isinstance(self.target_state, SessionState):
            raise TypeError("target_state must be SessionState")
        if not isinstance(
            self.semantic_inputs,
            RecognitionSemanticView
            | ImportSemanticView
            | CorrectionSemanticView
            | GradingSemanticView
            | MetadataSemanticView,
        ):
            raise TypeError("semantic_inputs must be a typed semantic view")
        if self.projection_request is not None and not isinstance(
            self.projection_request, EffectiveResponseProjection
        ):
            raise TypeError("projection_request must be EffectiveResponseProjection")

        if self.operation_kind is OperationKind.RECOGNIZE:
            if not isinstance(self.semantic_inputs, RecognitionSemanticView):
                raise ValueError("semantic input does not match operation kind")
            if (
                self.semantic_inputs.state is not SessionState.CREATED
                or self.target_state is not SessionState.RECOGNIZED
            ):
                raise ValueError("RECOGNIZE requires CREATED to RECOGNIZED")
            return
        if self.operation_kind is OperationKind.CORRECT:
            if not isinstance(self.semantic_inputs, CorrectionSemanticView):
                raise ValueError("semantic input does not match operation kind")
            if (
                self.semantic_inputs.state not in (SessionState.GRADED, SessionState.FINALIZED)
                or self.target_state is not SessionState.GRADED
            ):
                raise ValueError("CORRECT requires GRADED or FINALIZED to GRADED")
            return
        if self.operation_kind is OperationKind.REGRADE:
            if not isinstance(self.semantic_inputs, GradingSemanticView):
                raise ValueError("semantic input does not match operation kind")
            if (
                self.semantic_inputs.state
                not in (SessionState.RECOGNIZED, SessionState.GRADED, SessionState.FINALIZED)
                or self.target_state is not SessionState.GRADED
            ):
                raise ValueError("REGRADE requires RECOGNIZED, GRADED, or FINALIZED to GRADED")
            return
        if self.operation_kind is OperationKind.FINALIZE:
            if not isinstance(self.semantic_inputs, GradingSemanticView):
                raise ValueError("semantic input does not match operation kind")
            if (
                self.semantic_inputs.state is not SessionState.GRADED
                or self.target_state is not SessionState.FINALIZED
                or self.semantic_inputs.scores is None
            ):
                raise ValueError("FINALIZE requires scored GRADED to FINALIZED")
            return
        if self.operation_kind is OperationKind.METADATA_EDIT:
            if not isinstance(self.semantic_inputs, MetadataSemanticView):
                raise ValueError("semantic input does not match operation kind")
            if self.target_state is not self.semantic_inputs.session.state:
                raise ValueError("METADATA_EDIT must preserve the current state")
            return
        raise AssertionError("unreachable closed OperationKind")


@dataclass(frozen=True, slots=True)
class MetadataEditCommand:
    session_id: str
    expected_revision: int
    operation_id: str
    exam_name: str
    exam_year: int | None
    exam_term: ExamTerm

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _revision(self.expected_revision)
        _operation(self.operation_id)
        _text(self.exam_name, "exam_name")
        _year(self.exam_year)
        if not isinstance(self.exam_term, ExamTerm):
            raise TypeError("exam_term must be ExamTerm")


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    root: str
    rebuild_index: bool
    cleanup_orphans: bool
    operation_id: str

    def __post_init__(self) -> None:
        _text(self.root, "root")
        if type(self.rebuild_index) is not bool or type(self.cleanup_orphans) is not bool:
            raise TypeError("recovery flags must be bool")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class RecoveryIssue:
    session_id: str | None
    code: str
    detail: str

    def __post_init__(self) -> None:
        if self.session_id is not None:
            _session_id(self.session_id)
        _text(self.code, "code")
        _text(self.detail, "detail")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    sessions_scanned: int
    index_state: IndexState
    quarantined: tuple[RecoveryIssue, ...]
    cleaned: tuple[RecoveryIssue, ...]

    def __post_init__(self) -> None:
        _count(self.sessions_scanned, "sessions_scanned")
        if not isinstance(self.index_state, IndexState):
            raise TypeError("index_state must be IndexState")
        if not all(isinstance(item, RecoveryIssue) for item in (*self.quarantined, *self.cleaned)):
            raise TypeError("recovery issues must be RecoveryIssue values")


@dataclass(frozen=True, slots=True)
class AnswerKeyRequest:
    path: str
    sheet_name: str

    def __post_init__(self) -> None:
        _text(self.path, "path")
        _text(self.sheet_name, "sheet_name")


@dataclass(frozen=True, slots=True)
class AnswerKeyValidation:
    snapshot: AnswerKeySnapshot
    source_name: str | None = None
    source_bytes: bytes | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AnswerKeySnapshot):
            raise TypeError("snapshot must be AnswerKeySnapshot")
        if (self.source_name is None) != (self.source_bytes is None):
            raise ValueError("answer-key source name and bytes must be provided together")
        if self.source_name is not None:
            validate_portable_component(self.source_name)
            if type(self.source_bytes) is not bytes or not self.source_bytes:
                raise ValueError("answer-key source bytes must be nonempty")


@dataclass(frozen=True, slots=True)
class ScoreInput:
    responses: tuple[EffectiveResponse, ...]
    key: AnswerKeySnapshot

    def __post_init__(self) -> None:
        if not self.responses or not all(
            isinstance(response, EffectiveResponse) for response in self.responses
        ):
            raise ValueError("responses must be nonempty EffectiveResponse values")
        if not isinstance(self.key, AnswerKeySnapshot):
            raise TypeError("key must be AnswerKeySnapshot")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    work_item_id: str
    score: Decimal | None
    rank: int | None

    def __post_init__(self) -> None:
        _identifier(self.work_item_id, "work_item_id")
        if self.score is not None:
            _nonnegative_decimal(self.score, "score")
        if self.rank is not None:
            _revision(self.rank, "rank")
        if (self.score is None) != (self.rank is None):
            raise ValueError("score and rank must both be present or absent")


@dataclass(frozen=True, slots=True)
class ScoreStatistics:
    participant_count: int
    average_score: Decimal | None
    highest_score: Decimal | None
    lowest_score: Decimal | None

    def __post_init__(self) -> None:
        _count(self.participant_count, "participant_count")
        scores = (self.average_score, self.highest_score, self.lowest_score)
        if self.participant_count == 0:
            if any(score is not None for score in scores):
                raise ValueError("empty score statistics must not contain scores")
            return
        if self.average_score is None or self.highest_score is None or self.lowest_score is None:
            raise ValueError("nonempty score statistics must contain all scores")
        _nonnegative_decimal(self.average_score, "average_score")
        _nonnegative_decimal(self.highest_score, "highest_score")
        _nonnegative_decimal(self.lowest_score, "lowest_score")
        if not self.lowest_score <= self.average_score <= self.highest_score:
            raise ValueError("score statistics are inconsistent")


@dataclass(frozen=True, slots=True)
class ScoreSet:
    maximum_score: Decimal
    rows: tuple[ScoreResult, ...]
    statistics: ScoreStatistics

    def __post_init__(self) -> None:
        _nonnegative_decimal(self.maximum_score, "maximum_score")
        if not all(isinstance(row, ScoreResult) for row in self.rows):
            raise TypeError("rows must be ScoreResult values")
        if not isinstance(self.statistics, ScoreStatistics):
            raise TypeError("statistics must be ScoreStatistics")
        if len({row.work_item_id for row in self.rows}) != len(self.rows):
            raise ValueError("rows must have unique work_item_id values")

        scored_rows = tuple(row for row in self.rows if row.score is not None)
        if any(cast(Decimal, row.score) > self.maximum_score for row in scored_rows):
            raise ValueError("score cannot exceed maximum_score")
        if self.statistics.participant_count != len(scored_rows):
            raise ValueError("participant_count must match scored rows")
        scores = tuple(cast(Decimal, row.score) for row in scored_rows)
        if scores:
            if (
                self.statistics.average_score != sum(scores) / len(scores)
                or self.statistics.highest_score != max(scores)
                or self.statistics.lowest_score != min(scores)
            ):
                raise ValueError("statistics must match scored rows")
        expected_ranks: dict[Decimal, int] = {}
        for score in sorted(set(scores), reverse=True):
            expected_ranks[score] = 1 + sum(item > score for item in scores)
        if any(row.rank != expected_ranks[cast(Decimal, row.score)] for row in scored_rows):
            raise ValueError("ranks must match descending competition order")


@dataclass(frozen=True, slots=True)
class SessionMutationRequest:
    session_id: str
    expected_revision: int
    operation_id: str

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        _revision(self.expected_revision)
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class SoftDeleteResult:
    committed: bool
    location: str
    index_state: IndexState
    operation_id: str

    def __post_init__(self) -> None:
        _committed(self.committed)
        _text(self.location, "location")
        if not isinstance(self.index_state, IndexState):
            raise TypeError("index_state must be IndexState")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class TrashRestoreResult:
    committed: bool
    location: str
    index_state: IndexState
    operation_id: str

    def __post_init__(self) -> None:
        SoftDeleteResult(self.committed, self.location, self.index_state, self.operation_id)


@dataclass(frozen=True, slots=True)
class PermanentDeleteResult:
    committed: bool
    index_state: IndexState
    cleanup_state: CleanupState
    operation_id: str

    def __post_init__(self) -> None:
        _committed(self.committed)
        if not isinstance(self.index_state, IndexState) or not isinstance(
            self.cleanup_state, CleanupState
        ):
            raise TypeError("state fields must be closed enums")
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class Settings:
    default_profile: str
    default_sensitivity: int
    use_multiprocessing: bool

    def __post_init__(self) -> None:
        _default_profile(self.default_profile)
        if type(self.default_sensitivity) is not int or not 1 <= self.default_sensitivity <= 10:
            raise ValueError("default_sensitivity must be an integer from 1 through 10")
        if type(self.use_multiprocessing) is not bool:
            raise TypeError("use_multiprocessing must be bool")


@dataclass(frozen=True, slots=True)
class SettingsSaveCommand:
    settings: Settings
    expected_revision: int
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.settings, Settings):
            raise TypeError("settings must be Settings")
        _revision(self.expected_revision)
        _operation(self.operation_id)


@dataclass(frozen=True, slots=True)
class SettingsSaveResult:
    committed: bool
    revision: int
    operation_id: str

    def __post_init__(self) -> None:
        _committed(self.committed)
        _revision(self.revision, "revision")
        _operation(self.operation_id)


__all__ = [
    "AnswerKeyRequest",
    "AnswerKeyValidation",
    "BackupExportRequest",
    "BackupExportResult",
    "BackupRestoreResult",
    "BackupValidateRequest",
    "CancelOperationCommand",
    "CollisionPolicy",
    "CombinedReportRequest",
    "CombinedReportResult",
    "CommitGenerationResult",
    "CorrectionBatch",
    "CorrectionSemanticView",
    "EffectiveResponseProjection",
    "FinalizeCommand",
    "GenerationMutation",
    "GenerationSemanticInputs",
    "GradingSemanticView",
    "ImportResponseCommand",
    "ImportSemanticView",
    "MetadataEditCommand",
    "MetadataSemanticView",
    "PermanentDeleteResult",
    "ProfileImportRequest",
    "ProfileImportResult",
    "RecognitionSemanticView",
    "RecoveryIssue",
    "RecoveryReport",
    "RecoveryRequest",
    "RegradeCommand",
    "ResponseBookRequest",
    "ResponseBookValidation",
    "RestoreCommand",
    "ScanCommand",
    "ScanProgress",
    "ScanSource",
    "ScoreInput",
    "ScoreResult",
    "ScoreStatistics",
    "SessionCreateResult",
    "SessionMutationRequest",
    "Settings",
    "SettingsSaveCommand",
    "SettingsSaveResult",
    "SnapshotRef",
    "SnapshotRequest",
    "SoftDeleteResult",
    "TrashRestoreResult",
]
