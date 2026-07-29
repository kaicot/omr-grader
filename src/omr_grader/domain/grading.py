"""Pure scoring of committed effective responses against a committed answer key."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from omr_grader.application.dto import ScoreInput, ScoreResult, ScoreSet, ScoreStatistics
from omr_grader.domain.enums import AnswerKeySnapshotKind, AnswerStatus, KeyQuestionStatus
from omr_grader.domain.models import AnswerKeySnapshot, EffectiveResponse

REVIEW = "검토"
CORRECT = "O"
INCORRECT = "X"
EXCLUDED = "제외"


def question_outcomes(response: ScoreInput | object, key: object | None = None) -> tuple[str, ...]:
    """Return the Korean display outcome for each key question.

    ``ScoreInput`` is accepted only as a convenience for callers rendering one
    response; normal scoring passes an ``EffectiveResponse`` and an
    ``AnswerKeySnapshot``.  This function deliberately has no workbook or
    persistence dependency.
    """
    if isinstance(response, ScoreInput):
        if len(response.responses) != 1:
            raise ValueError("question outcomes require exactly one response")
        key = response.key
        response = response.responses[0]
    if not isinstance(response, EffectiveResponse) or not isinstance(key, AnswerKeySnapshot):
        raise TypeError("response and key snapshots are required")

    if len(response.answers) != len(key.entries):
        raise ValueError("response and key must have matching question cardinality")

    outcomes: list[str] = []
    for answer, entry in zip(response.answers, key.entries, strict=True):
        if entry.status is KeyQuestionStatus.UNASKED:
            outcomes.append(EXCLUDED)
        elif answer.status is AnswerStatus.UNCERTAIN:
            outcomes.append(REVIEW)
        elif entry.status is KeyQuestionStatus.ALL:
            outcomes.append(CORRECT)
        elif answer.choices == entry.answer.choices:
            # Choice tuples are canonical sorted sets, so equality is exact-set
            # AND semantics rather than a partial-overlap test.
            outcomes.append(CORRECT)
        else:
            outcomes.append(INCORRECT)
    return tuple(outcomes)


def score_effective(score_input: ScoreInput) -> ScoreSet:
    """Score every work item independently using only committed domain values.

    An uncertain answer makes only that work item unscoreable.  Duplicate
    student IDs intentionally do not affect scoring or ranking: work item ID
    is the authority and is the ScoreSet row identity.
    """
    if not isinstance(score_input, ScoreInput):
        raise TypeError("score_input must be ScoreInput")
    if (
        score_input.key.snapshot_kind is not AnswerKeySnapshotKind.WORKBOOK
        or score_input.key.validation_errors
    ):
        raise ValueError("grading requires a valid committed answer key snapshot")

    work_item_ids = tuple(response.work_item_id for response in score_input.responses)
    if len(set(work_item_ids)) != len(work_item_ids):
        raise ValueError("responses must have unique work_item_id values")

    point_values = tuple(
        Decimal(entry.points)
        for entry in score_input.key.entries
        if entry.status is not KeyQuestionStatus.UNASKED
    )
    with localcontext() as context:
        context.prec = _decimal_precision(point_values)
        context.rounding = ROUND_HALF_EVEN
        maximum = sum(point_values, Decimal("0"))
    preliminary: list[tuple[str, Decimal | None]] = []
    for response in score_input.responses:
        outcomes = question_outcomes(response, score_input.key)
        if REVIEW in outcomes:
            preliminary.append((response.work_item_id, None))
            continue
        points = tuple(
            Decimal(entry.points)
            for outcome, entry in zip(outcomes, score_input.key.entries, strict=True)
            if outcome == CORRECT and entry.status is not KeyQuestionStatus.UNASKED
        )
        with localcontext() as context:
            context.prec = _decimal_precision(points)
            context.rounding = ROUND_HALF_EVEN
            score = sum(points, Decimal("0"))
        preliminary.append((response.work_item_id, score))

    scores = tuple(score for _, score in preliminary if score is not None)
    ranks = {score: 1 + sum(other > score for other in scores) for score in set(scores)}
    rows = tuple(
        ScoreResult(work_item_id, score, None if score is None else ranks[score])
        for work_item_id, score in preliminary
    )
    with localcontext() as context:
        context.prec = _decimal_precision(point_values + scores)
        context.rounding = ROUND_HALF_EVEN
        statistics = (
            ScoreStatistics(0, None, None, None)
            if not scores
            else ScoreStatistics(
                len(scores),
                sum(scores, Decimal("0")) / len(scores),
                max(scores),
                min(scores),
            )
        )
        return ScoreSet(maximum, rows, statistics)


def _decimal_precision(values: tuple[Decimal, ...]) -> int:
    """Return enough precision to add nonnegative finite values exactly."""
    if not values:
        return 28
    integer_digits = max(max(value.adjusted() + 1, 0) for value in values)
    fractional_digits = 0
    for value in values:
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int):
            fractional_digits = max(fractional_digits, -exponent, 0)
    return max(28, integer_digits + fractional_digits + len(str(len(values))) + 1)


__all__ = ["CORRECT", "EXCLUDED", "INCORRECT", "REVIEW", "question_outcomes", "score_effective"]
