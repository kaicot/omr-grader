"""Application DTO boundary contracts."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from typing import get_type_hints

import pytest

import omr_grader.application.dto as dto
from omr_grader.application.validation_token import ResponseValidationToken
from omr_grader.domain.enums import (
    CleanupState,
    ExamTerm,
    FieldStatus,
    IndexState,
    OperationKind,
    SessionState,
    TargetKind,
)
from omr_grader.domain.models import (
    AnswerValue,
    CorrectionDraft,
    IdCorrectionValue,
    SessionRecord,
)


def _dto_types() -> tuple[type[object], ...]:
    return tuple(
        value
        for name in dto.__all__
        if isinstance(value := getattr(dto, name), type) and is_dataclass(value)
    )


def _valid_correction() -> CorrectionDraft:
    return CorrectionDraft(
        "work-item",
        TargetKind.ID_CELL,
        0,
        IdCorrectionValue("1", FieldStatus.NORMAL),
        IdCorrectionValue("2", FieldStatus.NORMAL),
        "manual correction",
    )


def _valid_creation_commands() -> tuple[object, ...]:
    token = object.__new__(ResponseValidationToken)
    return (
        dto.ScanCommand(
            "session",
            "operation",
            0,
            "exam",
            2026,
            ExamTerm.FIRST,
            "profile",
            None,
            dto.ScanSource(("page.png",)),
            1,
            False,
        ),
        dto.ImportResponseCommand(token, "session", "operation", 0),
    )


def _valid_mutation_commands() -> tuple[object, ...]:
    session = object.__new__(SessionRecord)
    object.__setattr__(session, "state", SessionState.CREATED)
    metadata = dto.MetadataSemanticView(session)
    return (
        dto.CorrectionBatch("session", 1, "key", "operation", (_valid_correction(),)),
        dto.RegradeCommand("session", 1, "key.xlsx", "Sheet1", "operation"),
        dto.FinalizeCommand("session", 1, "operation"),
        dto.GenerationMutation(
            "session",
            "operation",
            OperationKind.METADATA_EDIT,
            1,
            SessionState.CREATED,
            metadata,
            None,
        ),
        dto.MetadataEditCommand("session", 1, "operation", "exam", 2026, ExamTerm.FIRST),
        dto.SessionMutationRequest("session", 1, "operation"),
        dto.SettingsSaveCommand(dto.Settings("profile.omrtemplate", 1, False), 1, "operation"),
    )


def test_all_dto_annotations_resolve() -> None:
    for dto_type in _dto_types():
        get_type_hints(dto_type)


def test_dtos_do_not_expose_warning_or_error_channels() -> None:
    forbidden_names = {"warning", "warnings", "error", "errors"}

    for dto_type in _dto_types():
        assert forbidden_names.isdisjoint(field.name for field in fields(dto_type))


def test_import_response_command_requires_concrete_validation_token() -> None:
    annotations = get_type_hints(dto.ImportResponseCommand)

    assert annotations["validation_token"] is ResponseValidationToken
    assert "source_identity" not in annotations
    assert "source_sha256" not in annotations


@pytest.mark.parametrize("invalid_revision", (-1, 1, True, 0.0))
def test_creation_commands_require_zero_revision(invalid_revision: object) -> None:
    for command in _valid_creation_commands():
        with pytest.raises(ValueError):
            replace(command, expected_revision=invalid_revision)


@pytest.mark.parametrize("invalid_revision", (0, -1, True, 1.0))
def test_mutation_commands_require_positive_revisions(invalid_revision: object) -> None:
    for command in _valid_mutation_commands():
        with pytest.raises(ValueError):
            replace(command, expected_revision=invalid_revision)


@pytest.mark.parametrize("invalid_operation", ("", " ", 1))
def test_every_revisioned_command_rejects_invalid_operation_ids(invalid_operation: object) -> None:
    for command in _valid_creation_commands() + _valid_mutation_commands():
        with pytest.raises(ValueError):
            replace(command, operation_id=invalid_operation)


@pytest.mark.parametrize(
    ("request_factory", "expected_policy"),
    (
        (
            lambda policy: dto.CombinedReportRequest(
                ("session",), True, "report.xlsx", policy, "operation"
            ),
            dto.CollisionPolicy.ERROR,
        ),
        (
            lambda policy: dto.BackupExportRequest(
                dto.SnapshotRequest("session", None, dto.SnapshotPurpose.BACKUP),
                "backup.zip",
                policy,
                "operation",
            ),
            dto.CollisionPolicy.REPLACE,
        ),
    ),
)
def test_report_and_backup_requests_allow_only_nonrenaming_collisions(
    request_factory: object, expected_policy: dto.CollisionPolicy
) -> None:
    assert callable(request_factory)
    assert request_factory(expected_policy).collision is expected_policy
    with pytest.raises(ValueError):
        request_factory(dto.CollisionPolicy.RENAME)
    with pytest.raises(TypeError):
        request_factory("replace")


def test_profile_import_accepts_rename_collision() -> None:
    request = dto.ProfileImportRequest(
        "profile.json", dto.CollisionPolicy.RENAME, "profile", "capability"
    )

    assert request.collision is dto.CollisionPolicy.RENAME


@pytest.mark.parametrize(
    "case",
    (
        dto.ScanCommand(
            "session",
            "operation",
            0,
            "exam",
            2026,
            ExamTerm.FIRST,
            "profile",
            None,
            dto.ScanSource(("page.png",)),
            1,
            False,
        ),
        dto.ResponseBookRequest("responses.xlsx", "Sheet1", "exam", 2026, ExamTerm.FIRST),
        dto.MetadataEditCommand("session", 1, "operation", "exam", 2026, ExamTerm.FIRST),
    ),
)
@pytest.mark.parametrize("invalid_year", (1999, 2101, True, 2026.0))
def test_exam_requests_reject_invalid_years(case: object, invalid_year: object) -> None:
    with pytest.raises(ValueError):
        replace(case, exam_year=invalid_year)


@pytest.mark.parametrize("sensitivity", (0, 1, 10, 11))
def test_sensitivity_contracts_enforce_one_through_ten(sensitivity: int) -> None:
    scan = _valid_creation_commands()[0]
    assert isinstance(scan, dto.ScanCommand)

    if 1 <= sensitivity <= 10:
        assert replace(scan, sensitivity=sensitivity).sensitivity == sensitivity
        assert (
            dto.Settings("profile.omrtemplate", sensitivity, False).default_sensitivity
            == sensitivity
        )
    else:
        with pytest.raises(ValueError):
            replace(scan, sensitivity=sensitivity)
        with pytest.raises(ValueError):
            dto.Settings("profile.omrtemplate", sensitivity, False)


@pytest.mark.parametrize("invalid_sensitivity", (True, 1.0))
def test_sensitivity_contracts_reject_non_integer_values(invalid_sensitivity: object) -> None:
    scan = _valid_creation_commands()[0]
    assert isinstance(scan, dto.ScanCommand)
    with pytest.raises(ValueError):
        replace(scan, sensitivity=invalid_sensitivity)
    with pytest.raises(ValueError):
        dto.Settings("profile.omrtemplate", invalid_sensitivity, False)


@pytest.mark.parametrize(
    "result_factory",
    (
        lambda: dto.SessionCreateResult(
            True, "session", 1, "generation", IndexState.CURRENT, "operation"
        ),
        lambda: dto.CommitGenerationResult(
            True, "session", 1, "generation", IndexState.CURRENT, "operation"
        ),
        lambda: dto.BackupRestoreResult(
            True, "session", 1, "generation", IndexState.CURRENT, "operation"
        ),
        lambda: dto.SoftDeleteResult(True, "trash/session", IndexState.CURRENT, "operation"),
        lambda: dto.TrashRestoreResult(True, "session", IndexState.CURRENT, "operation"),
        lambda: dto.PermanentDeleteResult(
            True, IndexState.CURRENT, CleanupState.COMPLETE, "operation"
        ),
        lambda: dto.SettingsSaveResult(True, 1, "operation"),
    ),
)
def test_success_results_reject_uncommitted_state(result_factory: object) -> None:
    result = result_factory()
    with pytest.raises(ValueError):
        replace(result, committed=False)


@pytest.mark.parametrize(
    "default_profile",
    ("", "profile.omrtemplate", "Profile.OMRTEMPLATE"),
)
def test_settings_accepts_empty_or_safe_template_default_profile(default_profile: str) -> None:
    settings = dto.Settings(default_profile, 1, False)

    assert settings.default_profile == default_profile


@pytest.mark.parametrize(
    "default_profile",
    (
        "profile.txt",
        ".omrtemplate",
        "../profile.omrtemplate",
        "folder/profile.omrtemplate",
        r"C:\profile.omrtemplate",
        "NUL.omrtemplate",
        "CONIN$.omrtemplate",
        "CONOUT$.omrtemplate",
        "COM¹.omrtemplate",
        "LPT².omrtemplate",
        "profile.omrtemplate ",
    ),
)
def test_settings_rejects_unsafe_or_wrong_extension_default_profile(default_profile: str) -> None:
    with pytest.raises(ValueError):
        dto.Settings(default_profile, 1, False)


def _generation_view(
    operation_kind: OperationKind,
    state: SessionState,
    scores: dto.ScoreSet | None = None,
) -> dto.GenerationSemanticInputs:
    if operation_kind is OperationKind.RECOGNIZE:
        view = object.__new__(dto.RecognitionSemanticView)
        object.__setattr__(view, "state", state)
        return view
    if operation_kind is OperationKind.IMPORT_RESPONSES:
        view = object.__new__(dto.ImportSemanticView)
        object.__setattr__(view, "state", state)
        return view
    if operation_kind is OperationKind.CORRECT:
        view = object.__new__(dto.CorrectionSemanticView)
        object.__setattr__(view, "state", state)
        return view
    if operation_kind in (OperationKind.REGRADE, OperationKind.FINALIZE):
        view = object.__new__(dto.GradingSemanticView)
        object.__setattr__(view, "state", state)
        object.__setattr__(view, "scores", scores)
        return view
    if operation_kind is OperationKind.METADATA_EDIT:
        session = object.__new__(SessionRecord)
        object.__setattr__(session, "state", state)
        view = object.__new__(dto.MetadataSemanticView)
        object.__setattr__(view, "session", session)
        return view
    raise AssertionError("test helper only accepts generation operations")


def _allows_generation_transition(
    operation_kind: OperationKind,
    source_state: SessionState,
    target_state: SessionState,
) -> bool:
    if operation_kind is OperationKind.RECOGNIZE:
        return source_state is SessionState.CREATED and target_state is SessionState.RECOGNIZED
    if operation_kind is OperationKind.CORRECT:
        return (
            source_state in (SessionState.GRADED, SessionState.FINALIZED)
            and target_state is SessionState.GRADED
        )
    if operation_kind is OperationKind.REGRADE:
        return (
            source_state in (SessionState.RECOGNIZED, SessionState.GRADED, SessionState.FINALIZED)
            and target_state is SessionState.GRADED
        )
    if operation_kind is OperationKind.FINALIZE:
        return source_state is SessionState.GRADED and target_state is SessionState.FINALIZED
    if operation_kind is OperationKind.METADATA_EDIT:
        return source_state is target_state
    return False


@pytest.mark.parametrize(
    "operation_kind",
    (
        OperationKind.RECOGNIZE,
        OperationKind.IMPORT_RESPONSES,
        OperationKind.CORRECT,
        OperationKind.REGRADE,
        OperationKind.FINALIZE,
        OperationKind.METADATA_EDIT,
    ),
)
@pytest.mark.parametrize("source_state", tuple(SessionState))
@pytest.mark.parametrize("target_state", tuple(SessionState))
def test_generation_mutation_enforces_every_state_transition_and_target_state(
    operation_kind: OperationKind,
    source_state: SessionState,
    target_state: SessionState,
) -> None:
    scores = _valid_score_set() if operation_kind is OperationKind.FINALIZE else None
    semantic_inputs = _generation_view(operation_kind, source_state, scores)
    allowed = _allows_generation_transition(operation_kind, source_state, target_state)

    if allowed:
        mutation = dto.GenerationMutation(
            "session",
            "operation",
            operation_kind,
            1,
            target_state,
            semantic_inputs,
            None,
        )
        assert mutation.target_state is target_state
    else:
        with pytest.raises(ValueError):
            dto.GenerationMutation(
                "session",
                "operation",
                operation_kind,
                1,
                target_state,
                semantic_inputs,
                None,
            )


def test_generation_mutation_requires_validated_scores_to_finalize() -> None:
    semantic_inputs = _generation_view(OperationKind.FINALIZE, SessionState.GRADED)

    with pytest.raises(ValueError):
        dto.GenerationMutation(
            "session",
            "operation",
            OperationKind.FINALIZE,
            1,
            SessionState.FINALIZED,
            semantic_inputs,
            None,
        )


def test_grading_semantic_view_exposes_typed_score_proof() -> None:
    answer_key = object.__new__(dto.AnswerKeySnapshot)
    annotations = get_type_hints(dto.GradingSemanticView)

    assert annotations["scores"] == dto.ScoreSet | None
    with pytest.raises(TypeError):
        dto.GradingSemanticView(answer_key, SessionState.GRADED, object())  # type: ignore[arg-type]


def test_generation_mutation_uses_typed_semantic_projection_and_target_payloads() -> None:
    annotations = get_type_hints(dto.GenerationMutation)
    session = object.__new__(SessionRecord)
    object.__setattr__(session, "state", SessionState.CREATED)
    metadata = dto.MetadataSemanticView(session)

    assert annotations["semantic_inputs"] is dto.GenerationSemanticInputs
    assert annotations["projection_request"] == dto.EffectiveResponseProjection | None
    assert annotations["target_state"] is SessionState
    with pytest.raises(TypeError):
        dto.GenerationMutation(
            "session", "operation", OperationKind.METADATA_EDIT, 1, "created", metadata, None
        )
    with pytest.raises(TypeError):
        dto.GenerationMutation(
            "session", "operation", OperationKind.METADATA_EDIT, 1, SessionState.CREATED, {}, None
        )
    with pytest.raises(TypeError):
        dto.GenerationMutation(
            "session",
            "operation",
            OperationKind.METADATA_EDIT,
            1,
            SessionState.CREATED,
            metadata,
            {},
        )


def test_generation_mutation_rejects_create_operation() -> None:
    semantic_inputs = _generation_view(OperationKind.RECOGNIZE, SessionState.CREATED)

    with pytest.raises(ValueError):
        dto.GenerationMutation(
            "session",
            "operation",
            OperationKind.CREATE,
            1,
            SessionState.RECOGNIZED,
            semantic_inputs,
            None,
        )


def test_correction_batches_use_id_values_with_id_targets() -> None:
    annotations = get_type_hints(CorrectionDraft)
    correction = _valid_correction()

    assert annotations["target_kind"] is TargetKind
    assert annotations["before"] == IdCorrectionValue | AnswerValue
    assert annotations["after"] == IdCorrectionValue | AnswerValue
    batch = dto.CorrectionBatch("session", 1, "key", "operation", (correction,))
    assert batch.edits == (correction,)

    with pytest.raises(ValueError):
        replace(correction, target_kind=TargetKind.ANSWER_CELL)


def _valid_score_set() -> dto.ScoreSet:
    rows = (
        dto.ScoreResult("highest", Decimal("10"), 1),
        dto.ScoreResult("tied-one", Decimal("7"), 2),
        dto.ScoreResult("tied-two", Decimal("7"), 2),
        dto.ScoreResult("lowest", Decimal("2"), 4),
        dto.ScoreResult("unscored", None, None),
    )
    return dto.ScoreSet(
        Decimal("10"),
        rows,
        dto.ScoreStatistics(4, Decimal("6.5"), Decimal("10"), Decimal("2")),
    )


@pytest.mark.parametrize("invalid", (Decimal("-1"), Decimal("NaN"), Decimal("Infinity")))
def test_score_values_and_maximum_must_be_finite_and_nonnegative(invalid: Decimal) -> None:
    with pytest.raises(ValueError):
        dto.ScoreResult("work-item", invalid, 1)
    with pytest.raises(ValueError):
        dto.ScoreSet(invalid, (), dto.ScoreStatistics(0, None, None, None))


@pytest.mark.parametrize("field", ("average_score", "highest_score", "lowest_score"))
@pytest.mark.parametrize("invalid", (Decimal("-1"), Decimal("NaN"), Decimal("Infinity")))
def test_score_statistics_values_must_be_finite_and_nonnegative(
    field: str, invalid: Decimal
) -> None:
    values: dict[str, Decimal] = {
        "average_score": Decimal("1"),
        "highest_score": Decimal("1"),
        "lowest_score": Decimal("1"),
    }
    values[field] = invalid

    with pytest.raises(ValueError):
        dto.ScoreStatistics(1, **values)


def test_score_set_enforces_maximum_unique_items_participants_ranks_and_statistics() -> None:
    score_set = _valid_score_set()
    assert tuple(row.rank for row in score_set.rows) == (1, 2, 2, 4, None)

    with pytest.raises(ValueError):
        replace(score_set, rows=score_set.rows[:1] + (score_set.rows[0],))
    with pytest.raises(ValueError):
        replace(
            score_set,
            rows=(dto.ScoreResult("over-maximum", Decimal("11"), 1),),
            statistics=dto.ScoreStatistics(1, Decimal("11"), Decimal("11"), Decimal("11")),
        )
    with pytest.raises(ValueError):
        replace(
            score_set,
            statistics=dto.ScoreStatistics(3, Decimal("6.5"), Decimal("10"), Decimal("2")),
        )
    with pytest.raises(ValueError):
        replace(
            score_set,
            rows=(
                dto.ScoreResult("highest", Decimal("10"), 1),
                dto.ScoreResult("tied-one", Decimal("7"), 3),
                dto.ScoreResult("tied-two", Decimal("7"), 3),
                dto.ScoreResult("lowest", Decimal("2"), 4),
                dto.ScoreResult("unscored", None, None),
            ),
        )
    with pytest.raises(ValueError):
        replace(
            score_set,
            statistics=dto.ScoreStatistics(4, Decimal("6"), Decimal("10"), Decimal("2")),
        )
