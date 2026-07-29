"""Pure correction validation and effective-response projection."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .enums import FieldStatus, SourceKind, StudentIdStatus, TargetKind
from .errors import Err, ErrorInfo, Ok, Result
from .models import (
    AnswerValue,
    AutomaticPage,
    CorrectionDraft,
    CorrectionEvent,
    EffectiveResponse,
    IdCorrectionValue,
    ImportedResponseRef,
)

if TYPE_CHECKING:
    from omr_grader.application.dto import EffectiveResponseProjection

type Correction = CorrectionDraft | CorrectionEvent
_DIGITS = re.compile(r"^[0-9]{8}$")


@dataclass(frozen=True, slots=True)
class _ResponseState:
    """Internal projection retaining ID-cell detail absent from EffectiveResponse."""

    response: EffectiveResponse
    id_cells: tuple[IdCorrectionValue, ...]
    original_id_status: StudentIdStatus


def _error(code: str, reason: str, field_path: str | None = None) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path, {"reason": reason}),))


def _target_name(kind: TargetKind, key: int) -> str:
    return f"{kind.value}:{key}"


def _event_order(item: CorrectionEvent) -> tuple[int, str]:
    return item.committed_revision, item.event_id


def _ordered(corrections: Iterable[Correction]) -> Result[tuple[Correction, ...]]:
    values = tuple(corrections)
    events: list[CorrectionEvent] = []
    drafts: list[CorrectionDraft] = []
    for item in values:
        if isinstance(item, CorrectionEvent):
            events.append(item)
        elif isinstance(item, CorrectionDraft):
            drafts.append(item)
        else:
            return _error(
                "INVALID_CORRECTION",
                "corrections must be correction drafts or events",
                "corrections",
            )
    if events and drafts:
        return _error(
            "MIXED_CORRECTION_TYPES",
            "drafts and committed events cannot be ordered together",
            "corrections",
        )
    if events:
        if len({(item.committed_revision, item.event_id) for item in events}) != len(events):
            return _error(
                "DUPLICATE_CORRECTION_EVENT",
                "committed correction identity is duplicated",
                "corrections",
            )
        ordered_events: tuple[CorrectionEvent, ...] = tuple(sorted(events, key=_event_order))
        return Ok(ordered_events)
    return Ok(tuple(drafts))


def validate_idempotency(
    corrections: Iterable[CorrectionEvent], existing: Iterable[CorrectionEvent] = ()
) -> Result[None]:
    """Reject reuse of an idempotency key for a different correction payload."""
    values = tuple(corrections) + tuple(existing)
    if not all(isinstance(item, CorrectionEvent) for item in values):
        return _error(
            "INVALID_CORRECTION_EVENT", "idempotency validation requires committed events"
        )
    if len({(item.committed_revision, item.event_id) for item in values}) != len(values):
        return _error(
            "DUPLICATE_CORRECTION_EVENT",
            "committed correction identity is duplicated",
            "corrections",
        )
    by_key: dict[str, tuple[object, ...]] = {}
    for event in values:
        semantic = (
            event.session_id,
            event.work_item_id,
            event.target_kind,
            event.target_key,
            event.expected_base_revision,
            event.before,
            event.after,
            event.reason,
            event.local_actor,
        )
        prior = by_key.setdefault(event.idempotency_key, semantic)
        if prior != semantic:
            return _error(
                "IDEMPOTENCY_CONFLICT",
                "idempotency key was used for a different correction",
                "idempotency_key",
            )
    return Ok(None)
def validate_correction_event_history(
    events: Iterable[CorrectionEvent],
    drafts: Iterable[CorrectionDraft],
    *,
    session_id: str,
    expected_new_base_revision: int | None = None,
) -> Result[None]:
    """Require immutable event authority to exactly represent its draft projection."""
    event_values = tuple(events)
    draft_values = tuple(drafts)
    if len(event_values) != len(draft_values):
        return _error(
            "CORRECTION_EVENT_PROJECTION_MISMATCH",
            "correction events and projected drafts have different lengths",
            "corrections",
        )
    if not all(isinstance(event, CorrectionEvent) for event in event_values):
        return _error(
            "INVALID_CORRECTION_EVENT",
            "correction event history must contain committed events",
            "corrections",
        )
    if len({event.event_id for event in event_values}) != len(event_values):
        return _error(
            "DUPLICATE_CORRECTION_EVENT",
            "committed correction event ID is duplicated",
            "corrections",
        )
    idempotency = validate_idempotency(event_values)
    if isinstance(idempotency, Err):
        return idempotency
    expected_drafts = tuple(
        CorrectionDraft(
            event.work_item_id,
            event.target_kind,
            event.target_key,
            event.before,
            event.after,
            event.reason,
        )
        for event in event_values
    )
    if expected_drafts != draft_values:
        return _error(
            "CORRECTION_EVENT_PROJECTION_MISMATCH",
            "correction events do not exactly match projected drafts",
            "corrections",
        )
    for event in event_values:
        if event.session_id != session_id:
            return _error(
                "CORRECTION_SESSION_MISMATCH",
                "committed correction session does not match the authoritative session",
                "session_id",
            )
        if event.committed_revision != event.expected_base_revision + 1:
            return _error(
                "INVALID_COMMITTED_REVISION",
                "committed correction revision must immediately succeed its base revision",
                "committed_revision",
            )
    if expected_new_base_revision is not None:
        if type(expected_new_base_revision) is not int or expected_new_base_revision < 1:
            return _error(
                "INVALID_CORRECTION_CONTEXT",
                "authoritative expected base revision must be a positive integer",
                "expected_base_revision",
            )
        new_start = next(
            (
                index
                for index, event in enumerate(event_values)
                if event.expected_base_revision == expected_new_base_revision
            ),
            None,
        )
        if new_start is None:
            return _error(
                "MISSING_CORRECTION_EVENT_SUFFIX",
                "target correction generation must add a committed event suffix",
                "corrections",
            )
        for event in event_values[:new_start]:
            if event.expected_base_revision >= expected_new_base_revision:
                return _error(
                    "INVALID_CORRECTION_EVENT_SUFFIX",
                    "historical correction events must precede the target generation",
                    "expected_base_revision",
                )
        for event in event_values[new_start:]:
            if event.expected_base_revision != expected_new_base_revision:
                return _error(
                    "INVALID_CORRECTION_EVENT_SUFFIX",
                    "target correction events must form one complete suffix group",
                    "expected_base_revision",
                )
    return Ok(None)



def validate_correction_batch(
    responses: Sequence[EffectiveResponse],
    corrections: Iterable[Correction],
    *,
    session_id: str,
    expected_base_revision: int,
    existing_events: Iterable[CorrectionEvent] = (),
) -> Result[None]:
    """Validate preconditions, no-ops, idempotency, and new duplicate-ID collisions."""
    correction_values = tuple(corrections)
    existing_values = tuple(existing_events)
    if not all(isinstance(response, EffectiveResponse) for response in responses):
        return _error(
            "INVALID_EFFECTIVE_RESPONSE", "responses must be EffectiveResponse values", "responses"
        )
    if not all(isinstance(event, CorrectionEvent) for event in existing_values):
        return _error(
            "INVALID_CORRECTION_EVENT",
            "existing_events must contain committed correction events",
            "existing_events",
        )
    states = tuple(_state_from_response(response) for response in responses)
    applied = _validated_apply(
        states,
        correction_values,
        existing_values,
        session_id=session_id,
        expected_base_revision=expected_base_revision,
    )
    if isinstance(applied, Err):
        return applied
    return Ok(None)


def apply_correction_batch(
    responses: Sequence[EffectiveResponse],
    corrections: Iterable[Correction],
    *,
    session_id: str,
    expected_base_revision: int,
    existing_events: Iterable[CorrectionEvent] = (),
) -> Result[tuple[EffectiveResponse, ...]]:
    """Validate and apply a correction batch without mutating any source response."""
    correction_values = tuple(corrections)
    existing_values = tuple(existing_events)
    if not all(isinstance(response, EffectiveResponse) for response in responses):
        return _error(
            "INVALID_EFFECTIVE_RESPONSE", "responses must be EffectiveResponse values", "responses"
        )
    if not all(isinstance(event, CorrectionEvent) for event in existing_values):
        return _error(
            "INVALID_CORRECTION_EVENT",
            "existing_events must contain committed correction events",
            "existing_events",
        )
    states = tuple(_state_from_response(response) for response in responses)
    applied = _validated_apply(
        states,
        correction_values,
        existing_values,
        session_id=session_id,
        expected_base_revision=expected_base_revision,
    )
    if isinstance(applied, Err):
        return applied
    return Ok(tuple(state.response for state in applied.value))


def project_effective_responses(
    projection: EffectiveResponseProjection,
    *,
    session_id: str,
    expected_base_revision: int,
) -> Result[tuple[EffectiveResponse, ...]]:
    """Project immutable automatic/imported snapshots plus ordered corrections."""
    try:
        pages = projection.automatic_pages
        imported = projection.imported_responses
        corrections = projection.corrections
    except AttributeError:
        return _error(
            "INVALID_PROJECTION",
            "projection must expose response snapshots and corrections",
            "projection",
        )
    if not all(isinstance(item, AutomaticPage) for item in pages) or not all(
        isinstance(item, ImportedResponseRef) for item in imported
    ):
        return _error(
            "INVALID_PROJECTION", "projection contains invalid source snapshots", "projection"
        )
    for page in pages:
        if len(page.answers) != 100:
            return _error(
                "UNPROJECTABLE_PAGE",
                "automatic page has no complete answer set",
                page.page_ref.work_item_id,
            )
    try:
        base = [_from_automatic(page) for page in pages]
        base.extend(_from_imported(response) for response in imported)
    except (AttributeError, TypeError, ValueError) as error:
        return _error("INVALID_PROJECTION", str(error), "projection")
    if len({item.response.work_item_id for item in base}) != len(base):
        return _error(
            "DUPLICATE_WORK_ITEM", "base responses contain duplicate work-item IDs", "projection"
        )
    applied = _validated_apply(
        tuple(base),
        corrections,
        (),
        session_id=session_id,
        expected_base_revision=expected_base_revision,
    )
    if isinstance(applied, Err):
        return applied
    return Ok(tuple(state.response for state in applied.value))


def _from_automatic(page: AutomaticPage) -> _ResponseState:
    values = tuple(
        IdCorrectionValue(cell.selected_digit, cell.status) for cell in page.student_id.cells
    )
    student_id, status = _id_state(values, page.student_id.status)
    response = EffectiveResponse(
        page.page_ref.work_item_id,
        page.page_ref.source_kind,
        page.page_ref.source_label,
        student_id,
        status,
        tuple(answer.value for answer in page.answers),
    )
    return _ResponseState(response, values, page.student_id.status)


def _from_imported(response: ImportedResponseRef) -> _ResponseState:
    student_id = response.raw_student_id if _DIGITS.fullmatch(response.raw_student_id) else None
    status = StudentIdStatus.NORMAL if student_id is not None else StudentIdStatus.INVALID
    effective = EffectiveResponse(
        response.work_item_id,
        SourceKind.IMPORTED_XLSX,
        response.source_filename,
        student_id,
        status,
        response.answers,
    )
    return _ResponseState(effective, tuple(_response_id_cells(effective)), status)


def _validated_apply(
    states: Sequence[_ResponseState],
    corrections: Iterable[Correction],
    existing_events: Iterable[CorrectionEvent],
    *,
    session_id: str,
    expected_base_revision: int,
) -> Result[tuple[_ResponseState, ...]]:
    correction_values = tuple(corrections)
    raw_events = tuple(item for item in correction_values if isinstance(item, CorrectionEvent))
    authority = _preflight_event_authority(raw_events, session_id, expected_base_revision)
    if isinstance(authority, Err):
        return authority
    ordered = _ordered(correction_values)
    if isinstance(ordered, Err):
        return ordered
    events = tuple(item for item in ordered.value if isinstance(item, CorrectionEvent))
    authority = _validate_event_revision_groups(events, expected_base_revision)
    if isinstance(authority, Err):
        return authority
    idempotency = validate_idempotency(events, existing_events)
    if isinstance(idempotency, Err):
        return idempotency
    applied = _apply_states(states, ordered.value)
    if isinstance(applied, Err):
        return applied
    before_counts = _valid_id_counts(tuple(state.response for state in states))
    after_counts = _valid_id_counts(tuple(state.response for state in applied.value))
    for student_id, after_count in after_counts.items():
        if after_count > 1 and after_count > before_counts.get(student_id, 0):
            return _error(
                "DUPLICATE_EFFECTIVE_ID",
                "corrections create or increase a duplicate effective student ID",
                "corrections",
            )
    return applied


def _preflight_event_authority(
    events: Sequence[CorrectionEvent],
    session_id: str,
    expected_base_revision: int,
) -> Result[None]:
    """Validate raw committed events before their ordering is inspected."""
    if not isinstance(session_id, str) or not session_id:
        return _error(
            "INVALID_CORRECTION_CONTEXT",
            "authoritative session_id must be a non-empty string",
            "session_id",
        )
    if type(expected_base_revision) is not int or expected_base_revision < 1:
        return _error(
            "INVALID_CORRECTION_CONTEXT",
            "authoritative expected_base_revision must be a positive integer",
            "expected_base_revision",
        )
    for event in events:
        if event.session_id != session_id:
            return _error(
                "CORRECTION_SESSION_MISMATCH",
                "committed correction session does not match the authoritative session",
                "session_id",
            )
        if event.expected_base_revision < expected_base_revision:
            return _error(
                "STALE_CORRECTION_BASE_REVISION",
                "committed correction expected base revision predates the authoritative revision",
                "expected_base_revision",
            )
    return Ok(None)


def _validate_event_revision_groups(
    events: Sequence[CorrectionEvent], expected_base_revision: int
) -> Result[None]:
    """Require deterministic, contiguous committed revision generations."""
    revision = expected_base_revision
    position = 0
    while position < len(events):
        group_revision = events[position].committed_revision
        group_end = position
        while group_end < len(events) and events[group_end].committed_revision == group_revision:
            event = events[group_end]
            if event.expected_base_revision != revision:
                return _error(
                    "STALE_CORRECTION_BASE_REVISION",
                    "committed correction expected base revision does not match the revision chain",
                    "expected_base_revision",
                )
            group_end += 1
        if group_revision != revision + 1:
            return _error(
                "INVALID_COMMITTED_REVISION",
                "committed correction revision must immediately succeed its base revision",
                "committed_revision",
            )
        revision = group_revision
        position = group_end
    return Ok(None)


def _state_from_response(response: EffectiveResponse) -> _ResponseState:
    return _ResponseState(response, tuple(_response_id_cells(response)), response.student_id_status)


def _apply_states(
    states: Sequence[_ResponseState], corrections: Sequence[Correction]
) -> Result[tuple[_ResponseState, ...]]:
    current = list(states)
    positions = {state.response.work_item_id: index for index, state in enumerate(current)}
    if len(positions) != len(current):
        return _error(
            "DUPLICATE_WORK_ITEM",
            "effective responses contain duplicate work-item IDs",
            "responses",
        )
    for correction in corrections:
        position = positions.get(correction.work_item_id)
        if position is None:
            return _error(
                "CORRECTION_TARGET_MISSING", "correction work item does not exist", "work_item_id"
            )
        state = current[position]
        response = state.response
        if correction.target_kind is TargetKind.ID_CELL:
            cells = list(state.id_cells)
            before = cells[correction.target_key]
            if before != correction.before:
                return _error(
                    "STALE_CORRECTION",
                    "correction before value does not match the effective value",
                    _target_name(correction.target_kind, correction.target_key),
                )
            if correction.after == before:
                return _error(
                    "NOOP_CORRECTION",
                    "correction after value must change the effective value",
                    _target_name(correction.target_kind, correction.target_key),
                )
            if not isinstance(correction.after, IdCorrectionValue):
                return _error(
                    "INVALID_CORRECTION",
                    "ID-cell correction after value must be an ID correction value",
                    _target_name(correction.target_kind, correction.target_key),
                )
            cells[correction.target_key] = correction.after
            student_id, status = _id_state(tuple(cells), state.original_id_status)
            targets = _targets_with(
                response.corrected_targets, correction.target_kind, correction.target_key
            )
            current[position] = _ResponseState(
                replace(
                    response,
                    student_id=student_id,
                    student_id_status=status,
                    corrected_targets=targets,
                ),
                tuple(cells),
                state.original_id_status,
            )
        else:
            before_answer = response.answers[correction.target_key - 1]
            if before_answer != correction.before:
                return _error(
                    "STALE_CORRECTION",
                    "correction before value does not match the effective value",
                    _target_name(correction.target_kind, correction.target_key),
                )
            if correction.after == before_answer:
                return _error(
                    "NOOP_CORRECTION",
                    "correction after value must change the effective value",
                    _target_name(correction.target_kind, correction.target_key),
                )
            if not isinstance(correction.after, AnswerValue):
                return _error(
                    "INVALID_CORRECTION",
                    "answer-cell correction after value must be an answer value",
                    _target_name(correction.target_kind, correction.target_key),
                )
            answers = list(response.answers)
            answers[correction.target_key - 1] = correction.after
            targets = _targets_with(
                response.corrected_targets, correction.target_kind, correction.target_key
            )
            current[position] = _ResponseState(
                replace(response, answers=tuple(answers), corrected_targets=targets),
                state.id_cells,
                state.original_id_status,
            )
    return Ok(tuple(current))


def _response_id_cells(response: EffectiveResponse) -> list[IdCorrectionValue]:
    if response.student_id is not None:
        return [IdCorrectionValue(digit, FieldStatus.NORMAL) for digit in response.student_id]
    status = (
        FieldStatus.UNCERTAIN
        if response.student_id_status is StudentIdStatus.UNREADABLE
        else FieldStatus.BLANK
    )
    return [IdCorrectionValue(None, status) for _ in range(8)]


def _id_state(
    values: tuple[IdCorrectionValue, ...], original_status: StudentIdStatus
) -> tuple[str | None, StudentIdStatus]:
    if all(value.status is FieldStatus.NORMAL for value in values):
        return "".join(value.digit or "" for value in values), StudentIdStatus.NORMAL
    if original_status is StudentIdStatus.UNREADABLE:
        return None, StudentIdStatus.UNREADABLE
    return None, StudentIdStatus.INVALID


def _targets_with(targets: tuple[str, ...], kind: TargetKind, key: int) -> tuple[str, ...]:
    parsed = set(targets)
    parsed.add(_target_name(kind, key))
    return tuple(
        sorted(
            parsed,
            key=lambda target: (
                0 if target.startswith("id_cell:") else 1,
                int(target.split(":", 1)[1]),
            ),
        )
    )


def _valid_id_counts(responses: Sequence[EffectiveResponse]) -> dict[str, int]:
    result: dict[str, int] = {}
    for response in responses:
        if response.student_id is not None and _DIGITS.fullmatch(response.student_id):
            result[response.student_id] = result.get(response.student_id, 0) + 1
    return result


__all__ = [
    "apply_correction_batch",
    "project_effective_responses",
    "validate_correction_batch",
    "validate_correction_event_history",
    "validate_idempotency",
]
