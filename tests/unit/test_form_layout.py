from __future__ import annotations

import cv2
import numpy as np
import pytest

from omr_grader.recognition.bubbles import find_bubbles
from omr_grader.recognition.form_layout import (
    RIGHT_ANGLES,
    FormLayout,
    LatticeBlock,
    detect_layout,
    drop_unmarked_header_rows,
    layout_from_bubbles,
    rotate_points,
)
from tests.helpers.omr_engine import reference_sheet
from tests.helpers.synthetic_omr import (
    NAME_BOX,
    SheetGeometry,
    add_handwriting,
    paint_ring,
    render_sheet_with_geometry,
    to_gray,
    turn_points,
)

HUNDRED = ((8,), ((5, 20),) * 5)


def _assert_nodes_on_truth(layout: FormLayout, geometry: SheetGeometry, tolerance: float) -> None:
    """Every fitted lattice node sits on the printed ring it stands for."""
    size = (geometry.width, geometry.height)
    ids = layout.id_blocks[0].nodes
    expected = turn_points(geometry.id_nodes, layout.rotation, *size)
    assert ids.shape == expected.shape
    assert np.abs(ids - expected).max() < tolerance
    for block, truth in zip(layout.answer_blocks, geometry.block_nodes, strict=True):
        expected = turn_points(truth, layout.rotation, *size)
        assert block.nodes.shape == expected.shape
        assert np.abs(block.nodes - expected).max() < tolerance


def _starts(layout: FormLayout) -> tuple[int | None, ...]:
    return tuple(block.question_start for block in layout.answer_blocks)


def test_hundred_question_form_is_numbered_left_to_right() -> None:
    sheet = reference_sheet()
    layout = sheet.layout

    assert layout.rotation == 0
    assert abs(layout.skew_radians) < 0.002
    assert [block.kind for block in layout.blocks].count("id") == 1
    assert [block.kind for block in layout.blocks].count("answer") == 5
    assert len(layout.blocks) == 6
    assert (layout.id_blocks[0].cols, layout.id_blocks[0].rows) == (8, 10)
    assert layout.id_blocks[0].question_start is None
    assert _starts(layout) == (1, 21, 41, 61, 81)
    assert [(b.cols, b.rows) for b in layout.answer_blocks] == [(5, 20)] * 5
    assert layout.question_count == 100
    assert layout.signature == HUNDRED
    # Every ring was seen, so nothing had to be recovered from neighbors.
    assert [block.detected for block in layout.id_blocks] == [80]
    assert [block.detected for block in layout.answer_blocks] == [100] * 5
    assert max(block.residual for block in layout.blocks) < 0.5
    assert 18.0 < layout.radius < 28.0
    _assert_nodes_on_truth(layout, sheet.geometry, 1.0)


def test_fifty_question_form_ends_with_a_short_block() -> None:
    sheet = reference_sheet((20, 20, 10))
    layout = sheet.layout

    assert layout.question_count == 50
    assert _starts(layout) == (1, 21, 41)
    assert [(b.cols, b.rows) for b in layout.answer_blocks] == [(5, 20), (5, 20), (5, 10)]
    assert [b.kind for b in layout.answer_blocks] == ["answer"] * 3  # the 5x10 block too
    assert layout.signature == ((8,), ((5, 20), (5, 20), (5, 10)))
    assert layout.signature != HUNDRED
    _assert_nodes_on_truth(layout, sheet.geometry, 1.0)


def test_five_by_ten_lattices_are_told_apart_by_their_printed_digits() -> None:
    # An ID-like grid of five digit columns and five answer blocks, all of them 5x10.
    image, geometry = render_sheet_with_geometry({}, "", id_columns=5, layout=(10,) * 5)
    gray = to_gray(image)
    bubbles = find_bubbles(gray)
    assert bubbles is not None

    layout = detect_layout(gray)
    without_digits = layout_from_bubbles(bubbles, None)

    assert layout is not None and without_digits is not None
    assert [(b.cols, b.rows) for b in layout.id_blocks] == [(5, 10)]
    assert [(b.cols, b.rows) for b in layout.answer_blocks] == [(5, 10)] * 5
    assert _starts(layout) == (1, 11, 21, 31, 41)
    assert layout.signature == ((5,), ((5, 10),) * 5)
    _assert_nodes_on_truth(layout, geometry, 1.0)
    # Without the page, a 5x10 lattice cannot be an ID grid: it is read as an answer block.
    assert without_digits.id_blocks == ()
    assert [b.kind for b in without_digits.blocks] == ["answer"] * 6


@pytest.mark.parametrize(
    ("rotation", "page_turn", "skew"),
    ((1.5, 0, 1.5), (88.5, 90, -1.5)),
    ids=("skewed-clockwise", "skewed-counterclockwise-and-sideways"),
)
def test_skew_is_measured_and_numbering_survives(
    rotation: float, page_turn: int, skew: float
) -> None:
    image, geometry = render_sheet_with_geometry({}, "", rotation=rotation)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert geometry.turn == page_turn
    assert layout.rotation == geometry.upright_rotation
    assert layout.skew_radians == pytest.approx(np.radians(skew), abs=0.003)
    assert layout.signature == HUNDRED
    assert _starts(layout) == (1, 21, 41, 61, 81)
    _assert_nodes_on_truth(layout, geometry, 1.5)


@pytest.mark.parametrize("turn", (90, 180, 270))
def test_a_page_scanned_at_any_right_angle_gives_the_upright_structure(turn: int) -> None:
    upright = reference_sheet()
    image, geometry = render_sheet_with_geometry(upright.answers, upright.student_id, rotation=turn)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert layout.rotation == geometry.upright_rotation == (360 - turn) % 360
    assert layout.signature == upright.layout.signature
    assert _starts(layout) == _starts(upright.layout) == (1, 21, 41, 61, 81)
    assert [b.rows for b in layout.answer_blocks] == [20] * 5
    assert abs(layout.skew_radians) < 0.002
    _assert_nodes_on_truth(layout, geometry, 1.0)


@pytest.mark.parametrize("turn", (0, 180))
def test_printed_digit_weights_orient_a_page_without_question_numbers(turn: int) -> None:
    # Digit (1) carries less ink than (5), so the page reads upright without numbers.
    image, geometry = render_sheet_with_geometry({}, "", rotation=turn, decorations=False)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert layout.rotation == geometry.upright_rotation
    assert _starts(layout) == (1, 21, 41, 61, 81)
    _assert_nodes_on_truth(layout, geometry, 1.0)


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("turn", (0, 180))
def test_question_numbers_orient_a_form_whose_rings_print_no_digits(turn: int, seed: int) -> None:
    # Empty rings give the digit cue nothing; its noise must not overrule the numbers.
    image, geometry = render_sheet_with_geometry({}, "", rotation=turn, digits=False, seed=seed)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert layout.rotation == geometry.upright_rotation
    assert _starts(layout) == (1, 21, 41, 61, 81)
    _assert_nodes_on_truth(layout, geometry, 1.0)


def test_a_row_of_column_label_rings_above_each_block_is_not_taken_for_question_1() -> None:
    # Unnumbered label rings one row above every block would shift each number by one.
    image, geometry = render_sheet_with_geometry({}, "", header_rings=True)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert layout.signature == HUNDRED
    assert _starts(layout) == (1, 21, 41, 61, 81)
    _assert_nodes_on_truth(layout, geometry, 1.0)


def test_a_plain_numbered_sheet_keeps_its_first_and_last_rows() -> None:
    image, geometry = render_sheet_with_geometry({}, "", seed=5)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert [block.rows for block in layout.answer_blocks] == [20] * 5
    _assert_nodes_on_truth(layout, geometry, 1.0)


@pytest.mark.parametrize("share", (0.8, 0.9, 1.0))
@pytest.mark.parametrize("turn", (0, 180))
def test_a_student_marking_choice_1_almost_everywhere_does_not_turn_the_page(
    share: float, turn: int
) -> None:
    # A marked (1) carries far more ink than a printed (5); marked rows are not compared.
    answers = {question: 1 if question % 10 < 10 * share else 3 for question in range(1, 101)}
    image, geometry = render_sheet_with_geometry(answers, "20261234", rotation=turn)

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert layout.rotation == geometry.upright_rotation
    assert _starts(layout) == (1, 21, 41, 61, 81)


def _answered(shift: int, count: int = 100) -> dict[int, int]:
    return {question: (question + shift) % 5 + 1 for question in range(1, count + 1)}


def _pages(
    count: int, answered: int = 100, **options: object
) -> list[tuple[FormLayout, np.ndarray]]:
    """Detected layouts of ``count`` sheets, each answering questions 1..``answered``."""
    found = []
    for page in range(count):
        image, _ = render_sheet_with_geometry(
            _answered(page, answered),
            "20261234",
            seed=page,
            **options,  # type: ignore[arg-type]
        )
        gray = to_gray(image)
        layout = detect_layout(gray)
        assert layout is not None
        found.append((layout, gray))
    return found


@pytest.mark.parametrize(("label", "in_table"), (("No.", False), ("0", False), (None, True)))
def test_label_rows_no_student_marks_are_dropped_given_three_pages(
    label: str | None, in_table: bool
) -> None:
    # A label or a table rule in the number cell defeats the one-page number check.
    found = _pages(3, header_rings=True, header_label=label, header_in_table=in_table)
    assert [block.rows for block in found[0][0].answer_blocks] == [21] * 5

    layouts, dropped = drop_unmarked_header_rows(found)

    assert dropped == 5
    for layout in layouts:
        assert layout.signature == HUNDRED
        assert _starts(layout) == (1, 21, 41, 61, 81)


def test_two_pages_are_not_enough_to_drop_a_header_row() -> None:
    found = _pages(2, header_rings=True, header_label="No.")

    layouts, dropped = drop_unmarked_header_rows(found)

    assert dropped == 0
    assert [block.rows for block in layouts[0].answer_blocks] == [21] * 5


def test_a_short_exam_on_a_long_card_keeps_every_row() -> None:
    # Unused questions leave rows unmarked at the end of blocks and in whole blocks.
    found = _pages(3, answered=30)

    layouts, dropped = drop_unmarked_header_rows(found)

    assert dropped == 0
    assert all(layout.signature == HUNDRED for layout in layouts)


def test_a_first_question_nobody_answered_is_dropped_and_reported() -> None:
    # The documented trade-off: on three or more pages a question 1 that every student
    # left blank looks like a header row. The count it reports (99) lets the user notice.
    found = []
    for page in range(3):
        answers = {q: a for q, a in _answered(page).items() if q != 1}
        image, _ = render_sheet_with_geometry(answers, "20261234", seed=page)
        gray = to_gray(image)
        layout = detect_layout(gray)
        assert layout is not None
        found.append((layout, gray))

    layouts, dropped = drop_unmarked_header_rows(found)

    assert dropped == 1
    assert layouts[0].question_count == 99


@pytest.mark.parametrize("gap", (2.0, 3.0))
def test_blocks_stacked_one_or_two_empty_rows_apart_stay_separate(gap: float) -> None:
    # Bridging those empty rows would turn them into questions and shift every number.
    image, geometry = render_sheet_with_geometry(
        {}, "", layout=((8, 8), (8, 8)), stack_gap_rows=gap
    )

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert [block.rows for block in layout.answer_blocks] == [8] * 4
    assert _starts(layout) == (1, 9, 17, 25)
    _assert_nodes_on_truth(layout, geometry, 1.0)


def test_bands_are_numbered_left_to_right_and_blocks_of_a_band_top_to_bottom() -> None:
    image, geometry = render_sheet_with_geometry({}, "", layout=((8, 8), (8, 8)))

    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert _starts(layout) == (1, 9, 17, 25)
    assert [b.rows for b in layout.answer_blocks] == [8] * 4
    assert layout.question_count == 32
    # Blocks 1 and 2 share a band and block 3 starts the next band.
    first, second, third = (layout.answer_blocks[i].nodes[0, 0] for i in range(3))
    assert first[0] == pytest.approx(second[0], abs=1.0)
    assert second[1] > first[1] + 700
    assert third[0] > first[0] + 400
    _assert_nodes_on_truth(layout, geometry, 1.0)


def test_missing_rings_are_recovered_and_stray_header_rings_are_trimmed() -> None:
    sheet = reference_sheet()
    page = sheet.gray.copy()
    paper = int(np.median(page))
    first, second, *_ = sheet.geometry.block_nodes
    # A whole interior row of block 1 and a few scattered rings of block 2 are missing.
    for choice in range(5):
        cv2.circle(page, tuple(int(v) for v in first[7, choice]), 32, (paper,), -1)
    for row, choice in ((3, 0), (9, 2), (15, 4)):
        cv2.circle(page, tuple(int(v) for v in second[row, choice]), 32, (paper,), -1)
    # Two header circles sit one row above the first row of block 1.
    for choice in (0, 1):
        x, y = first[0, choice]
        paint_ring(page, (float(x), float(y) - 99.0), digit=choice + 1)

    layout = detect_layout(page)

    assert layout is not None
    assert layout.signature == HUNDRED
    assert _starts(layout) == (1, 21, 41, 61, 81)
    assert layout.answer_blocks[0].detected == 95
    assert layout.answer_blocks[1].detected == 97
    _assert_nodes_on_truth(layout, sheet.geometry, 1.0)


@pytest.mark.parametrize("seed", (1, 2))
def test_handwriting_and_text_in_an_empty_area_create_no_blocks(seed: int) -> None:
    sheet = reference_sheet()
    page = sheet.image.copy()
    add_handwriting(page, NAME_BOX, seed)

    layout = detect_layout(to_gray(page))

    assert layout is not None
    assert len(layout.blocks) == 6
    assert layout.signature == HUNDRED
    assert _starts(layout) == (1, 21, 41, 61, 81)
    assert [block.detected for block in layout.blocks] == [80, 100, 100, 100, 100, 100]
    _assert_nodes_on_truth(layout, sheet.geometry, 1.0)


def test_pages_without_a_form_have_no_layout() -> None:
    rng = np.random.default_rng(3)
    scattered = np.full((2480, 3508), 245, dtype=np.uint8)
    for _ in range(60):
        center = (int(rng.integers(80, 3400)), int(rng.integers(80, 2400)))
        cv2.circle(scattered, center, 22, (110,), 3, cv2.LINE_AA)

    assert detect_layout(np.full((600, 900), 245, dtype=np.uint8)) is None
    assert detect_layout(scattered) is None


def test_layout_properties_order_blocks_by_question_number() -> None:
    def block(kind: str, cols: int, rows: int, start: int | None = None) -> LatticeBlock:
        return LatticeBlock(kind, cols, rows, np.zeros((rows, cols, 2)), rows * cols, 0.1, start)

    layout = FormLayout(
        24.0,
        0.0,
        (
            block("answer", 5, 10, 21),
            block("id", 8, 10),
            block("other", 4, 4),
            block("answer", 5, 20, 1),
        ),
    )

    assert layout.rotation == 0
    assert _starts(layout) == (1, 21)
    assert layout.question_count == 30
    assert [b.cols for b in layout.id_blocks] == [8]
    assert layout.signature == ((8,), ((5, 20), (5, 10)))


def test_rotate_points_follows_clockwise_right_angle_rotation() -> None:
    marker = np.zeros((50, 80), dtype=np.uint8)
    marker[12, 63] = 255
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    point = np.array([[63.0, 12.0]])

    assert RIGHT_ANGLES == (0, 90, 180, 270)
    assert np.array_equal(rotate_points(point, 0, 80, 50), point)
    for rotation, code in codes.items():
        rotated = cv2.rotate(marker, code)
        row, column = np.unravel_index(int(np.argmax(rotated)), rotated.shape)
        assert tuple(rotate_points(point, rotation, 80, 50)[0]) == (float(column), float(row))
    with pytest.raises(ValueError, match="rotation"):
        rotate_points(point, 45, 80, 50)
