"""Which questions an exam's answer sheets print, and keys that ask beyond them."""

from __future__ import annotations

from collections.abc import Sequence

from omr_grader.domain.enums import AnswerStatus, KeyQuestionStatus
from omr_grader.domain.models import AnswerKeySnapshot, EffectiveResponse

_ALL_QUESTIONS = tuple(range(1, 101))


def printed_questions(responses: Sequence[EffectiveResponse]) -> tuple[int, ...]:
    """Questions every answer sheet prints: no response reads them unasked.

    One sheet is enough to leave a question out, so a correction typed on another sheet
    cannot make it gradable.  Without responses every question counts as printed.
    """
    return tuple(
        question
        for question in _ALL_QUESTIONS
        if all(
            response.answers[question - 1].status is not AnswerStatus.UNASKED
            for response in responses
        )
    )


def asked_off_form(key: AnswerKeySnapshot, printed: Sequence[int]) -> tuple[int, ...]:
    """Questions the key asks that the answer sheets do not print.

    Grading them would give every student zero, so the key must leave them out.
    """
    on_form = set(printed)
    return tuple(
        entry.question
        for entry in key.entries
        if entry.status is not KeyQuestionStatus.UNASKED and entry.question not in on_form
    )


def question_ranges(questions: Sequence[int]) -> str:
    """Compact Korean-friendly ranges such as 51~60, 75."""
    parts: list[str] = []
    start = previous = questions[0]
    for question in (*questions[1:], None):
        if question is not None and question == previous + 1:
            previous = question
            continue
        parts.append(str(start) if start == previous else f"{start}~{previous}")
        if question is not None:
            start = previous = question
    return ", ".join(parts)


def off_form_reason(missing: Sequence[int], printed: Sequence[int]) -> str:
    """Why a key asking ``missing`` cannot grade sheets that print ``printed``."""
    ranges = question_ranges(missing)
    last = max(printed, default=0)
    if last and tuple(printed) == tuple(range(1, last + 1)) and min(missing) > last:
        return (
            f"정답표에 {ranges}번이 있지만 답안지에는 {last}번까지만 있습니다. "
            f"정답표에서 {ranges}번 행을 지운 뒤 다시 불러오세요."
        )
    return (
        f"정답표의 {ranges}번 문항은 답안지 양식에 없습니다. "
        "정답표에서 그 행을 지운 뒤 다시 불러오세요."
    )


__all__ = ["asked_off_form", "off_form_reason", "printed_questions", "question_ranges"]
