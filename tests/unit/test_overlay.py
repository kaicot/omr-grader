import numpy as np

from omr_grader.domain.enums import AnswerStatus, CellStatus, KeyQuestionStatus
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerRecognition,
    AnswerValue,
    CellEvidence,
    PixelRect,
    RatioRect,
)
from omr_grader.recognition.overlay import (
    BLANK_COLOR,
    CORRECT_COLOR,
    INCORRECT_COLOR,
    NORMAL_COLOR,
    REVIEW_COLOR,
    render_overlay,
    render_scored_overlay,
)


def _cell(index: int, status: CellStatus, selected: bool) -> CellEvidence:
    return CellEvidence(
        index=index,
        question=index + 1,
        digit=None,
        choice=1,
        pixel_rect=PixelRect(index * 20, 10, 16, 16),
        ratio_rect=RatioRect("0", "0", "0.1", "0.1"),
        fill_score="0.5",
        selected=selected,
        status=status,
    )


def test_overlay_is_non_destructive_and_uses_distinct_status_colors() -> None:
    source = np.full((50, 80, 3), 255, dtype=np.uint8)
    original = source.copy()
    evidence = (
        _cell(2, CellStatus.UNCERTAIN, True),
        _cell(0, CellStatus.NORMAL, True),
        _cell(1, CellStatus.BLANK, False),
    )

    result = render_overlay(source, evidence)
    assert hasattr(result, "value")
    overlay = result.value
    assert np.array_equal(source, original)
    assert not np.array_equal(source, overlay)
    assert tuple(overlay[10, 0]) == NORMAL_COLOR
    assert tuple(overlay[10, 20]) == BLANK_COLOR
    assert tuple(overlay[10, 40]) == REVIEW_COLOR


def test_overlay_output_is_deterministic_for_unordered_evidence() -> None:
    source = np.full((50, 80, 3), 255, dtype=np.uint8)
    cells = (_cell(2, CellStatus.MULTIPLE, True), _cell(0, CellStatus.NORMAL, True))

    first = render_overlay(source, cells).value
    second = render_overlay(source, tuple(reversed(cells))).value
    assert np.array_equal(first, second)


def test_scored_overlay_marks_correct_blue_and_wrong_red() -> None:
    source = np.full((60, 140, 3), 255, dtype=np.uint8)
    evidence = tuple(
        CellEvidence(
            index=choice - 1,
            question=1,
            digit=None,
            choice=choice,
            pixel_rect=PixelRect((choice - 1) * 25 + 5, 10, 20, 20),
            ratio_rect=RatioRect("0", "0", "0.1", "0.1"),
            fill_score="0.8" if choice == 2 else "0.1",
            selected=choice == 2,
            status=CellStatus.NORMAL if choice == 2 else CellStatus.BLANK,
        )
        for choice in range(1, 6)
    )
    response = AnswerRecognition(1, AnswerValue((2,), AnswerStatus.NORMAL), evidence)
    key = AnswerKeyEntry(
        1,
        AnswerValue((3,), AnswerStatus.NORMAL),
        "1",
        KeyQuestionStatus.ANSWER,
    )

    overlay = render_scored_overlay(source, evidence, (response,), (key,)).value

    assert tuple(overlay[10, 40]) == INCORRECT_COLOR
    assert tuple(overlay[10, 65]) == CORRECT_COLOR
