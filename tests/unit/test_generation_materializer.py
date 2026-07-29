from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omr_grader.application.dto import (
    CorrectionSemanticView,
    EffectiveResponseProjection,
    GenerationMutation,
)
from omr_grader.domain.corrections import validate_correction_event_history
from omr_grader.domain.enums import AnswerStatus, OperationKind, SessionState, TargetKind
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import AnswerValue, CorrectionDraft, CorrectionEvent
from omr_grader.infrastructure.generation_materializer import (
    _read_correction_events,
    _validate_projection_lineage,
)

BLANK = AnswerValue((), AnswerStatus.BLANK)
MARKED = AnswerValue((1,), AnswerStatus.NORMAL)


def _draft(before: AnswerValue = BLANK, after: AnswerValue = MARKED) -> CorrectionDraft:
    return CorrectionDraft("work-item", TargetKind.ANSWER_CELL, 1, before, after, "review")


def _event(
    event_id: str,
    draft: CorrectionDraft,
    *,
    base: int = 1,
    session_id: str = "session",
) -> CorrectionEvent:
    return CorrectionEvent(
        1,
        event_id,
        session_id,
        draft.work_item_id,
        draft.target_kind,
        draft.target_key,
        base,
        draft.before,
        draft.after,
        draft.reason,
        "local",
        "2026-01-01T00:00:00.000000Z",
        base + 1,
        event_id,
    )


def test_correct_event_append_requires_exact_new_suffix() -> None:
    old = _draft()
    new = _draft(MARKED, BLANK)
    result = validate_correction_event_history(
        (_event("old", old), _event("new", new, base=2)),
        (old, new),
        session_id="session",
        expected_new_base_revision=2,
    )
    assert isinstance(result, Ok)


@pytest.mark.parametrize(
    "events,drafts",
    (
        ((), ()),
        ((_event("future", _draft(), base=3),), (_draft(),)),
        (
            (
                _event("new", _draft(), base=2),
                _event("old", _draft(MARKED, BLANK), base=1),
            ),
            (_draft(), _draft(MARKED, BLANK)),
        ),
    ),
)
def test_correct_event_suffix_rejects_missing_future_or_non_suffix_groups(
    events: tuple[CorrectionEvent, ...], drafts: tuple[CorrectionDraft, ...]
) -> None:
    result = validate_correction_event_history(
        events, drafts, session_id="session", expected_new_base_revision=2
    )
    assert isinstance(result, Err)


def test_lifecycle_event_file_round_trips_cumulative_authority(tmp_path: Path) -> None:
    first = _draft()
    second = _draft(MARKED, BLANK)
    events = (_event("first", first), _event("second", second, base=2))
    (tmp_path / "correction_events.json").write_text(
        json.dumps({"schema_version": 1, "events": [event.to_dict() for event in events]}),
        encoding="utf-8",
    )
    assert _read_correction_events(tmp_path) == events


@pytest.mark.parametrize(
    "payload",
    (
        {"schema_version": 1, "events": []},
        {"schema_version": 1, "events": [_event("one", _draft()).to_dict()]},
    ),
)
def test_materializer_event_file_is_typed_and_canonical(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    (tmp_path / "correction_events.json").write_text(json.dumps(payload), encoding="utf-8")
    events = _read_correction_events(tmp_path)
    result = validate_correction_event_history(
        events,
        tuple(
            CorrectionDraft(
                event.work_item_id,
                event.target_kind,
                event.target_key,
                event.before,
                event.after,
                event.reason,
            )
            for event in events
        ),
        session_id="session",
    )
    assert isinstance(result, Ok)


def test_missing_or_mismatched_event_projection_is_rejected() -> None:
    draft = _draft()
    result = validate_correction_event_history(
        (_event("one", draft),), (), session_id="session"
    )
    assert isinstance(result, Err)


def test_altered_event_before_value_is_rejected() -> None:
    draft = _draft()
    altered = _draft(MARKED, BLANK)
    result = validate_correction_event_history(
        (_event("one", draft),), (altered,), session_id="session"
    )
    assert isinstance(result, Err)


@pytest.mark.parametrize(
    "event",
    (
        _event("wrong-session", _draft(), session_id="other"),
        CorrectionEvent(
            1,
            "wrong-revision",
            "session",
            "work-item",
            TargetKind.ANSWER_CELL,
            1,
            1,
            BLANK,
            MARKED,
            "review",
            "local",
            "2026-01-01T00:00:00.000000Z",
            3,
            "wrong-revision",
        ),
    ),
)
def test_materializer_rejects_stale_session_or_revision_event(event: CorrectionEvent) -> None:
    draft = _draft()
    result = validate_correction_event_history((event,), (draft,), session_id="session")
    assert isinstance(result, Err)
def test_lifecycle_projection_rejects_parent_authority_mutation() -> None:
    old = _draft()
    altered = CorrectionDraft(
        old.work_item_id,
        old.target_kind,
        old.target_key,
        old.before,
        old.after,
        "different reason",
    )
    new = _draft(MARKED, BLANK)
    parent = EffectiveResponseProjection((), (), (old,))
    target = EffectiveResponseProjection((), (), (altered, new))
    mutation = GenerationMutation(
        "session",
        "operation",
        OperationKind.CORRECT,
        1,
        SessionState.GRADED,
        CorrectionSemanticView((new,), SessionState.GRADED),
        target,
    )
    with pytest.raises(ValueError, match="correction projection"):
        _validate_projection_lineage(SimpleNamespace(mutation=mutation), parent, target)
