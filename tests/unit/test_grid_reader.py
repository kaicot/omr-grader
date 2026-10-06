"""The v4 grid reader, tested on pages drawn directly in a profile frame.

Every fixture page has printed rings with digits (ID digits 0-9 down a column, choices 1-5
across a row), so the reader has to look past the print. Mark sizes are chosen from the
measured fill of a disk of that radius: a 9 px disk fills about 0.67 of the measured disk
(a clear mark), 4.5 px about 0.19 (the gray zone between the blank ceiling 0.11 and the
mark threshold 0.24) and 2.5 px about 0.07 (a speck). A 4.85 px disk (about 0.21) lies between
the mark thresholds of sensitivity 10 (0.18) and sensitivity 5 (0.24).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cache

import cv2
import numpy as np
import pytest

from omr_grader.domain.enums import AnswerStatus, CellStatus, FieldStatus, StudentIdStatus
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.profile import Profile, parse_profile_bytes
from omr_grader.recognition.form_alignment import profile_nodes
from omr_grader.recognition.grid_reader import (
    GridRecognition,
    question_count,
    read_grid,
)
from tests.helpers.omr_engine import make_thresholds
from tests.helpers.synthetic_omr import apply_shading, paint_mark, paint_ring

RING = 16.0
PITCH = (44.0, 40.0)
MARGIN = 60.0
CLEAR, EDGE, GRAY, SPECK = 9.0, 4.85, 4.5, 2.5

Marks = int | Mapping[int, float]
NORMAL, BLANK = AnswerStatus.NORMAL, AnswerStatus.BLANK
MULTIPLE, UNCERTAIN, UNASKED = AnswerStatus.MULTIPLE, AnswerStatus.UNCERTAIN, AnswerStatus.UNASKED

# One row of every kind, then blank rows: the reader judges a row against all the others.
STANDARD: dict[int, Marks] = {
    1: 3,  # one clear mark
    2: {1: CLEAR, 4: CLEAR},  # two clear marks
    3: {2: CLEAR, 5: GRAY},  # a clear mark with a rival in the gray zone
    4: {5: GRAY},  # only a gray-zone mark
    5: {2: CLEAR, 3: SPECK},  # a clear mark with a rival below the blank ceiling
    6: {4: SPECK},  # only a speck
    7: {1: CLEAR, 3: CLEAR, 5: CLEAR},
}


@dataclass(frozen=True, slots=True)
class Frame:
    """A profile frame with printed rings (no marks) and the center of every bubble."""

    profile: Profile
    image: np.ndarray
    id_nodes: Mapping[tuple[int, int], tuple[float, float]]
    answer_nodes: Mapping[tuple[int, int], tuple[float, float]]


@cache
def _frame(
    layout: tuple[int, ...] = (10, 10, 10, 10, 10),
    digits: tuple[int, ...] | None = (1, 2, 3, 4, 5),
    print_gray: int = 110,
) -> Frame:
    """ID grid at the left and one answer block per entry of ``layout``; 50 questions by default."""
    width = round(MARGIN + 8 * PITCH[0] + MARGIN + len(layout) * (5 * PITCH[0] + MARGIN))
    height = round(2 * MARGIN + max(10, max(layout)) * PITCH[1])
    regions: list[dict[str, object]] = [
        {
            "name": "id",
            "type": "id",
            "bbox_ratio": {
                "x": MARGIN / width,
                "y": MARGIN / height,
                "w": 8 * PITCH[0] / width,
                "h": 10 * PITCH[1] / height,
            },
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    left, start = MARGIN + 8 * PITCH[0] + MARGIN, 1
    for rows in layout:
        regions.append(
            {
                "name": f"q{start:03d}",
                "type": "answer",
                "question_start": start,
                "bbox_ratio": {
                    "x": left / width,
                    "y": MARGIN / height,
                    "w": 5 * PITCH[0] / width,
                    "h": rows * PITCH[1] / height,
                },
                "grid": {"cols": 5, "rows": rows},
            }
        )
        left += 5 * PITCH[0] + MARGIN
        start += rows
    page = {"orientation": "landscape", "aspect_ratio": width / height}
    page.update(source_width=width, source_height=height)
    wire = {"schema_version": 1, "profile_name": "frame", "page": page, "regions": regions}
    parsed = parse_profile_bytes(json.dumps(wire).encode())
    assert isinstance(parsed, Ok)
    profile = parsed.value
    nodes, ordered = profile_nodes(profile)
    image = np.full((height, width), 245, dtype=np.uint8)
    id_nodes: dict[tuple[int, int], tuple[float, float]] = {}
    answer_nodes: dict[tuple[int, int], tuple[float, float]] = {}
    cursor = 0
    for region in ordered:
        for row in range(region.grid.rows):
            for column in range(region.grid.cols):
                center = (float(nodes[cursor, 0]), float(nodes[cursor, 1]))
                cursor += 1
                if region.kind == "id":
                    paint_ring(image, center, RING, print_gray, digit=row)
                    id_nodes[(column, row)] = center
                else:
                    shown = None if digits is None else digits[column]
                    paint_ring(image, center, RING, print_gray, digit=shown)
                    answer_nodes[(int(region.question_start or 0) + row, column + 1)] = center
    image.setflags(write=False)
    return Frame(profile, image, id_nodes, answer_nodes)


def _page(
    frame: Frame,
    answers: Mapping[int, Marks] | None = None,
    student_id: str = "20250001",
    id_radius: float = CLEAR,
) -> np.ndarray:
    page = frame.image.copy()
    for column, character in enumerate(student_id):
        if character.isdigit():
            paint_mark(page, frame.id_nodes[(column, int(character))], id_radius)
    for question, marks in (answers or {}).items():
        for choice, radius in ({marks: CLEAR} if isinstance(marks, int) else marks).items():
            paint_mark(page, frame.answer_nodes[(question, choice)], radius)
    return page


def _read(
    page: np.ndarray,
    frame: Frame | None = None,
    *,
    sensitivity: int = 5,
    radius: float = RING,
    **options: object,
) -> GridRecognition:
    result = read_grid(
        page,
        (frame or _frame()).profile,
        make_thresholds(sensitivity),
        bubble_radius=radius,
        **options,  # type: ignore[arg-type]
    )
    assert isinstance(result, Ok), result
    return result.value


def _states(
    reading: GridRecognition, questions: Sequence[int]
) -> list[tuple[AnswerStatus, tuple[int, ...]]]:
    return [
        (reading.answers[q - 1].value.status, reading.answers[q - 1].value.choices)
        for q in questions
    ]


def test_every_decision_state_is_reached_and_unmarked_rows_are_blank() -> None:
    frame = _frame()

    reading = _read(_page(frame, STANDARD), frame)

    assert _states(reading, range(1, 9)) == [
        (NORMAL, (3,)),
        (MULTIPLE, (1, 4)),
        (UNCERTAIN, (2, 5)),
        (UNCERTAIN, (5,)),
        (NORMAL, (2,)),
        (BLANK, ()),
        (MULTIPLE, (1, 3, 5)),
        (BLANK, ()),
    ]
    assert {answer.value.status for answer in reading.answers[7:50]} == {BLANK}
    assert reading.student_id.status is StudentIdStatus.NORMAL
    assert reading.student_id.value == "20250001"
    assert reading.needs_manual_review  # rows 3 and 4 wait for a person


def test_cells_of_a_decided_row_carry_their_status_and_selection() -> None:
    frame = _frame()
    reading = _read(_page(frame, STANDARD), frame)

    single, double, mixed, speck = (reading.answers[q - 1] for q in (1, 2, 3, 6))

    assert [cell.selected for cell in single.cells] == [False, False, True, False, False]
    assert [cell.status for cell in single.cells] == [
        CellStatus.BLANK,
        CellStatus.BLANK,
        CellStatus.NORMAL,
        CellStatus.BLANK,
        CellStatus.BLANK,
    ]
    assert [cell.selected for cell in double.cells] == [True, False, False, True, False]
    assert {double.cells[0].status, double.cells[3].status} == {CellStatus.MULTIPLE}
    # In a row under review every cell is shown as uncertain; the plausible ones are selected.
    assert {cell.status for cell in mixed.cells} == {CellStatus.UNCERTAIN}
    assert [cell.selected for cell in mixed.cells] == [False, True, False, False, True]
    assert not any(cell.selected for cell in speck.cells)


def test_scores_are_the_ink_above_the_printed_digit_and_stay_tiny_on_unmarked_cells() -> None:
    frame = _frame()
    reading = _read(_page(frame, STANDARD), frame)
    drawn = {
        (question, choice)
        for question, marks in STANDARD.items()
        for choice in ({marks} if isinstance(marks, int) else marks)
    }
    scores = {
        (answer.question, cell.choice): float(cell.fill_score or "nan")
        for answer in reading.answers[:50]
        for cell in answer.cells
    }

    assert scores[(1, 3)] == pytest.approx(0.67, abs=0.05)
    assert scores[(3, 5)] == pytest.approx(0.19, abs=0.05)
    assert scores[(6, 4)] == pytest.approx(0.07, abs=0.04)
    # Every other cell shows only its printed digit, which the unmarked level cancels out.
    assert max(score for cell, score in scores.items() if cell not in drawn) < 0.02


def test_an_untouched_page_has_no_ink_above_its_printed_digits() -> None:
    frame = _frame()

    reading = _read(frame.image, frame)

    answers = [
        float(cell.fill_score or "nan") for item in reading.answers[:50] for cell in item.cells
    ]
    digits = [
        float(cell.fill_score or "nan")
        for column in reading.student_id.cells
        for cell in column.candidates
    ]
    assert max(answers) < 0.02
    assert max(digits) < 0.05  # a digit that is never the strongest has no baseline row
    assert {answer.value.status for answer in reading.answers[:50]} == {BLANK}
    assert reading.student_id.status is StudentIdStatus.INVALID
    assert reading.needs_manual_review


@pytest.mark.parametrize("print_gray", (60, 20), ids=("dark-print", "black-print"))
def test_dark_print_is_not_mistaken_for_marks(print_gray: int) -> None:
    frame = _frame(print_gray=print_gray)

    blank = _read(frame.image, frame)
    marked = _read(_page(frame, STANDARD), frame)

    assert {answer.value.status for answer in blank.answers[:50]} == {BLANK}
    assert (
        max(float(cell.fill_score or "nan") for a in blank.answers[:50] for cell in a.cells) < 0.06
    )
    assert _states(marked, range(1, 9)) == _states(
        _read(_page(_frame(), STANDARD), _frame()), range(1, 9)
    )
    assert marked.student_id.value == "20250001"


def test_evidence_lists_the_id_grid_then_every_question_in_order() -> None:
    frame = _frame()
    reading = _read(_page(frame, STANDARD), frame)

    assert [cell.index for cell in reading.evidence] == list(range(580))
    assert [cell.digit for cell in reading.evidence[:80]] == [
        d for _ in range(8) for d in range(10)
    ]
    assert all(cell.question is None and cell.choice is None for cell in reading.evidence[:80])
    assert [(c.question, c.choice) for c in reading.evidence[80:85]] == [
        (1, n) for n in range(1, 6)
    ]
    assert [c.question for c in reading.evidence[80:330:5]] == list(range(1, 51))
    assert [answer.question for answer in reading.answers] == list(range(1, 101))
    from_answers = tuple(cell for answer in reading.answers for cell in answer.cells)
    from_id = tuple(cell for column in reading.student_id.cells for cell in column.candidates)
    assert reading.evidence == from_id + from_answers


def test_cell_rectangles_surround_the_bubbles_they_describe() -> None:
    frame = _frame()
    reading = _read(_page(frame, STANDARD), frame)

    for question, choice in ((1, 1), (1, 5), (25, 3), (50, 5)):
        cell = reading.answers[question - 1].cells[choice - 1]
        assert cell.pixel_rect is not None and cell.ratio_rect is not None
        x, y = frame.answer_nodes[(question, choice)]
        rect = cell.pixel_rect
        assert (rect.w, rect.h) == (round(PITCH[0]), round(PITCH[1]))
        assert abs(rect.x + rect.w / 2 - x) <= 1 and abs(rect.y + rect.h / 2 - y) <= 1
    first = reading.student_id.cells[0].candidates[0]
    assert first.pixel_rect is not None
    assert abs(first.pixel_rect.x + first.pixel_rect.w / 2 - frame.id_nodes[(0, 0)][0]) <= 1


def test_questions_the_form_does_not_print_are_unasked_placeholders() -> None:
    frame = _frame()
    reading = _read(_page(frame, STANDARD), frame)

    assert question_count(frame.profile) == 50
    unasked = reading.answers[50:]
    assert [answer.question for answer in unasked] == list(range(51, 101))
    assert {(a.value.status, a.value.choices) for a in unasked} == {(UNASKED, ())}
    for answer in unasked:
        assert [cell.choice for cell in answer.cells] == [1, 2, 3, 4, 5]
        assert all(cell.question == answer.question for cell in answer.cells)
        assert all(cell.pixel_rect is None and cell.ratio_rect is None for cell in answer.cells)
        assert all(cell.fill_score is None and not cell.selected for cell in answer.cells)
        assert {cell.status for cell in answer.cells} == {CellStatus.BLANK}
    assert [cell.index for cell in reading.evidence[330:]] == list(range(330, 580))
    # Unasked rows never ask for a review by themselves.
    assert not _read(_page(frame, {1: 2}), frame).needs_manual_review


def test_a_form_that_prints_all_hundred_questions_has_no_placeholders() -> None:
    frame = _frame((20,) * 5)

    reading = _read(_page(frame, {1: 1, 100: 5, 57: 3}), frame)

    assert question_count(frame.profile) == 100
    assert UNASKED not in {answer.value.status for answer in reading.answers}
    assert _states(reading, (1, 57, 100, 2)) == [
        (NORMAL, (1,)),
        (NORMAL, (3,)),
        (NORMAL, (5,)),
        (BLANK, ()),
    ]
    assert all(cell.pixel_rect is not None for answer in reading.answers for cell in answer.cells)
    assert not reading.needs_manual_review


@pytest.mark.parametrize(
    ("mark", "status"),
    ((SPECK, BLANK), (GRAY, UNCERTAIN), (CLEAR, NORMAL)),
    ids=("speck-is-blank", "gray-zone-is-uncertain", "clear-mark-is-normal"),
)
def test_a_lone_mark_is_judged_by_its_ink(mark: float, status: AnswerStatus) -> None:
    frame = _frame()

    reading = _read(_page(frame, {4: {2: mark}}), frame)

    answer = reading.answers[3]
    assert answer.value.status is status
    assert answer.value.choices == ((2,) if status is not BLANK else ())


def test_light_pencil_counts_in_proportion_to_its_darkness() -> None:
    frame = _frame()
    page = _page(frame)
    for question, value in ((1, 150), (2, 205), (3, 230)):
        paint_mark(page, frame.answer_nodes[(question, 4)], 11.0, value)

    reading = _read(page, frame)

    assert _states(reading, (1, 2, 3)) == [(NORMAL, (4,)), (UNCERTAIN, (4,)), (BLANK, ())]
    shades = [float(reading.answers[q - 1].cells[3].fill_score or "nan") for q in (1, 2, 3)]
    assert shades[0] > 0.3 > shades[1] > 0.11 > shades[2]


def test_a_higher_sensitivity_accepts_a_fainter_mark_but_a_speck_never_counts() -> None:
    frame = _frame()
    page = _page(frame, {1: {1: EDGE}, 2: {3: SPECK}, 3: 4})

    states = {
        level: _states(_read(page, frame, sensitivity=level), (1, 2, 3)) for level in (1, 5, 10)
    }

    assert states[5][0] == (UNCERTAIN, (1,))  # fill 0.21 is below the 0.24 threshold
    assert states[1][0] == (UNCERTAIN, (1,))
    assert states[10][0] == (NORMAL, (1,))  # threshold 0.18
    assert [row[1] for row in states.values()] == [(BLANK, ())] * 3
    assert [row[2] for row in states.values()] == [(NORMAL, (4,))] * 3


def test_bubble_radius_is_only_a_starting_point_for_measuring_the_ring() -> None:
    frame = _frame()
    page = _page(frame, STANDARD)
    expected = _states(_read(page, frame), range(1, 9))

    for guess in (12.0, 20.0):
        assert _states(_read(page, frame, radius=guess), range(1, 9)) == expected


def test_uneven_scanner_light_does_not_change_the_reading() -> None:
    frame = _frame()
    page = _page(frame, STANDARD)
    reference = _read(page, frame)

    shaded = _read(apply_shading(page, 0.55), frame)

    assert _states(shaded, range(1, 51)) == _states(reference, range(1, 51))
    assert shaded.student_id.value == reference.student_id.value == "20250001"


@pytest.mark.parametrize("tone", (0, 24, 255), ids=("black", "dark", "white"))
def test_a_page_of_a_single_tone_reads_as_blank(tone: int) -> None:
    frame = _frame()
    page = np.full(frame.image.shape, tone, dtype=np.uint8)

    reading = _read(page, frame)

    assert {answer.value.status for answer in reading.answers[:50]} == {BLANK}
    assert reading.student_id.status is StudentIdStatus.INVALID


def test_a_page_that_fades_across_its_width_reads_as_blank() -> None:
    frame = _frame()
    shape = frame.image.shape
    page = np.tile(np.linspace(80, 240, shape[1]).astype(np.uint8), (shape[0], 1))

    reading = _read(page, frame)

    assert {answer.value.status for answer in reading.answers[:50]} == {BLANK}


@pytest.mark.parametrize(
    "convert",
    (
        lambda page: cv2.cvtColor(page, cv2.COLOR_GRAY2BGR),
        lambda page: cv2.cvtColor(page, cv2.COLOR_GRAY2BGRA),
        lambda page: page.astype(np.float32),
    ),
    ids=("bgr", "bgra", "float"),
)
def test_color_alpha_and_float_pages_read_like_gray_ones(convert: object) -> None:
    frame = _frame()
    page = _page(frame, STANDARD)

    reading = _read(convert(page), frame)  # type: ignore[operator]

    assert _states(reading, range(1, 9)) == _states(_read(page, frame), range(1, 9))
    assert reading.student_id.value == "20250001"


def test_a_complete_id_is_read_digit_by_digit() -> None:
    frame = _frame()

    reading = _read(_page(frame, STANDARD, "20250001"), frame)

    ids = reading.student_id
    assert (ids.status, ids.value) == (StudentIdStatus.NORMAL, "20250001")
    assert [cell.selected_digit for cell in ids.cells] == list("20250001")
    assert {cell.status for cell in ids.cells} == {FieldStatus.NORMAL}
    assert [item.digit for item in ids.cells[3].candidates if item.selected] == [5]


@pytest.mark.parametrize("digits", ("2025000", "20", "2"))
def test_blank_columns_after_the_digits_are_allowed(digits: str) -> None:
    frame = _frame()

    ids = _read(_page(frame, STANDARD, digits), frame).student_id

    assert (ids.status, ids.value) == (StudentIdStatus.NORMAL, digits)
    assert [cell.status for cell in ids.cells[len(digits) :]] == [FieldStatus.BLANK] * (
        8 - len(digits)
    )
    assert len(ids.cells) == 8


@pytest.mark.parametrize("digits", ("2025 001", " 2025001", "20 50001", "  250001"))
def test_a_blank_column_before_a_digit_makes_the_id_invalid(digits: str) -> None:
    frame = _frame()

    reading = _read(_page(frame, STANDARD, digits), frame)

    ids = reading.student_id
    assert ids.status is StudentIdStatus.INVALID
    assert ids.value is None
    for column, character in enumerate(digits):
        expected = FieldStatus.BLANK if character == " " else FieldStatus.NORMAL
        assert ids.cells[column].status is expected
    assert reading.needs_manual_review


def test_an_id_with_a_trailing_blank_after_a_full_prefix_is_still_valid() -> None:
    frame = _frame()

    ids = _read(_page(frame, STANDARD, "2025000 "), frame).student_id

    assert (ids.status, ids.value) == (StudentIdStatus.NORMAL, "2025000")


def test_an_id_column_with_two_marks_or_a_doubtful_mark_is_invalid() -> None:
    frame = _frame()
    doubled = _page(frame, STANDARD, "2025000")
    paint_mark(doubled, frame.id_nodes[(7, 4)], CLEAR)
    paint_mark(doubled, frame.id_nodes[(7, 6)], CLEAR)
    doubtful = _page(frame, STANDARD, "2025000")
    paint_mark(doubtful, frame.id_nodes[(7, 3)], GRAY)

    two = _read(doubled, frame).student_id
    gray = _read(doubtful, frame).student_id

    assert two.status is StudentIdStatus.INVALID and two.value is None
    assert two.cells[7].status is FieldStatus.MULTIPLE
    assert two.cells[7].selected_digit is None
    assert gray.status is StudentIdStatus.INVALID and gray.value is None
    assert gray.cells[7].status is FieldStatus.UNCERTAIN
    assert [item.digit for item in gray.cells[7].candidates if item.selected] == [3]


def test_a_page_without_an_id_needs_review_but_its_answers_are_still_read() -> None:
    frame = _frame()

    reading = _read(_page(frame, STANDARD, ""), frame)

    assert reading.student_id.status is StudentIdStatus.INVALID
    assert {cell.status for cell in reading.student_id.cells} == {FieldStatus.BLANK}
    assert reading.answers[0].value.status is NORMAL
    assert reading.needs_manual_review


@pytest.mark.parametrize(
    "provenance",
    (None, "local-background-v2"),
    ids=("uncalibrated", "older-calibration"),
)
def test_thresholds_without_this_calibration_confirm_nothing(provenance: str | None) -> None:
    frame = _frame()
    page = _page(frame, STANDARD)
    thresholds = make_thresholds(5, calibrated=provenance is not None)
    if provenance is not None:
        thresholds = replace(thresholds, calibration_provenance=provenance)

    result = read_grid(page, frame.profile, thresholds, bubble_radius=RING)

    assert isinstance(result, Ok)
    reading = result.value
    printed = reading.answers[:50]
    assert {answer.value.status for answer in printed} == {UNCERTAIN}
    assert printed[0].value.choices == (3,)  # what was seen is kept as review evidence
    assert printed[1].value.choices == (1, 4)
    assert printed[7].value.choices == ()
    assert reading.student_id.value is None
    assert reading.student_id.status is StudentIdStatus.INVALID
    assert reading.needs_manual_review
    assert {answer.value.status for answer in reading.answers[50:]} == {UNASKED}


def test_an_untrusted_alignment_withholds_every_value_but_keeps_the_evidence() -> None:
    frame = _frame()
    page = _page(frame, STANDARD)
    trusted = _read(page, frame)

    withheld = _read(page, frame, trusted=False)

    printed = zip(trusted.answers[:50], withheld.answers[:50], strict=True)
    for before, after in printed:
        assert after.value.status is UNCERTAIN
        assert after.value.choices == before.value.choices
        assert [cell.selected for cell in after.cells] == [cell.selected for cell in before.cells]
        assert {cell.status for cell in after.cells} == {CellStatus.UNCERTAIN}
        assert [cell.fill_score for cell in after.cells] == [
            cell.fill_score for cell in before.cells
        ]
    assert [a.value.status for a in withheld.answers[50:]] == [UNASKED] * 50
    assert withheld.answers[50:] == trusted.answers[50:]
    assert withheld.student_id.status is StudentIdStatus.INVALID
    assert withheld.student_id.value is None
    assert {cell.status for cell in withheld.student_id.cells} == {FieldStatus.UNCERTAIN}
    assert all(cell.selected_digit is None for cell in withheld.student_id.cells)
    assert withheld.needs_manual_review
    selected = [(c.digit, c.selected) for col in withheld.student_id.cells for c in col.candidates]
    expected = [(c.digit, c.selected) for col in trusted.student_id.cells for c in col.candidates]
    assert selected == expected


def test_a_page_read_upside_down_is_withheld() -> None:
    # Printed (1) is the lightest digit; a form whose first column prints the heavy (5)
    # is being read from the wrong end, so nothing it shows may be trusted.
    frame = _frame(digits=(5, 4, 3, 2, 1))
    page = _page(frame, STANDARD)

    reading = _read(page, frame)

    assert reading.needs_manual_review
    printed = reading.answers[:50]
    assert {answer.value.status for answer in printed} == {UNCERTAIN}
    assert printed[0].value.choices == (3,)
    assert printed[1].value.choices == (1, 4)
    assert reading.student_id.status is StudentIdStatus.INVALID
    assert reading.student_id.value is None
    assert {answer.value.status for answer in reading.answers[50:]} == {UNASKED}
    assert not any(answer.value.status in {NORMAL, BLANK, MULTIPLE} for answer in reading.answers)


def test_a_form_printed_without_digits_is_not_mistaken_for_upside_down() -> None:
    frame = _frame(digits=None)

    reading = _read(_page(frame, STANDARD), frame)

    assert _states(reading, (1, 2, 6)) == [(NORMAL, (3,)), (MULTIPLE, (1, 4)), (BLANK, ())]
    assert reading.student_id.value == "20250001"


def test_invalid_images_thresholds_radii_and_profiles_are_typed_errors() -> None:
    frame = _frame()
    page = frame.image
    good = make_thresholds()
    empty_profile = replace(frame.profile, regions=frame.profile.regions[:1])
    bent_thresholds = replace(good, blank_ceiling=0.5)

    def failure(*arguments: object, **options: object) -> str:
        result = read_grid(*arguments, **options)  # type: ignore[arg-type]
        assert isinstance(result, Err)
        return result.errors[0].code

    assert failure("page", frame.profile, good, bubble_radius=RING) == "INVALID_NORMALIZED_IMAGE"
    assert failure(page[:0], frame.profile, good, bubble_radius=RING) == "INVALID_NORMALIZED_IMAGE"
    assert failure(page[..., None].repeat(2, 2), frame.profile, good, bubble_radius=RING) == (
        "INVALID_NORMALIZED_IMAGE"
    )
    assert failure(page, frame.profile, bent_thresholds, bubble_radius=RING) == "INVALID_THRESHOLDS"
    assert failure(page, frame.profile, "thresholds", bubble_radius=RING) == "INVALID_THRESHOLDS"
    for radius in (0.5, float("nan"), float("inf")):
        assert failure(page, frame.profile, good, bubble_radius=radius) == "INVALID_BUBBLE_RADIUS"
    assert failure(page, "profile", good, bubble_radius=RING) == "INVALID_PROFILE"
    assert failure(page, empty_profile, good, bubble_radius=RING) == "INVALID_FROZEN_PROFILE"
    # Cells are placed by ratio, so only an image too small to hold them is a geometry error.
    assert failure(page[:4, :6], frame.profile, good, bubble_radius=RING) == (
        "INVALID_PROFILE_GEOMETRY"
    )


def test_a_frame_drawn_at_high_resolution_is_read_without_hitting_opencv_limits() -> None:
    # 500 bubbles of radius 25.6 px need more ring samples than one OpenCV remap allows.
    frame = _frame((20, 20, 20, 20, 20))
    scale = 1.6
    page = cv2.resize(
        _page(frame, {1: 3, 37: 5, 100: 1}), None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    )

    result = read_grid(page, frame.profile, make_thresholds(), bubble_radius=RING * scale)

    assert isinstance(result, Ok)
    marked = {a.question: a.value.choices for a in result.value.answers if a.value.status is NORMAL}
    assert marked == {1: (3,), 37: (5,), 100: (1,)}
    assert result.value.student_id.value == "20250001"
