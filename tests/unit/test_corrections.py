from __future__ import annotations

from omr_grader.domain import corrections
from omr_grader.domain.corrections import apply_correction_batch, validate_correction_batch
from omr_grader.domain.enums import (
    AnswerStatus,
    FieldStatus,
    SourceKind,
    StudentIdStatus,
    TargetKind,
)
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import (
    AnswerValue,
    CorrectionDraft,
    CorrectionEvent,
    EffectiveResponse,
    IdCorrectionValue,
)

BLANK = AnswerValue((), AnswerStatus.BLANK)
NORMAL = AnswerValue((1,), AnswerStatus.NORMAL)


def _response(work_item_id: str, student_id: str | None = "12345678") -> EffectiveResponse:
    return EffectiveResponse(
        work_item_id,
        SourceKind.IMAGE,
        work_item_id,
        student_id,
        StudentIdStatus.NORMAL if student_id is not None else StudentIdStatus.INVALID,
        (BLANK,) * 100,
    )


def _answer_edit(work_item_id: str, before: AnswerValue = BLANK) -> CorrectionDraft:
    return CorrectionDraft(work_item_id, TargetKind.ANSWER_CELL, 1, before, NORMAL, "review")


def _event(
    event_id: str,
    *,
    idempotency_key: str = "key",
    session_id: str = "session",
    expected_base_revision: int = 1,
    committed_revision: int = 2,
    before: AnswerValue = BLANK,
    after: AnswerValue = NORMAL,
) -> CorrectionEvent:
    return CorrectionEvent(
        1,
        event_id,
        session_id,
        "wi_one",
        TargetKind.ANSWER_CELL,
        1,
        expected_base_revision,
        before,
        after,
        "review",
        "actor",
        "2026-01-01T00:00:00.000000Z",
        committed_revision,
        idempotency_key,
    )


_CONTEXT = {"session_id": "session", "expected_base_revision": 1}


def test_corrections_are_ordered_by_effective_before_values_and_do_not_mutate_source() -> None:
    source = _response("wi_one")
    first = _answer_edit("wi_one")
    second = CorrectionDraft("wi_one", TargetKind.ANSWER_CELL, 1, NORMAL, BLANK, "review")
    result = apply_correction_batch((source,), (first, second), **_CONTEXT)
    assert isinstance(result, Ok)
    assert result.value[0].answers[0] == BLANK
    assert result.value[0].corrected_targets == ("answer_cell:1",)
    assert source.answers[0] == BLANK


def test_noop_and_stale_corrections_are_rejected() -> None:
    source = _response("wi_one")
    noop = CorrectionDraft("wi_one", TargetKind.ANSWER_CELL, 1, BLANK, BLANK, "review")
    stale = _answer_edit("wi_one", NORMAL)
    noop_result = apply_correction_batch((source,), (noop,), **_CONTEXT)
    stale_result = apply_correction_batch((source,), (stale,), **_CONTEXT)
    assert isinstance(noop_result, Err) and noop_result.errors[0].code == "NOOP_CORRECTION"
    assert isinstance(stale_result, Err) and stale_result.errors[0].code == "STALE_CORRECTION"


def test_new_duplicate_effective_id_is_rejected_but_resolution_is_allowed() -> None:
    first = _response("wi_one", "12345678")
    second = _response("wi_two", "12345679")
    increase = CorrectionDraft(
        "wi_two",
        TargetKind.ID_CELL,
        7,
        IdCorrectionValue("9", FieldStatus.NORMAL),
        IdCorrectionValue("8", FieldStatus.NORMAL),
        "review",
    )
    rejected = validate_correction_batch((first, second), (increase,), **_CONTEXT)
    assert isinstance(rejected, Err)
    assert rejected.errors[0].code == "DUPLICATE_EFFECTIVE_ID"

    duplicate = _response("wi_two", "12345678")
    resolve = CorrectionDraft(
        "wi_two",
        TargetKind.ID_CELL,
        7,
        IdCorrectionValue("8", FieldStatus.NORMAL),
        IdCorrectionValue("9", FieldStatus.NORMAL),
        "review",
    )
    accepted = apply_correction_batch((first, duplicate), (resolve,), **_CONTEXT)
    assert isinstance(accepted, Ok)
    assert accepted.value[1].student_id == "12345679"


def test_duplicate_correction_event_identity_is_rejected() -> None:
    event = _event("event-1")
    result = validate_correction_batch((_response("wi_one"),), (event, event), **_CONTEXT)

    assert isinstance(result, Err)
    assert result.errors[0].code == "DUPLICATE_CORRECTION_EVENT"


def test_existing_idempotency_key_conflict_is_rejected() -> None:
    existing = _event("event-1", idempotency_key="same")
    new = _event("event-2", idempotency_key="same", after=BLANK)

    result = validate_correction_batch(
        (_response("wi_one"),), (new,), existing_events=(existing,), **_CONTEXT
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "IDEMPOTENCY_CONFLICT"


def test_duplicate_work_item_inputs_and_malformed_inputs_return_errors() -> None:
    source = _response("wi_one")

    duplicate = apply_correction_batch((source, source), (), **_CONTEXT)
    malformed = apply_correction_batch(("not a response",), (), **_CONTEXT)  # type: ignore[arg-type]
    mixed = apply_correction_batch(
        (source,), (_answer_edit("wi_one"), _event("event-1")), **_CONTEXT
    )

    assert isinstance(duplicate, Err)
    assert duplicate.errors[0].code == "DUPLICATE_WORK_ITEM"
    assert isinstance(malformed, Err)
    assert malformed.errors[0].code == "INVALID_EFFECTIVE_RESPONSE"
    assert isinstance(mixed, Err)
    assert mixed.errors[0].code == "MIXED_CORRECTION_TYPES"


def test_id_cell_correction_uses_exact_partial_source_cells() -> None:
    response = _response("wi_one", None)
    partial_cells = (
        IdCorrectionValue("1", FieldStatus.NORMAL),
        IdCorrectionValue(None, FieldStatus.BLANK),
        IdCorrectionValue("3", FieldStatus.NORMAL),
        IdCorrectionValue("4", FieldStatus.NORMAL),
        IdCorrectionValue("5", FieldStatus.NORMAL),
        IdCorrectionValue("6", FieldStatus.NORMAL),
        IdCorrectionValue("7", FieldStatus.NORMAL),
        IdCorrectionValue("8", FieldStatus.NORMAL),
    )
    state = corrections._ResponseState(response, partial_cells, StudentIdStatus.INVALID)
    correction = CorrectionDraft(
        "wi_one",
        TargetKind.ID_CELL,
        1,
        IdCorrectionValue(None, FieldStatus.BLANK),
        IdCorrectionValue("2", FieldStatus.NORMAL),
        "review",
    )

    result = corrections._apply_states((state,), (correction,))

    assert isinstance(result, Ok)
    assert result.value[0].response.student_id == "12345678"
    assert result.value[0].id_cells == (
        IdCorrectionValue("1", FieldStatus.NORMAL),
        IdCorrectionValue("2", FieldStatus.NORMAL),
        *partial_cells[2:],
    )


def test_committed_events_require_authoritative_session_and_revision_chain() -> None:
    source = _response("wi_one")

    cross_session = validate_correction_batch(
        (source,), (_event("event-1", session_id="other"),), **_CONTEXT
    )
    stale_base = validate_correction_batch(
        (source,), (_event("event-1", expected_base_revision=2, committed_revision=3),), **_CONTEXT
    )
    invalid_commit = validate_correction_batch(
        (source,), (_event("event-1", committed_revision=3),), **_CONTEXT
    )
    replay = validate_correction_batch(
        (source,),
        (
            _event("event-1"),
            _event("event-2", expected_base_revision=2, committed_revision=2),
        ),
        **_CONTEXT,
    )

    assert isinstance(cross_session, Err)
    assert cross_session.errors[0].code == "CORRECTION_SESSION_MISMATCH"
    assert isinstance(stale_base, Err)
    assert stale_base.errors[0].code == "STALE_CORRECTION_BASE_REVISION"
    assert isinstance(invalid_commit, Err)
    assert invalid_commit.errors[0].code == "INVALID_COMMITTED_REVISION"
    assert isinstance(replay, Err)
    assert replay.errors[0].code == "STALE_CORRECTION_BASE_REVISION"


def test_valid_ordered_committed_events_are_applied() -> None:
    source = _response("wi_one")
    first = _event("event-1")
    second = CorrectionEvent(
        1,
        "event-2",
        "session",
        "wi_one",
        TargetKind.ANSWER_CELL,
        1,
        2,
        NORMAL,
        BLANK,
        "review",
        "actor",
        "2026-01-01T00:00:00.000000Z",
        3,
        "key-2",
    )

    result = apply_correction_batch((source,), (second, first), **_CONTEXT)

    assert isinstance(result, Ok)
    assert result.value[0].answers[0] == BLANK


def test_committed_event_authority_precedes_duplicate_and_mixed_ordering() -> None:
    source = _response("wi_one")
    cross_session_duplicate = validate_correction_batch(
        (source,),
        (
            _event("event-1", session_id="other"),
            _event("event-1", session_id="other"),
        ),
        **_CONTEXT,
    )
    stale_mixed = validate_correction_batch(
        (source,),
        (
            _answer_edit("wi_one"),
            _event(
                "event-1",
                expected_base_revision=1,
                committed_revision=2,
            ),
        ),
        session_id="session",
        expected_base_revision=2,
    )

    assert isinstance(cross_session_duplicate, Err)
    assert cross_session_duplicate.errors[0].code == "CORRECTION_SESSION_MISMATCH"
    assert isinstance(stale_mixed, Err)
    assert stale_mixed.errors[0].code == "STALE_CORRECTION_BASE_REVISION"


def test_same_generation_events_are_deterministically_ordered_and_applied() -> None:
    source = _response("wi_one")
    first = _event("event-a")
    second = _event(
        "event-b",
        idempotency_key="key-b",
        before=NORMAL,
        after=BLANK,
    )

    result = apply_correction_batch((source,), (second, first), **_CONTEXT)

    assert isinstance(result, Ok)
    assert result.value[0].answers[0] == BLANK


def test_committed_event_revision_groups_reject_gaps_replays_and_conflicts() -> None:
    source = _response("wi_one")
    gap = validate_correction_batch(
        (source,), (_event("event-1", committed_revision=3),), **_CONTEXT
    )
    replay = validate_correction_batch(
        (source,),
        (_event("event-1", expected_base_revision=1, committed_revision=2),),
        session_id="session",
        expected_base_revision=2,
    )
    conflict = validate_correction_batch(
        (source,),
        (
            _event("event-a"),
            _event(
                "event-b",
                idempotency_key="key-b",
                expected_base_revision=2,
                committed_revision=2,
            ),
        ),
        **_CONTEXT,
    )

    assert isinstance(gap, Err)
    assert gap.errors[0].code == "INVALID_COMMITTED_REVISION"
    assert isinstance(replay, Err)
    assert replay.errors[0].code == "STALE_CORRECTION_BASE_REVISION"
    assert isinstance(conflict, Err)
    assert conflict.errors[0].code == "STALE_CORRECTION_BASE_REVISION"
