"""The synthetic sheet generator is the ground truth of the v4 tests, so it is checked first."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from tests.helpers.synthetic_omr import (
    ANSWER_PITCH,
    BAND_PITCH,
    DISK_RADIUS,
    NAME_BOX,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    add_handwriting,
    apply_shading,
    choices_of,
    encode_jpeg,
    encode_png,
    paint_mark,
    paint_ring,
    render_sheet,
    render_sheet_with_geometry,
    sample_answers,
    to_gray,
    turn_points,
    write_png,
)


def _dark_centroid(gray: np.ndarray, center: tuple[float, float]) -> tuple[float, float]:
    """Center of the dark mark (gray < 80) in a window around ``center``."""
    x, y = round(center[0]), round(center[1])
    window = gray[y - 30 : y + 31, x - 30 : x + 31]
    ys, xs = np.nonzero(window < 80)
    assert len(xs) > 200, "no mark found at the expected position"
    return float(xs.mean()) + x - 30, float(ys.mean()) + y - 30


def test_choices_stand_for_blank_single_and_double_marks() -> None:
    assert choices_of(None) == ()
    assert choices_of(3) == (3,)
    assert choices_of((4, 2)) == (2, 4)
    assert choices_of([2, 2]) == (2,)
    for invalid in (0, 6, (1, 9)):
        with pytest.raises(ValueError, match="1 to 5"):
            choices_of(invalid)


def test_sample_answers_mix_single_marks_blanks_and_double_marks() -> None:
    answers = sample_answers(100)

    blanks = [q for q, choice in answers.items() if choice is None]
    doubles = [q for q, choice in answers.items() if isinstance(choice, tuple)]
    assert len(answers) == 100
    assert blanks == [11, 22, 33, 44, 55, 66, 77, 88, 99]
    assert doubles == [17, 34, 51, 68, 85]
    assert all(len(choices_of(answers[q])) == 2 for q in doubles)
    assert all(1 <= answers[q] <= 5 for q in answers if q not in blanks + doubles)  # type: ignore[operator]
    assert len(sample_answers(50)) == 50


@pytest.mark.parametrize(
    "options",
    (
        {},
        {"rotation": -2.0, "scale": 1.02, "shift": (40.0, -30.0)},
        {"rotation": 90},
        {"rotation": 272.0, "shift": (-20.0, 25.0)},
    ),
    ids=("upright", "skewed-scaled-shifted", "quarter-turn", "turned-and-skewed"),
)
def test_reported_geometry_sits_on_the_drawn_marks(options: dict[str, object]) -> None:
    image, geometry = render_sheet_with_geometry(
        {7: 3, 88: 5},
        "9",
        noise=0,
        blur=0,
        jitter=False,
        **options,  # type: ignore[arg-type]
    )
    gray = to_gray(image)

    for center in (geometry.answer_center(7, 3), geometry.answer_center(88, 5)):
        found = _dark_centroid(gray, center)
        assert np.hypot(found[0] - center[0], found[1] - center[1]) < 0.7
    center = geometry.id_center(0, 9)
    found = _dark_centroid(gray, center)
    assert np.hypot(found[0] - center[0], found[1] - center[1]) < 0.7
    assert geometry.upright_rotation == (360 - geometry.turn) % 360
    assert gray.shape == (geometry.height, geometry.width)


def test_blocks_stand_side_by_side_or_stacked_inside_a_band() -> None:
    _, plain = render_sheet_with_geometry(layout=(20, 20, 10), noise=0, blur=0)
    _, stacked = render_sheet_with_geometry(layout=((8, 8), (8, 8)), noise=0, blur=0)

    assert plain.block_starts == (1, 21, 41)
    assert plain.question_count == 50
    assert plain.block_nodes[1][0, 0, 0] - plain.block_nodes[0][0, 0, 0] == BAND_PITCH
    assert plain.id_nodes.shape == (10, 8, 2)
    assert stacked.block_starts == (1, 9, 17, 25)
    assert stacked.block_nodes[1][0, 0, 0] == stacked.block_nodes[0][0, 0, 0]
    assert stacked.block_nodes[1][0, 0, 1] > stacked.block_nodes[0][-1, 0, 1] + 4 * ANSWER_PITCH[1]
    assert stacked.block_nodes[2][0, 0, 0] - stacked.block_nodes[0][0, 0, 0] == BAND_PITCH
    assert plain.centers.shape == (80 + 250, 2)
    with pytest.raises(ValueError, match="not printed"):
        plain.answer_center(51, 1)


def test_quarter_turns_are_lossless_and_points_follow_them() -> None:
    upright, geometry = render_sheet_with_geometry(noise=0, blur=0)
    turned, turned_geometry = render_sheet_with_geometry(noise=0, blur=0, rotation=90)

    assert np.array_equal(turned, cv2.rotate(upright, cv2.ROTATE_90_CLOCKWISE))
    assert turned_geometry.turn == 90
    assert turned_geometry.upright_rotation == 270
    assert (turned_geometry.width, turned_geometry.height) == (PAGE_HEIGHT, PAGE_WIDTH)
    expected = turn_points(geometry.centers, 90, geometry.width, geometry.height)
    assert np.allclose(turned_geometry.centers, expected)
    with pytest.raises(ValueError, match="rotation"):
        turn_points(geometry.centers, 45, geometry.width, geometry.height)


def test_turn_points_agree_with_opencv_for_every_right_angle() -> None:
    marker = np.zeros((50, 80), dtype=np.uint8)
    marker[12, 63] = 255
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}

    for rotation, code in codes.items():
        rotated = cv2.rotate(marker, code)
        row, column = np.unravel_index(int(np.argmax(rotated)), rotated.shape)
        moved = turn_points(np.array([[63.0, 12.0]]), rotation, 80, 50)
        assert tuple(moved[0]) == (float(column), float(row))
    assert np.array_equal(turn_points(np.array([[63.0, 12.0]]), 0, 80, 50), [[63.0, 12.0]])


def test_marks_follow_the_requested_style_and_the_id_string() -> None:
    disk = render_sheet({1: 1}, "", noise=0, blur=0, jitter=False)
    dot = render_sheet({1: 1}, "", noise=0, blur=0, jitter=False, mark_style="dot")
    speck = render_sheet({1: 1}, "", noise=0, blur=0, jitter=False, mark_style="speck")
    sized = render_sheet({1: 1}, "", noise=0, blur=0, jitter=False, mark_radius=12.0)
    partial_id = render_sheet({}, "2 5", noise=0, blur=0, jitter=False)
    blank = render_sheet({}, "", noise=0, blur=0, jitter=False)

    areas = [int(np.count_nonzero(to_gray(item) < 80)) for item in (disk, dot, speck, sized, blank)]
    assert areas[0] > areas[3] > areas[1] > areas[2] > areas[4]
    disk_area = np.pi * DISK_RADIUS**2
    assert abs(areas[0] - areas[4] - disk_area) < 0.1 * disk_area
    # Digits 2 and 5 are marked; the blank middle column is left alone.
    extra = to_gray(partial_id) < 80
    changed = np.count_nonzero(extra) - areas[4]
    assert 1.8 * np.pi * 15**2 < changed < 2.2 * np.pi * 17**2


def test_sheet_options_are_validated() -> None:
    with pytest.raises(ValueError, match="mark style"):
        render_sheet(mark_style="star")
    with pytest.raises(ValueError, match="at most"):
        render_sheet(layout=(10,) * 6)
    with pytest.raises(ValueError, match="at least one row"):
        render_sheet(layout=(0,))
    with pytest.raises(ValueError, match="student_id"):
        render_sheet(student_id="123456789")
    with pytest.raises(ValueError, match="not printed"):
        render_sheet({101: 1})
    with pytest.raises(ValueError, match="1 to 5"):
        render_sheet({1: 6})


def test_rendering_is_deterministic_per_seed() -> None:
    first = render_sheet({3: 2}, "1", seed=4)
    again = render_sheet({3: 2}, "1", seed=4)
    other = render_sheet({3: 2}, "1", seed=5)

    assert first.dtype == np.uint8 and first.shape == (PAGE_HEIGHT, PAGE_WIDTH, 3)
    assert np.array_equal(first, again)
    assert not np.array_equal(first, other)


def test_paint_helpers_work_on_gray_and_color_images() -> None:
    for image in (np.full((80, 80), 245, np.uint8), np.full((80, 80, 3), 245, np.uint8)):
        paint_ring(image, (40, 40), digit=3)
        paint_mark(image, (40, 40), 6.0)
        assert tuple(np.atleast_1d(image[40, 40])) == (25,) * (3 if image.ndim == 3 else 1)
        assert np.atleast_1d(image[40, 62]).min() < 200  # the ring, 22 px from the center
        assert np.atleast_1d(image[5, 5]).min() == 245


def test_encoders_round_trip_and_files_are_written(tmp_path: Path) -> None:
    image = render_sheet({1: 1}, "2", noise=0, width=600, height=400)

    png = encode_png(image)
    jpeg = encode_jpeg(image, 80)
    written = write_png(tmp_path / "sheet.png", image)

    assert np.array_equal(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR), image)
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == image.shape
    assert written.read_bytes().startswith(b"\x89PNG")


def test_handwriting_stays_inside_its_box_and_shading_darkens_one_side() -> None:
    page = np.full((2480, 3508, 3), 245, dtype=np.uint8)
    other = page.copy()

    add_handwriting(page, NAME_BOX, 3)
    add_handwriting(other, NAME_BOX, 3)
    left, top, right, bottom = NAME_BOX
    outside = page.copy()
    outside[top - 30 : bottom + 30, left - 30 : right + 30] = 245
    shaded = apply_shading(np.full((10, 100), 200, dtype=np.uint8), 0.5)

    assert np.array_equal(page, other)
    assert np.count_nonzero(page[top:bottom, left:right] < 100) > 1000
    assert np.all(outside == 245)
    assert shaded[0, 0] == 100 and shaded[0, -1] == 200
    assert np.all(np.diff(shaded[0].astype(int)) >= 0)
