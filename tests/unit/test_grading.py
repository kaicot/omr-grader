from dataclasses import replace
from decimal import ROUND_UP, Decimal, localcontext

import pytest

from omr_grader.application.dto import ScoreInput
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    KeyQuestionStatus,
    SourceKind,
    StudentIdStatus,
)
from omr_grader.domain.grading import (
    CORRECT,
    EXCLUDED,
    INCORRECT,
    REVIEW,
    question_outcomes,
    score_effective,
)
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    EffectiveResponse,
)


def _answer(choices=(), status=AnswerStatus.BLANK):
    return AnswerValue(tuple(choices), status)


def _key():
    entries = []
    for number in range(1, 101):
        if number == 1:
            entries.append(
                AnswerKeyEntry(
                    number, _answer((1, 2), AnswerStatus.MULTIPLE), "1.5", KeyQuestionStatus.ANSWER
                )
            )
        elif number == 2:
            entries.append(
                AnswerKeyEntry(number, _answer((), AnswerStatus.ALL), "2", KeyQuestionStatus.ALL)
            )
        else:
            entries.append(
                AnswerKeyEntry(
                    number, _answer((), AnswerStatus.UNASKED), "0", KeyQuestionStatus.UNASKED
                )
            )
    return AnswerKeySnapshot(
        1, AnswerKeySnapshotKind.WORKBOOK, "key.xlsx", "a" * 64, "정답", "v1", tuple(entries), ()
    )


def _response(work_item_id, first, second=(), second_status=AnswerStatus.BLANK):
    first_status = (
        AnswerStatus.BLANK
        if not first
        else AnswerStatus.MULTIPLE
        if len(first) > 1
        else AnswerStatus.NORMAL
    )
    answers = [
        _answer(first, first_status),
        _answer(second, second_status),
    ]
    answers.extend(_answer() for _ in range(98))
    return EffectiveResponse(
        work_item_id,
        SourceKind.IMPORTED_XLSX,
        "row",
        "12345678",
        StudentIdStatus.NORMAL,
        tuple(answers),
    )


def test_decimal_zero_fraction_and_large_scores_are_exact():
    key = _key()
    entries = list(key.entries)
    entries[0] = replace(entries[0], points="0.1")
    entries[1] = replace(entries[1], points="0.2")
    fractional = score_effective(
        ScoreInput((_response("fraction", (1, 2)),), replace(key, entries=tuple(entries)))
    )
    assert fractional.maximum_score == Decimal("0.3")
    assert fractional.rows[0].score == Decimal("0.3")
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        frozen = score_effective(
            ScoreInput((_response("frozen", (1, 2)),), replace(key, entries=tuple(entries)))
        )
    assert frozen.rows[0].score == frozen.statistics.average_score == Decimal("0.3")

    entries[0] = replace(entries[0], points="123456789012345678901234567890.123456789012")
    entries[1] = replace(entries[1], points="0")
    large = score_effective(
        ScoreInput((_response("large", (1, 2)),), replace(key, entries=tuple(entries)))
    )
    assert large.rows[0].score == Decimal("123456789012345678901234567890.123456789012")

    entries[0] = replace(entries[0], points="0")
    zero = score_effective(
        ScoreInput((_response("zero", (1, 2)),), replace(key, entries=tuple(entries)))
    )
    assert zero.maximum_score == zero.rows[0].score == Decimal("0")


def test_competition_ranks_continue_after_multiple_tie_groups():
    key = _key()
    entries = list(key.entries)
    entries[1] = AnswerKeyEntry(
        2, _answer((2,), AnswerStatus.NORMAL), "2", KeyQuestionStatus.ANSWER
    )
    key = replace(key, entries=tuple(entries))
    responses = (
        _response("first_a", (1, 2), (2,), AnswerStatus.NORMAL),
        _response("first_b", (1, 2), (2,), AnswerStatus.NORMAL),
        _response("third_a", (1, 2)),
        _response("third_b", (1, 2)),
        _response("fifth", ()),
    )

    scores = score_effective(ScoreInput(responses, key))

    assert [(row.score, row.rank) for row in scores.rows] == [
        (Decimal("3.5"), 1),
        (Decimal("3.5"), 1),
        (Decimal("1.5"), 3),
        (Decimal("1.5"), 3),
        (Decimal("0"), 5),
    ]


def test_duplicate_work_items_and_malformed_cardinality_are_rejected():
    key = _key()
    response = _response("duplicate", (1, 2))
    with pytest.raises(ValueError, match="unique work_item_id"):
        score_effective(ScoreInput((response, response), key))

    object.__setattr__(response, "answers", response.answers[:-1])
    with pytest.raises(ValueError, match="matching question cardinality"):
        question_outcomes(response, key)


def test_exact_set_all_unasked_and_competition_rank_are_pure():
    key = _key()
    first = _response("wi_a", (1, 2))
    second = _response("wi_b", (1, 2))
    third = _response("wi_c", (1,))
    scores = score_effective(ScoreInput((first, second, third), key))

    assert question_outcomes(first, key)[:3] == (CORRECT, CORRECT, EXCLUDED)
    assert question_outcomes(third, key)[0] == INCORRECT
    assert [(row.score, row.rank) for row in scores.rows] == [
        (Decimal("3.5"), 1),
        (Decimal("3.5"), 1),
        (Decimal("2"), 3),
    ]
    assert scores.statistics.average_score == Decimal("3")


def test_uncertain_is_unscoreable_only_for_its_work_item():
    key = _key()
    uncertain = _response("wi_review", (1, 2), (1,), AnswerStatus.UNCERTAIN)
    scored = _response("wi_scored", (1, 2))
    scores = score_effective(ScoreInput((uncertain, scored), key))

    assert question_outcomes(uncertain, key)[1] == REVIEW
    assert scores.rows[0].score is None and scores.rows[0].rank is None
    assert scores.rows[1].score == Decimal("3.5")


def test_permuting_independent_work_items_preserves_score_rank_mapping():
    key = _key()
    responses = (
        _response("wi_high", (1, 2)),
        _response("wi_low", (1,)),
        _response("wi_tie", (1, 2)),
    )
    forward = score_effective(ScoreInput(responses, key))
    reverse = score_effective(ScoreInput(tuple(reversed(responses)), key))

    assert {row.work_item_id: (row.score, row.rank) for row in forward.rows} == {
        row.work_item_id: (row.score, row.rank) for row in reverse.rows
    }
