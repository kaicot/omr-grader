"""Grading refuses an answer key that asks questions an answer sheet does not print."""

from __future__ import annotations

from types import SimpleNamespace

from omr_grader.application.dto import AnswerKeyValidation, RegradeCommand
from omr_grader.application.grading_use_case import (
    GradingUseCase,
    _questions_not_on_form,
    _ranges,
)
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    KeyQuestionStatus,
    SourceKind,
    StudentIdStatus,
)
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    EffectiveResponse,
)

PRINTED = 50


def _response(
    work_item_id: str, printed: int = PRINTED, typed: int | None = None
) -> EffectiveResponse:
    """A sheet printing questions 1..printed, all answered 1; ``typed`` is a corrected extra."""
    answers = [
        AnswerValue((1,), AnswerStatus.NORMAL)
        if question <= printed or question == typed
        else AnswerValue((), AnswerStatus.UNASKED)
        for question in range(1, 101)
    ]
    return EffectiveResponse(
        work_item_id,
        SourceKind.IMAGE,
        "scan.png",
        "12345678",
        StudentIdStatus.NORMAL,
        tuple(answers),
    )


def _key(asked: int) -> AnswerKeySnapshot:
    entries = tuple(
        AnswerKeyEntry(
            question, AnswerValue((1,), AnswerStatus.NORMAL), "1", KeyQuestionStatus.ANSWER
        )
        if question <= asked
        else AnswerKeyEntry(
            question, AnswerValue((), AnswerStatus.UNASKED), "0", KeyQuestionStatus.UNASKED
        )
        for question in range(1, 101)
    )
    return AnswerKeySnapshot(
        1, AnswerKeySnapshotKind.WORKBOOK, "key.xlsx", "a" * 64, "정답표", "v1", entries, ()
    )


def test_a_key_within_the_printed_questions_passes() -> None:
    responses = (_response("wi_a"), _response("wi_b"))

    assert _questions_not_on_form(responses, _key(PRINTED)) == ()
    assert _questions_not_on_form(responses, _key(30)) == ()
    assert _questions_not_on_form((), _key(100)) == ()


def test_asked_questions_beyond_the_form_are_reported_as_ranges() -> None:
    responses = (_response("wi_a"), _response("wi_b"))

    missing = _questions_not_on_form(responses, _key(53))

    assert missing == (51, 52, 53)
    assert _ranges(missing) == "51~53"
    assert _ranges((51, 52, 53, 60, 75, 76)) == "51~53, 60, 75~76"
    assert _ranges((7,)) == "7"


def test_one_sheet_without_the_question_blocks_it_even_when_another_was_corrected() -> None:
    # A value typed on one sheet for an unprinted question must not unblock it for the rest.
    responses = (_response("wi_a", typed=51), _response("wi_b"))

    assert _questions_not_on_form(responses, _key(51)) == (51,)


def test_regrade_stops_before_scoring_and_names_the_questions() -> None:
    snapshot = SimpleNamespace(responses=(_response("wi_a"), _response("wi_b")))
    commits: list[object] = []

    class Snapshots:
        def read_grading_snapshot(self, session_id: str, revision: int) -> Ok[object]:
            return Ok(snapshot)

    class AnswerKeys:
        def validate_answer_key(self, request: object) -> Ok[AnswerKeyValidation]:
            return Ok(AnswerKeyValidation(_key(100)))

    class Coordinator:
        def commit_generation(self, mutation: object) -> None:
            commits.append(mutation)

    result = GradingUseCase(Snapshots(), AnswerKeys(), Coordinator()).regrade(  # type: ignore[arg-type]
        RegradeCommand("session-1", 1, "key.xlsx", "정답표", "regrade-operation")
    )

    assert isinstance(result, Err)
    error = result.errors[0]
    assert error.code == "ANSWER_KEY_NOT_ON_FORM"
    assert error.context["questions"] == "51~100"
    assert "정답표의 51~100번 문항은 답안지 양식에 없습니다" in str(error.context["reason"])
    assert commits == []
