"""Averages commit within the twelve-digit decimal contract and stay readable."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from omr_grader.application.dto import (
    ScoreInput,
    ScoreResult,
    ScoreSet,
    ScoreStatistics,
    SnapshotRef,
)
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    ExamTerm,
    KeyQuestionStatus,
    SessionState,
    SourceKind,
    StudentIdStatus,
)
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.grading import score_effective
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    DashboardIndexEntry,
    EffectiveResponse,
)
from omr_grader.domain.score_average import (
    display_average,
    round_average,
    score_average,
    with_rounded_average,
)
from omr_grader.infrastructure.dashboard_repository import project_dashboard_entry

SESSION_ID = "scan-a539fe2d728b48b4b5a639cbab31fb78"
FOLDER = "26졸업고사p1_261006_122058"
LEGACY_AVERAGE = "75.33333333333333333333333333"


def _decimals(*values: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(value) for value in values)


@pytest.mark.parametrize(
    ("scores", "expected"),
    (
        (("70", "74", "82"), "75.333333333333"),
        (("75", "76"), "75.5"),
        (("80", "80"), "80"),
        (("0",), "0"),
        (("1", "1", "1", "1", "1", "1", "2"), "1.142857142857"),
        (("2", "2", "2", "2", "2", "2", "2", "2", "2", "2", "2", "3"), "2.083333333333"),
        (("0.000000000001", "0"), "0.000000000001"),
        (("0.000000000001", "0", "0"), "0"),
    ),
)
def test_score_average_rounds_half_up_to_twelve_canonical_digits(
    scores: tuple[str, ...], expected: str
) -> None:
    average = score_average(_decimals(*scores))

    assert format(average, "f") == expected


def test_score_average_keeps_large_exact_scores() -> None:
    large = Decimal("123456789012345678901234567890.123456789012")
    assert score_average((large,)) == large
    assert format(score_average((large, Decimal(0))), "f") == (
        "61728394506172839450617283945.061728394506"
    )


def test_score_average_rejects_empty_and_negative_inputs() -> None:
    with pytest.raises(ValueError):
        score_average(())
    with pytest.raises(ValueError):
        score_average((Decimal("-1"),))


def test_round_average_reads_the_unrounded_2_1_0_quotient() -> None:
    assert format(round_average(Decimal(LEGACY_AVERAGE)), "f") == "75.333333333333"
    assert format(round_average(Decimal("75.5")), "f") == "75.5"
    assert format(round_average(Decimal("75.5000")), "f") == "75.5"
    with pytest.raises(ValueError):
        round_average(Decimal("NaN"))


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("75.333333333333", "75.33"),
        ("75.335", "75.34"),
        ("75.5", "75.5"),
        ("80", "80"),
        ("0", "0"),
        (LEGACY_AVERAGE, "75.33"),
        ("", ""),
        (None, ""),
        ("not-a-number", "not-a-number"),
    ),
)
def test_display_average_shows_two_fraction_digits_at_most(
    value: str | None, expected: str
) -> None:
    assert display_average(value) == expected


def _statistics(average: str) -> ScoreStatistics:
    return ScoreStatistics(3, Decimal(average), Decimal("82"), Decimal("70"))


def _rows() -> tuple[ScoreResult, ...]:
    return (
        ScoreResult("wi_a", Decimal("70"), 3),
        ScoreResult("wi_b", Decimal("74"), 2),
        ScoreResult("wi_c", Decimal("82"), 1),
        ScoreResult("wi_review", None, None),
    )


@pytest.mark.parametrize("average", ("75.333333333333", LEGACY_AVERAGE))
def test_score_set_accepts_new_and_legacy_committed_averages(average: str) -> None:
    score_set = ScoreSet(Decimal("100"), _rows(), _statistics(average))

    assert score_set.statistics.participant_count == 3


@pytest.mark.parametrize("average", ("75.33", "75.333333333334", "75.4"))
def test_score_set_rejects_averages_that_do_not_match_the_rows(average: str) -> None:
    with pytest.raises(ValueError, match="statistics must match scored rows"):
        ScoreSet(Decimal("100"), _rows(), _statistics(average))


def _answer(choices: tuple[int, ...], status: AnswerStatus) -> AnswerValue:
    return AnswerValue(choices, status)


def _key() -> AnswerKeySnapshot:
    return AnswerKeySnapshot(
        1,
        AnswerKeySnapshotKind.WORKBOOK,
        "key.xlsx",
        "a" * 64,
        "정답표",
        "v1",
        tuple(
            AnswerKeyEntry(
                number, _answer((1,), AnswerStatus.NORMAL), "1", KeyQuestionStatus.ANSWER
            )
            for number in range(1, 101)
        ),
        (),
    )


def _response(work_item_id: str, correct: int) -> EffectiveResponse:
    return EffectiveResponse(
        work_item_id,
        SourceKind.PDF,
        "scan.pdf",
        "12345678",
        StudentIdStatus.NORMAL,
        tuple(
            _answer((1,) if number <= correct else (2,), AnswerStatus.NORMAL)
            for number in range(1, 101)
        ),
    )


def test_grading_three_students_commits_an_average_the_dashboard_accepts() -> None:
    scores = score_effective(
        ScoreInput(
            (_response("wi_a", 70), _response("wi_b", 74), _response("wi_c", 82)),
            _key(),
        )
    )

    average = scores.statistics.average_score
    assert average is not None
    assert str(average) == "75.333333333333"
    entry = DashboardIndexEntry(
        SESSION_ID,
        2,
        "c1a4399ac8c240e389bc7a89e3077a75",
        "5" * 64,
        FOLDER,
        "26졸업고사p1",
        None,
        ExamTerm.UNSPECIFIED,
        SessionState.GRADED,
        "2026-10-06T03:28:10.415374Z",
        3,
        str(average),
        "82",
        "70",
        0,
    )
    assert entry.average_score == "75.333333333333"


@dataclass(frozen=True)
class _Summary:
    manual_review: int


@dataclass(frozen=True)
class _Manifest:
    summary: _Summary


@dataclass
class _Lease:
    root_path: str
    payload: bytes
    snapshot_ref: SnapshotRef
    manifest: _Manifest = field(default_factory=lambda: _Manifest(_Summary(9)))

    def open_allowlisted(self, name: str) -> Ok[io.BytesIO] | Err:
        assert name == "semantic_inputs.json"
        return Ok(io.BytesIO(self.payload))


def _legacy_lease(tmp_path: Path, average: str) -> _Lease:
    session = tmp_path / FOLDER
    generation = session / "generations" / "g00000002_c1a4399ac8c240e389bc7a89e3077a75"
    generation.mkdir(parents=True)
    (session / "LOCATION.json").write_text(
        json.dumps(
            {
                "display_name": FOLDER,
                "operation_id": "67cf7c6e36a14ed1af37bf1118898bab",
                "schema_version": 1,
                "session_id": SESSION_ID,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session_record = {
        "created_at": "2026-10-06T03:20:58.384673Z",
        "exam_name": "26졸업고사p1",
        "exam_term": "unspecified",
        "exam_year": None,
        "graded_at": "2026-10-06T03:28:10.415374Z",
        "revision": 2,
        "schema_version": 1,
        "session_id": SESSION_ID,
        "state": "graded",
        "updated_at": "2026-10-06T03:28:10.415374Z",
    }
    statistics = {
        "average_score": average,
        "highest_score": "82",
        "lowest_score": "70",
        "participant_count": 3,
    }
    payload = json.dumps(
        {"combined": {"session": session_record, "scores": {"statistics": statistics}}},
        ensure_ascii=False,
    ).encode("utf-8")
    return _Lease(
        str(generation),
        payload,
        SnapshotRef(SESSION_ID, 2, "c1a4399ac8c240e389bc7a89e3077a75", "5" * 64),
    )


@pytest.mark.parametrize("average", (LEGACY_AVERAGE, "75.333333333333"))
def test_dashboard_lists_sessions_committed_with_either_average_form(
    tmp_path: Path, average: str
) -> None:
    projected = project_dashboard_entry(_legacy_lease(tmp_path, average))  # type: ignore[arg-type]

    assert isinstance(projected, Ok)
    assert projected.value.session_id == SESSION_ID
    assert projected.value.average_score == "75.333333333333"
    assert projected.value.participant_count == 3
    assert projected.value.needs_review_count == 9


def test_with_rounded_average_rewrites_only_a_committed_average() -> None:
    legacy = {
        "maximum_score": "100",
        "rows": [],
        "statistics": {
            "participant_count": 3,
            "average_score": LEGACY_AVERAGE,
            "highest_score": "82",
            "lowest_score": "70",
        },
    }

    rounded = with_rounded_average(legacy)

    assert rounded == {
        **legacy,
        "statistics": {**legacy["statistics"], "average_score": "75.333333333333"},
    }
    assert legacy["statistics"]["average_score"] == LEGACY_AVERAGE
    assert with_rounded_average(rounded) == rounded


@pytest.mark.parametrize(
    "value",
    (
        None,
        [],
        {"rows": []},
        {"statistics": None},
        {"statistics": {"average_score": None}},
        {"statistics": {"average_score": "not-a-number"}},
    ),
)
def test_with_rounded_average_leaves_other_payloads_alone(value: object) -> None:
    assert with_rounded_average(value) == value
