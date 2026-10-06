"""Canonical score averages shared by grading, persisted snapshots, and display."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import Final

AVERAGE_FRACTION_DIGITS: Final = 12
"""Committed decimals never carry more than twelve fractional digits."""

_DISPLAY_QUANTUM: Final = Decimal("0.01")


def score_average(scores: Sequence[Decimal]) -> Decimal:
    """Return the mean of nonnegative ``scores`` rounded half-up to twelve digits.

    The rounding is taken from the exact rational mean, so 166/3 commits as
    ``55.333333333333`` while terminating means such as 151/2 stay ``75.5``.
    """
    if not scores:
        raise ValueError("an average needs at least one score")
    mean = sum((Fraction(score) for score in scores), Fraction(0)) / len(scores)
    if mean < 0:
        raise ValueError("scores must be nonnegative")
    scaled = mean * 10**AVERAGE_FRACTION_DIGITS
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    if 2 * remainder >= scaled.denominator:
        quotient += 1
    with localcontext() as context:
        context.prec = max(28, len(str(quotient)) + 1)
        return _canonical(Decimal(quotient).scaleb(-AVERAGE_FRACTION_DIGITS))


def round_average(value: Decimal) -> Decimal:
    """Round a persisted average to the committed twelve-digit form.

    2.1.0 and 2.1.1 committed the unrounded 28-digit quotient (``75.3333…`` with
    26 fractional digits); those snapshots stay readable through this rounding.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("average must be a finite Decimal")
    with localcontext() as context:
        context.prec = max(28, value.adjusted() + AVERAGE_FRACTION_DIGITS + 2)
        quantized = value.quantize(
            Decimal(1).scaleb(-AVERAGE_FRACTION_DIGITS), rounding=ROUND_HALF_UP
        )
        return _canonical(quantized)


def with_rounded_average(scores: object) -> object:
    """Return a committed score payload whose average uses the twelve-digit form.

    Committed scores are compared with freshly recomputed ones before a session is
    opened or amended; rounding the stored average first keeps sessions graded by
    2.1.0–2.1.1 openable, regradable and correctable. Other values pass unchanged.
    """
    if not isinstance(scores, Mapping):
        return scores
    statistics = scores.get("statistics")
    if not isinstance(statistics, Mapping):
        return scores
    average = statistics.get("average_score")
    if not isinstance(average, str):
        return scores
    try:
        rounded = round_average(Decimal(average))
    except (InvalidOperation, ValueError):
        return scores
    return {**scores, "statistics": {**statistics, "average_score": str(rounded)}}


def display_average(value: str | None) -> str:
    """Format a committed average for people: two fractional digits, no padding."""
    if value is None or not value.strip():
        return "" if value is None else value
    try:
        number = Decimal(value)
        if not number.is_finite():
            return value
        rounded = number.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return value
    return format(_canonical(rounded), "f")


def _canonical(value: Decimal) -> Decimal:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal("0" if text in {"", "-0"} else text)


__all__ = [
    "AVERAGE_FRACTION_DIGITS",
    "display_average",
    "round_average",
    "score_average",
    "with_rounded_average",
]
