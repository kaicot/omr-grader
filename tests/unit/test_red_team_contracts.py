"""Adversarial checks for the frozen G001 wire and application boundaries."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any, get_args, get_origin, get_type_hints

import pytest

import omr_grader.application.dto as dto
import omr_grader.application.ports as ports
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    ExamTerm,
    KeyQuestionStatus,
    RosterRowStatus,
    RosterSnapshotKind,
    SessionState,
    SourceKind,
    StudentIdStatus,
)
from omr_grader.domain.errors import Err, Ok, Result
from omr_grader.domain.models import (
    SCHEMA_VERSION,
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    DashboardIndexEntry,
    EffectiveResponse,
    RatioRect,
    RosterEntry,
    RosterSnapshot,
)


def _unset_key() -> AnswerKeySnapshot:
    entries = tuple(
        AnswerKeyEntry(
            question, AnswerValue((), AnswerStatus.UNASKED), "0", KeyQuestionStatus.UNASKED
        )
        for question in range(1, 101)
    )
    return AnswerKeySnapshot(1, AnswerKeySnapshotKind.UNSET, None, None, None, "v1", entries, ())


def test_persisted_wire_schema_version_is_the_approved_integer_v1() -> None:
    assert SCHEMA_VERSION == 1


def _is_result(annotation: object) -> bool:
    if get_origin(annotation) is Result:
        return True
    members = get_args(annotation)
    return (
        len(members) == 2 and Err in members and any(get_origin(member) is Ok for member in members)
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda wire: wire["entries"][0]["answer"].__setitem__("unknown", True),
            id="unknown-nested-answer-key",
        ),
        pytest.param(
            lambda wire: wire["entries"][0].__setitem__("unknown", True),
            id="unknown-nested-entry",
        ),
        pytest.param(
            lambda wire: wire["entries"][0]["answer"].update(choices=[], status="normal"),
            id="impossible-answer-status",
        ),
        pytest.param(
            lambda wire: wire["entries"][0]["answer"].update(choices=[float("nan")]),
            id="nan-in-integer-wire-field",
        ),
    ],
)
def test_nested_semantic_wire_rejects_unknown_types_and_impossible_states(mutate: Any) -> None:
    wire = _unset_key().to_dict()
    mutate(wire)

    with pytest.raises((TypeError, ValueError)):
        AnswerKeySnapshot.from_dict(wire)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_ratio_wire_rejects_nonfinite_decimal_strings(value: str) -> None:
    with pytest.raises(ValueError, match="decimal must be finite"):
        RatioRect.from_dict({"x": value, "y": "0", "w": "1", "h": "1"})


@pytest.mark.parametrize(
    "value",
    [
        {"vendor.payload": {"items": [True, {"count": 1}, None]}},
        {"vendor.payload": ({"items": ("safe", 1)},)},
    ],
)
def test_extensions_accept_recursive_json_safe_values(value: dict[str, object]) -> None:
    snapshot = AnswerKeySnapshot(
        1,
        AnswerKeySnapshotKind.UNSET,
        None,
        None,
        None,
        "v1",
        _unset_key().entries,
        (),
        value,
    )

    assert AnswerKeySnapshot.from_dict(snapshot.to_dict()) == snapshot


@pytest.mark.parametrize(
    "value",
    [
        {"vendor.payload": {"bad": 1.5}},
        {"vendor.payload": {"bad": object()}},
        {"vendor.payload": {1: "bad-key"}},
    ],
)
def test_extensions_reject_recursive_non_json_values(value: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="JSON"):
        AnswerKeySnapshot(
            1,
            AnswerKeySnapshotKind.UNSET,
            None,
            None,
            None,
            "v1",
            _unset_key().entries,
            (),
            value,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda wire: wire.__setitem__("schema_version", True), id="boolean-schema"),
        pytest.param(
            lambda wire: wire["entries"][0].__setitem__("question", True), id="boolean-question"
        ),
    ],
)
def test_integer_wire_fields_reject_booleans(mutate: Any) -> None:
    wire = _unset_key().to_dict()
    mutate(wire)

    with pytest.raises(ValueError, match="integer|schema version"):
        AnswerKeySnapshot.from_dict(wire)


def test_public_port_methods_have_only_result_failure_channel_and_dtos_do_not_leak_it() -> None:
    for port_name in ports.__all__:
        protocol = getattr(ports, port_name)
        for method_name, method in protocol.__dict__.items():
            if method_name.startswith("_") or not callable(method):
                continue
            assert _is_result(get_type_hints(method)["return"]), f"{port_name}.{method_name}"

    forbidden_names = {"warning", "warnings", "error", "errors"}
    for dto_name in dto.__all__:
        dto_type = getattr(dto, dto_name)
        if is_dataclass(dto_type):
            assert forbidden_names.isdisjoint(field.name for field in fields(dto_type)), dto_name


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: RosterSnapshot(1, RosterSnapshotKind.WORKBOOK, None, None, None, "v1", (), ()),
            id="roster-workbook-without-provenance",
        ),
        pytest.param(
            lambda: AnswerKeySnapshot(
                1,
                AnswerKeySnapshotKind.WORKBOOK,
                "key.xlsx",
                None,
                "Sheet1",
                "v1",
                _unset_key().entries,
                (),
            ),
            id="key-workbook-without-hash",
        ),
    ],
)
def test_roster_and_key_require_complete_source_provenance(factory: Any) -> None:
    with pytest.raises(ValueError, match="provenance"):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: RosterEntry(
                "row-1", 1, 0, "00000000", "00000000", "name", RosterRowStatus.NORMAL, ("z", "a")
            ),
            id="unsorted-roster-issues",
        ),
        pytest.param(
            lambda: RosterEntry(
                "row-1", 1, 0, "00000000", "not-an-id", "name", RosterRowStatus.NORMAL, ()
            ),
            id="invalid-normalized-roster-id",
        ),
    ],
)
def test_roster_rows_reject_invalid_semantics(factory: Any) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize("score", ["1.0", "-1", "NaN", "Infinity"])
def test_dashboard_scores_require_canonical_nonnegative_decimals(score: str) -> None:
    with pytest.raises(ValueError):
        DashboardIndexEntry(
            "session-1",
            1,
            "generation-1",
            "a" * 64,
            "folder",
            "exam",
            2026,
            ExamTerm.FIRST,
            SessionState.CREATED,
            None,
            0,
            score,
            None,
            None,
            0,
        )


def test_effective_response_requires_consistent_identifier_and_ordered_targets() -> None:
    answers = tuple(AnswerValue((), AnswerStatus.BLANK) for _ in range(100))
    with pytest.raises(ValueError, match="effective response"):
        EffectiveResponse(
            "work-1", SourceKind.IMAGE, "scan.png", None, StudentIdStatus.NORMAL, answers
        )
    duplicate = EffectiveResponse(
        "work-1",
        SourceKind.IMAGE,
        "scan.png",
        "00000000",
        StudentIdStatus.DUPLICATE,
        answers,
    )
    assert duplicate.student_id == "00000000"
    assert duplicate.student_id_status is StudentIdStatus.DUPLICATE
    with pytest.raises(ValueError, match="unique and ordered"):
        EffectiveResponse(
            "work-1",
            SourceKind.IMAGE,
            "scan.png",
            None,
            StudentIdStatus.UNREADABLE,
            answers,
            ("answer_cell:2", "id_cell:0"),
        )
