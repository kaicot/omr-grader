from __future__ import annotations

import cv2
import numpy as np
import pytest

from omr_grader.domain.enums import AnswerStatus, CellStatus, KeyQuestionStatus
from omr_grader.domain.errors import Err
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
    MAX_OVERLAY_CELLS,
    NORMAL_COLOR,
    REVIEW_COLOR,
    render_overlay,
    render_scored_overlay,
    render_scored_overlay_scaled,
    scale_overlay_evidence,
)

WHITE = (255, 255, 255)
SIZE, PITCH, LEFT, TOP = 90, 110, 60, 20
RADIUS = round(0.4 * SIZE)  # rings are drawn at 0.4 of the cell size

NORMAL, BLANK = AnswerStatus.NORMAL, AnswerStatus.BLANK
MULTIPLE, UNCERTAIN, UNASKED = AnswerStatus.MULTIPLE, AnswerStatus.UNCERTAIN, AnswerStatus.UNASKED


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


def _unasked_cell(index: int) -> CellEvidence:
    return CellEvidence(index, 1, None, 1, None, None, None, False, CellStatus.BLANK)


def _answer(question: int, selected: tuple[int, ...], status: AnswerStatus) -> AnswerRecognition:
    """Five 90 px cells of one row: rows are 110 px apart, cells 110 px apart."""
    cells = tuple(
        CellEvidence(
            index=(question - 1) * 5 + choice - 1,
            question=question,
            digit=None,
            choice=choice,
            pixel_rect=PixelRect(
                LEFT + (choice - 1) * PITCH, TOP + (question - 1) * PITCH, SIZE, SIZE
            ),
            ratio_rect=RatioRect("0", "0", "0.1", "0.1"),
            fill_score="0.8" if choice in selected else "0",
            selected=choice in selected,
            status=CellStatus.NORMAL if choice in selected else CellStatus.BLANK,
        )
        for choice in range(1, 6)
    )
    return AnswerRecognition(question, AnswerValue(selected, status), cells)


def _key(
    question: int,
    choices: tuple[int, ...] = (),
    kind: KeyQuestionStatus = KeyQuestionStatus.ANSWER,
) -> AnswerKeyEntry:
    if kind is KeyQuestionStatus.ALL:
        return AnswerKeyEntry(question, AnswerValue((), AnswerStatus.ALL), "1", kind)
    if kind is KeyQuestionStatus.UNASKED:
        return AnswerKeyEntry(question, AnswerValue((), AnswerStatus.UNASKED), "0", kind)
    status = NORMAL if len(choices) == 1 else MULTIPLE
    return AnswerKeyEntry(question, AnswerValue(choices, status), "1", kind)


def _canvas(rows: int = 1) -> np.ndarray:
    return np.full((TOP + rows * PITCH, LEFT + 5 * PITCH, 3), 255, dtype=np.uint8)


def _ring(image: np.ndarray, question: int, choice: int) -> tuple[int, ...]:
    """The pixel on the ring of a cell, at its rightmost point."""
    x = LEFT + (choice - 1) * PITCH + SIZE // 2
    y = TOP + (question - 1) * PITCH + SIZE // 2
    return tuple(int(value) for value in image[y, x + RADIUS])


def _slash(image: np.ndarray, question: int) -> tuple[int, ...]:
    """The middle of the red slash over the question number, one cell left of choice 1."""
    x = LEFT + SIZE // 2 - SIZE
    y = TOP + (question - 1) * PITCH + SIZE // 2
    return tuple(int(value) for value in image[y, x])


def _score(
    answers: tuple[AnswerRecognition, ...],
    keys: tuple[AnswerKeyEntry, ...],
    rows: int | None = None,
) -> np.ndarray:
    result = render_scored_overlay(_canvas(rows or len(answers)), (), answers, keys)
    assert not isinstance(result, Err), result
    return result.value


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


def test_cells_without_geometry_are_not_drawn() -> None:
    source = np.full((50, 80, 3), 255, dtype=np.uint8)
    evidence = (_cell(0, CellStatus.NORMAL, True), _unasked_cell(1), _unasked_cell(2))
    with_geometry_only = render_overlay(source, evidence[:1])

    result = render_overlay(source, evidence)

    assert not isinstance(result, Err)
    assert np.array_equal(result.value, with_geometry_only.value)
    only_placeholders = render_overlay(source, evidence[1:])
    assert not isinstance(only_placeholders, Err)
    assert np.array_equal(only_placeholders.value, source)


def test_a_cell_with_only_one_of_its_two_rectangles_is_rejected() -> None:
    source = np.full((50, 80, 3), 255, dtype=np.uint8)
    lopsided = CellEvidence(
        0, 1, None, 1, None, RatioRect("0", "0", "0.1", "0.1"), "0.1", False, CellStatus.BLANK
    )
    other_way = CellEvidence(
        0, 1, None, 1, PixelRect(0, 0, 8, 8), None, "0.1", False, CellStatus.BLANK
    )

    for evidence in ((lopsided,), (other_way,)):
        result = render_overlay(source, evidence)
        assert isinstance(result, Err)
        assert result.errors[0].code == "INVALID_OVERLAY_EVIDENCE"


def test_overlay_rejects_bad_images_evidence_and_rectangles() -> None:
    source = np.full((50, 80, 3), 255, dtype=np.uint8)
    cell = _cell(0, CellStatus.NORMAL, True)
    outside = CellEvidence(
        1, 1, None, 1, PixelRect(70, 10, 16, 16), RatioRect("0", "0", "0.1", "0.1"),
        "0.5", True, CellStatus.NORMAL,
    )  # fmt: skip
    duplicates = (cell, cell)
    too_many = tuple(_unasked_cell(index) for index in range(MAX_OVERLAY_CELLS + 1))

    cases = {
        "INVALID_OVERLAY_IMAGE": (np.zeros((0, 3, 3), np.uint8), (cell,)),
        "INVALID_OVERLAY_EVIDENCE": (source, duplicates),
        "INVALID_OVERLAY_RECT": (source, (outside,)),
    }
    for code, (image, evidence) in cases.items():
        result = render_overlay(image, evidence)
        assert isinstance(result, Err) and result.errors[0].code == code
    for evidence in (too_many, [cell], ("cell",)):  # type: ignore[assignment]
        result = render_overlay(source, evidence)  # type: ignore[arg-type]
        assert isinstance(result, Err) and result.errors[0].code == "INVALID_OVERLAY_EVIDENCE"


def test_overlay_accepts_gray_and_alpha_images_and_returns_color() -> None:
    cell = _cell(0, CellStatus.NORMAL, True)

    for source in (
        np.full((50, 80), 255, dtype=np.uint8),
        np.full((50, 80, 4), 255, dtype=np.uint8),
    ):
        result = render_overlay(source, (cell,))
        assert not isinstance(result, Err)
        assert result.value.shape == (50, 80, 3)
        assert tuple(result.value[10, 0]) == NORMAL_COLOR


def test_a_correct_answer_gets_one_thick_blue_ring_and_nothing_else() -> None:
    answers = (_answer(1, (2,), NORMAL),)

    output = _score(answers, (_key(1, (2,)),))

    assert _ring(output, 1, 2) == CORRECT_COLOR
    assert tuple(output[TOP + SIZE // 2, LEFT + PITCH + SIZE // 2]) == WHITE  # ring only
    for choice in (1, 3, 4, 5):
        assert _ring(output, 1, choice) == WHITE
    assert _slash(output, 1) == WHITE


def test_a_wrong_answer_gets_a_red_ring_a_thin_blue_ring_on_the_key_and_a_slash() -> None:
    answers = (_answer(1, (4,), NORMAL),)

    output = _score(answers, (_key(1, (2,)),))

    assert _ring(output, 1, 4) == INCORRECT_COLOR
    assert _ring(output, 1, 2) == CORRECT_COLOR
    assert _slash(output, 1) == INCORRECT_COLOR
    for choice in (1, 3, 5):
        assert _ring(output, 1, choice) == WHITE
    # The key ring is the thin one: one fifth of the thick ring's width at this cell size.
    row = TOP + SIZE // 2
    thick = np.count_nonzero(output[row, LEFT + 3 * PITCH + SIZE // 2 :][:, 2] == 255)
    thin_zone = output[
        row, LEFT + PITCH + SIZE // 2 + RADIUS - 6 : LEFT + PITCH + SIZE // 2 + RADIUS + 7
    ]
    assert thick > 0
    assert 3 <= int(np.count_nonzero(np.all(thin_zone == CORRECT_COLOR, axis=1))) <= 5


def test_a_blank_answer_is_wrong_when_the_key_has_an_answer() -> None:
    output = _score((_answer(1, (), BLANK),), (_key(1, (3,)),))

    assert _ring(output, 1, 3) == CORRECT_COLOR
    assert _slash(output, 1) == INCORRECT_COLOR
    for choice in (1, 2, 4, 5):
        assert _ring(output, 1, choice) == WHITE


def test_every_wrong_choice_of_a_multiple_mark_is_ringed_in_red() -> None:
    output = _score((_answer(1, (1, 4), MULTIPLE),), (_key(1, (3,)),))

    assert _ring(output, 1, 1) == INCORRECT_COLOR
    assert _ring(output, 1, 4) == INCORRECT_COLOR
    assert _ring(output, 1, 3) == CORRECT_COLOR
    assert _slash(output, 1) == INCORRECT_COLOR


def test_a_multiple_answer_key_is_correct_only_for_exactly_those_choices() -> None:
    both = _score((_answer(1, (2, 4), MULTIPLE),), (_key(1, (2, 4)),))
    partial = _score((_answer(1, (2,), NORMAL),), (_key(1, (2, 4)),))

    assert _ring(both, 1, 2) == CORRECT_COLOR and _ring(both, 1, 4) == CORRECT_COLOR
    assert _slash(both, 1) == WHITE
    assert _ring(partial, 1, 4) == CORRECT_COLOR  # the key ring that was missed
    assert _slash(partial, 1) == INCORRECT_COLOR


def test_rows_under_review_get_orange_rings_and_the_key_but_no_slash() -> None:
    answers = (_answer(1, (2, 5), UNCERTAIN), _answer(2, (), UNCERTAIN))

    output = _score(answers, (_key(1, (3,)), _key(2, (4,))))

    assert _ring(output, 1, 2) == REVIEW_COLOR
    assert _ring(output, 1, 5) == REVIEW_COLOR
    assert _ring(output, 1, 3) == CORRECT_COLOR
    assert _slash(output, 1) == WHITE
    # Nothing seen on the second row: only the key's thin ring shows where the answer lies.
    assert _ring(output, 2, 4) == CORRECT_COLOR
    assert [_ring(output, 2, choice) for choice in (1, 2, 3, 5)] == [WHITE] * 4
    assert _slash(output, 2) == WHITE


def test_a_key_that_accepts_any_answer_counts_every_selection_as_correct() -> None:
    answers = (
        _answer(1, (3,), NORMAL),
        _answer(2, (1, 4), MULTIPLE),
        _answer(3, (), BLANK),
        _answer(4, (2,), UNCERTAIN),
    )
    keys = tuple(_key(question, kind=KeyQuestionStatus.ALL) for question in (1, 2, 3, 4))

    output = _score(answers, keys)

    assert _ring(output, 1, 3) == CORRECT_COLOR
    assert _ring(output, 2, 1) == CORRECT_COLOR and _ring(output, 2, 4) == CORRECT_COLOR
    assert all(_slash(output, question) == WHITE for question in (1, 2, 3))
    assert [_ring(output, 3, choice) for choice in range(1, 6)] == [WHITE] * 5  # nothing to show
    assert _ring(output, 4, 2) == REVIEW_COLOR  # still waiting for a person


def test_unasked_questions_and_unkeyed_questions_are_left_clean() -> None:
    answers = (
        _answer(1, (2,), NORMAL),  # the key does not ask this question
        _answer(2, (), UNASKED),  # the form does not print this question
        _answer(3, (4,), NORMAL),  # not in the key at all
    )
    keys = (_key(1, kind=KeyQuestionStatus.UNASKED), _key(2, (1,)))

    output = _score(answers, keys)

    assert np.array_equal(output, _canvas(3))


def test_rows_whose_cells_have_no_geometry_are_skipped() -> None:
    answer = _answer(1, (2,), NORMAL)
    no_geometry = AnswerRecognition(
        1,
        answer.value,
        tuple(
            CellEvidence(c.index, 1, None, c.choice, None, None, "0.8", c.selected, c.status)
            for c in answer.cells
        ),
    )

    output = _score((no_geometry,), (_key(1, (3,)),))

    assert np.array_equal(output, _canvas())


def test_scored_overlay_ignores_its_evidence_argument_and_keeps_the_source_untouched() -> None:
    source = _canvas(2)
    original = source.copy()
    answers = (_answer(1, (2,), NORMAL), _answer(2, (1,), NORMAL))
    keys = (_key(1, (2,)), _key(2, (5,)))
    evidence = tuple(cell for answer in answers for cell in answer.cells)

    with_evidence = render_scored_overlay(source, evidence, answers, keys)
    without = render_scored_overlay(source, (), answers, keys)

    assert not isinstance(with_evidence, Err) and not isinstance(without, Err)
    assert np.array_equal(with_evidence.value, without.value)
    assert np.array_equal(source, original)
    assert not np.array_equal(with_evidence.value, source)
    # No cell boxes or labels: only rings and the slash changed pixels.
    changed = np.any(with_evidence.value != source, axis=2)
    assert changed[TOP : TOP + SIZE, LEFT : LEFT + 5 * PITCH].sum() > 0
    assert not changed[:, LEFT + 2 * PITCH + SIZE // 2 - 5 : LEFT + 2 * PITCH + SIZE // 2 + 5].any()


def test_scored_overlay_accepts_gray_and_alpha_images_and_rejects_bad_input() -> None:
    answers = (_answer(1, (2,), NORMAL),)
    keys = (_key(1, (2,)),)
    gray = np.full(_canvas().shape[:2], 255, dtype=np.uint8)
    alpha = np.full((*gray.shape, 4), 255, dtype=np.uint8)

    for source in (gray, alpha):
        result = render_scored_overlay(source, (), answers, keys)
        assert not isinstance(result, Err)
        assert result.value.shape == (*gray.shape, 3)
        assert _ring(result.value, 1, 2) == CORRECT_COLOR
    for image in (np.zeros((0, 4, 3), np.uint8), np.zeros((4, 4, 2), np.uint8), "page"):
        result = render_scored_overlay(image, (), answers, keys)  # type: ignore[arg-type]
        assert isinstance(result, Err) and result.errors[0].code == "INVALID_OVERLAY_IMAGE"
    for bad_answers, bad_keys in (([answers[0]], keys), (answers, [keys[0]]), (("x",), keys)):
        result = render_scored_overlay(_canvas(), (), bad_answers, bad_keys)  # type: ignore[arg-type]
        assert isinstance(result, Err) and result.errors[0].code == "INVALID_OVERLAY_EVIDENCE"


def test_rings_stay_visible_at_the_size_of_a_profile_frame_cell() -> None:
    # A real frame cell is about 44 x 40 px: rings of radius 16 with a one pixel key ring.
    cells = tuple(
        CellEvidence(
            choice - 1, 1, None, choice, PixelRect(10 + (choice - 1) * 44, 10, 44, 40),
            RatioRect("0", "0", "0.1", "0.1"), "0.8", choice == 4, CellStatus.NORMAL if choice == 4 else CellStatus.BLANK,
        )
        for choice in range(1, 6)
    )  # fmt: skip
    answer = AnswerRecognition(1, AnswerValue((4,), NORMAL), cells)
    canvas = np.full((60, 240, 3), 255, dtype=np.uint8)

    wrong = render_scored_overlay(canvas, (), (answer,), (_key(1, (2,)),))
    right = render_scored_overlay(canvas, (), (answer,), (_key(1, (4,)),))

    assert not isinstance(wrong, Err) and not isinstance(right, Err)
    radius = round(0.4 * 40)
    assert tuple(wrong.value[30, 10 + 3 * 44 + 22 + radius]) == INCORRECT_COLOR
    assert tuple(right.value[30, 10 + 3 * 44 + 22 + radius]) == CORRECT_COLOR
    key_ring = wrong.value[30, 10 + 44 + 22 + radius]
    assert key_ring[0] > 200 and key_ring[1] < 140 and key_ring[2] < 140  # thin, blue-dominant
    assert tuple(wrong.value[30, 10 + 44 + 22]) == WHITE


def test_scaled_overlay_redraws_the_rings_at_the_target_size() -> None:
    source = _canvas(2)
    original = source.copy()
    answers = (_answer(1, (2,), NORMAL), _answer(2, (4,), NORMAL))
    keys = (_key(1, (2,)), _key(2, (1,)))
    evidence = tuple(cell for answer in answers for cell in answer.cells)
    target = (source.shape[1] // 2, source.shape[0] // 2)

    result = render_scored_overlay_scaled(source, evidence, answers, keys, target)

    assert not isinstance(result, Err)
    output = result.value
    assert output.shape == (target[1], target[0], 3)
    assert np.array_equal(source, original)
    radius = round(0.4 * (SIZE // 2))
    for question, choice, color in ((1, 2, CORRECT_COLOR), (2, 4, INCORRECT_COLOR)):
        x = (LEFT + (choice - 1) * PITCH + SIZE // 2) // 2
        y = (TOP + (question - 1) * PITCH + SIZE // 2) // 2
        assert tuple(output[y, x + radius]) == color
        assert tuple(output[y, x]) == WHITE
    assert (np.any(output != 255, axis=2)).sum() > 0


def test_scaled_overlay_rejects_bad_sizes_and_images() -> None:
    answers = (_answer(1, (2,), NORMAL),)
    for target in ((0, 10), (10, -1), (1.5, 10), (10,), (10, 10, 3), [10, 10]):
        result = render_scored_overlay_scaled(_canvas(), (), answers, (), target)  # type: ignore[arg-type]
        assert isinstance(result, Err)
    result = render_scored_overlay_scaled(np.zeros((0, 0, 3), np.uint8), (), answers, (), (10, 10))
    assert isinstance(result, Err) and result.errors[0].code == "INVALID_OVERLAY_IMAGE"


def test_scaling_evidence_maps_rectangles_and_leaves_unplaced_cells_alone() -> None:
    cell = CellEvidence(
        0, 1, None, 1, PixelRect(20, 30, 40, 20), RatioRect("0.1", "0.3", "0.2", "0.2"),
        "0.8", True, CellStatus.NORMAL,
    )  # fmt: skip

    half, doubled, clipped = (
        scale_overlay_evidence((cell,), (200, 100), size)
        for size in ((100, 50), (400, 200), (30, 20))
    )
    placeholder = scale_overlay_evidence((_unasked_cell(1),), (200, 100), (100, 50))

    assert half[0].pixel_rect == PixelRect(10, 15, 20, 10)
    assert doubled[0].pixel_rect == PixelRect(40, 60, 80, 40)
    rect = clipped[0].pixel_rect
    assert rect is not None and rect.x + rect.w <= 30 and rect.y + rect.h <= 20 and rect.w >= 1
    assert half[0].ratio_rect == cell.ratio_rect and half[0].status is cell.status
    assert placeholder == (_unasked_cell(1),)
    for bad in ((0, 100), (200, -1), (200.0, 100)):
        with pytest.raises(ValueError, match="positive integers"):
            scale_overlay_evidence((cell,), bad, (100, 50))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive integers"):
        scale_overlay_evidence((cell,), (200, 100), (100, 0))


def test_overlay_colors_are_distinct_bgr_triples() -> None:
    colors = {NORMAL_COLOR, BLANK_COLOR, REVIEW_COLOR, CORRECT_COLOR, INCORRECT_COLOR}

    assert len(colors) == 5
    assert CORRECT_COLOR == (255, 0, 0)  # blue in BGR
    assert INCORRECT_COLOR == (0, 0, 255)  # red in BGR
    assert REVIEW_COLOR[2] > REVIEW_COLOR[1] > REVIEW_COLOR[0]  # orange
    orange = np.array([[REVIEW_COLOR]], dtype=np.uint8)
    assert cv2.cvtColor(orange, cv2.COLOR_BGR2HSV)[0, 0, 0] < 25  # hue near red, not green
