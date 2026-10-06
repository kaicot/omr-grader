from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.domain.profile import Grid, parse_profile_bytes
from omr_grader.recognition.form_alignment import align_page, profile_nodes
from omr_grader.recognition.bubbles import find_bubbles
from omr_grader.recognition.form_layout import (
    FormLayout,
    LatticeBlock,
    detect_layout,
    layout_from_bubbles,
)
from omr_grader.recognition.form_profile import (
    FRAME_BUBBLE_RADIUS,
    MAX_QUESTIONS,
    build_profile,
    supported,
)
from tests.helpers.omr_engine import reference_sheet
from tests.helpers.synthetic_omr import render_sheet_with_geometry, to_gray

QUESTION_NAMES = ["q001_020", "q021_040", "q041_060", "q061_080", "q081_100"]


def _similarity(source: np.ndarray, target: np.ndarray) -> tuple[float, float, float]:
    """Least-squares scale, rotation (radians) and worst residual of source -> target."""
    count = len(source)
    design = np.zeros((2 * count, 4))
    design[0::2] = np.c_[source[:, 0], -source[:, 1], np.ones(count), np.zeros(count)]
    design[1::2] = np.c_[source[:, 1], source[:, 0], np.zeros(count), np.ones(count)]
    flat = target.reshape(-1)
    solution, *_ = np.linalg.lstsq(design, flat, rcond=None)
    scale = float(np.hypot(solution[0], solution[1]))
    rotation = float(np.arctan2(solution[1], solution[0]))
    return scale, rotation, float(np.abs(design @ solution - flat).max())


def _block(kind: str, cols: int, rows: int, start: int | None = None) -> LatticeBlock:
    return LatticeBlock(kind, cols, rows, np.zeros((rows, cols, 2)), rows * cols, 0.1, start)


def _code(error: ErrorInfo | None) -> str | None:
    return None if error is None else error.code


def test_profile_is_built_from_a_detected_layout() -> None:
    sheet = reference_sheet()
    height, width = sheet.gray.shape

    built = build_profile([(sheet.layout, (width, height))], "my form")

    assert isinstance(built, Ok)
    profile, payload = built.value
    scale = FRAME_BUBBLE_RADIUS / sheet.layout.radius
    assert profile.profile_name == "my form"
    assert profile.schema_version == 1
    assert [region.kind for region in profile.regions] == ["id"] + ["answer"] * 5
    assert [region.name for region in profile.regions] == ["id", *QUESTION_NAMES]
    assert profile.id_region.grid == Grid(8, 10)
    assert profile.id_region.question_start is None
    assert [region.grid for region in profile.answer_regions] == [Grid(5, 20)] * 5
    assert [region.question_start for region in profile.answer_regions] == [1, 21, 41, 61, 81]
    assert profile.page is not None
    assert profile.page.orientation == "landscape"
    assert (profile.page.source_width, profile.page.source_height) == (
        round(width * scale),
        round(height * scale),
    )
    reparsed = parse_profile_bytes(payload)
    assert isinstance(reparsed, Ok) and reparsed.value == profile
    wire = json.loads(payload)
    assert wire["schema_version"] == 1
    assert wire["regions"][0]["type"] == "id"
    assert "question_start" not in wire["regions"][0]
    assert wire["regions"][1]["question_start"] == 1


def test_profile_cells_sit_on_the_printed_rings_at_the_frame_bubble_size() -> None:
    sheet = reference_sheet()
    profile = sheet.profile

    nodes, regions = profile_nodes(profile)
    scale, rotation, worst = _similarity(sheet.geometry.centers, nodes)

    assert len(regions) == 6
    assert nodes.shape == (580, 2)
    assert scale == pytest.approx(FRAME_BUBBLE_RADIUS / sheet.layout.radius, rel=0.01)
    assert abs(rotation) < 0.01
    assert worst < 0.7  # frame pixels: bubbles are 16 px in radius there


def test_several_pages_are_averaged_and_pages_of_another_form_are_ignored() -> None:
    sheet = reference_sheet()
    other_form = reference_sheet((20, 20, 10))
    second_image, _ = render_sheet_with_geometry(
        sheet.answers, sheet.student_id, shift=(25.0, -15.0), seed=7
    )
    second = detect_layout(to_gray(second_image))
    assert second is not None
    size = (sheet.gray.shape[1], sheet.gray.shape[0])

    single = build_profile([(sheet.layout, size)], "form")
    averaged = build_profile(
        [(sheet.layout, size), (second, size), (other_form.layout, size)], "form"
    )

    assert isinstance(single, Ok) and isinstance(averaged, Ok)
    profile = averaged.value[0]
    assert [region.name for region in profile.regions] == ["id", *QUESTION_NAMES]
    assert [region.question_start for region in profile.answer_regions] == [1, 21, 41, 61, 81]
    # The frame follows the median bubble radius of the pages, so it may differ by a pixel
    # or two; the cells themselves stay on the same lattice.
    assert profile.page is not None and single.value[0].page is not None
    assert profile.page.source_width == pytest.approx(single.value[0].page.source_width, rel=0.01)
    assert profile.page.source_height == pytest.approx(single.value[0].page.source_height, rel=0.01)
    scale, rotation, worst = _similarity(
        profile_nodes(single.value[0])[0], profile_nodes(profile)[0]
    )
    assert scale == pytest.approx(1.0, abs=0.01)
    assert abs(rotation) < 0.01
    assert worst < 1.0
    assert profile.sha256 != single.value[0].sha256  # the second page did contribute


def test_cells_stay_on_the_rings_when_the_sample_pages_are_skewed_differently() -> None:
    """Engine defect reproducer: ``build_profile`` deskews by the median skew of its samples.

    The averaged lattice lives in the frame of the first sample, so a median that differs
    from that sample's skew leaves the profile turned against its own cells. With a 0.7
    degree page next to an upright one, projecting the cells back onto the first page
    misses the printed rings by about 2.9 px (0.3 px with either page alone).
    """
    sheet = reference_sheet()
    skewed_image, _ = render_sheet_with_geometry(
        sheet.answers, sheet.student_id, rotation=0.7, shift=(25.0, -15.0), seed=7
    )
    skewed = detect_layout(to_gray(skewed_image))
    assert skewed is not None
    size = (sheet.gray.shape[1], sheet.gray.shape[0])

    built = build_profile([(sheet.layout, size), (skewed, size)], "form")

    assert isinstance(built, Ok)
    alignment = align_page(sheet.gray, built.value[0])
    assert alignment is not None and alignment.rotation == 0
    nodes = profile_nodes(built.value[0])[0]
    on_page = cv2.perspectiveTransform(nodes.reshape(-1, 1, 2), alignment.inverse).reshape(-1, 2)
    worst = float(np.hypot(*(on_page - sheet.geometry.centers).T).max())
    assert worst < 1.0, f"profile cells miss the printed rings by up to {worst:.2f} px"


def test_a_sample_read_turned_over_is_left_out_even_when_it_comes_first() -> None:
    # Its lattice pairs every node with the wrong bubble; averaged in, it ruins the ID grid.
    sheet = reference_sheet()
    flipped_gray = cv2.rotate(sheet.gray, cv2.ROTATE_180)
    bubbles = find_bubbles(flipped_gray)
    assert bubbles is not None
    flipped = layout_from_bubbles(bubbles, flipped_gray)  # measured without turning it upright
    assert (
        flipped is not None
        and flipped.rotation == 0
        and flipped.signature == sheet.layout.signature
    )
    other_image, _ = render_sheet_with_geometry(sheet.answers, sheet.student_id, seed=4)
    other = detect_layout(to_gray(other_image))
    assert other is not None
    size = (sheet.gray.shape[1], sheet.gray.shape[0])

    built = build_profile([(flipped, size), (sheet.layout, size), (other, size)], "form")

    assert isinstance(built, Ok)
    alignment = align_page(sheet.gray, built.value[0])
    assert alignment is not None and alignment.rotation == 0 and alignment.trusted
    nodes = profile_nodes(built.value[0])[0]
    on_page = cv2.perspectiveTransform(nodes.reshape(-1, 1, 2), alignment.inverse).reshape(-1, 2)
    assert float(np.hypot(*(on_page - sheet.geometry.centers).T).max()) < 1.0


def test_a_form_without_samples_is_not_found() -> None:
    result = build_profile([], "form")

    assert isinstance(result, Err)
    assert result.errors[0].code == "FORM_NOT_FOUND"
    assert result.errors[0].message_key == "error.form_not_found"


def test_a_sideways_sample_builds_the_same_landscape_frame() -> None:
    sheet = reference_sheet()
    sideways, geometry = render_sheet_with_geometry(sheet.answers, sheet.student_id, rotation=90)
    layout = detect_layout(to_gray(sideways))
    assert layout is not None and layout.rotation == geometry.upright_rotation == 270

    built = build_profile([(layout, (sideways.shape[1], sideways.shape[0]))], "sideways")

    assert isinstance(built, Ok)
    profile = built.value[0]
    assert profile.page is not None and sheet.profile.page is not None
    assert profile.page.orientation == "landscape"
    assert abs(profile.page.source_width - sheet.profile.page.source_width) <= 2
    assert abs(profile.page.source_height - sheet.profile.page.source_height) <= 2
    scale, rotation, worst = _similarity(profile_nodes(sheet.profile)[0], profile_nodes(profile)[0])
    assert scale == pytest.approx(1.0, abs=0.005)
    assert abs(rotation) < 0.005
    assert worst < 1.0
    # The upright page is aligned with it without any turn.
    alignment = align_page(sheet.gray, profile)
    assert alignment is not None
    assert alignment.rotation == 0
    assert alignment.inlier_ratio > 0.99


def test_a_supported_layout_has_no_problem() -> None:
    sheet = reference_sheet()

    assert supported(sheet.layout) is None
    assert supported(reference_sheet((20, 20, 10)).layout) is None
    assert (
        supported(FormLayout(24.0, 0.0, (_block("id", 8, 10), _block("answer", 5, 100, 1)))) is None
    )


def test_an_id_grid_that_is_not_eight_digit_columns_is_unsupported() -> None:
    narrow, _ = render_sheet_with_geometry({}, "", id_columns=5)
    missing, _ = render_sheet_with_geometry({}, "", id_columns=0)
    layouts = {
        name: detect_layout(to_gray(image)) for name, image in (("5", narrow), ("0", missing))
    }

    for name, layout in layouts.items():
        assert layout is not None, name
        problem = supported(layout)
        assert problem is not None and problem.code == "FORM_ID_UNSUPPORTED"
        assert problem.message_key == "error.form_id_unsupported"
        built = build_profile([(layout, (3508, 2480))], "form")
        assert isinstance(built, Err)
        assert built.errors[0].code == "FORM_ID_UNSUPPORTED"
    assert layouts["5"] is not None and layouts["5"].id_blocks[0].cols == 5
    assert layouts["0"] is not None and layouts["0"].id_blocks == ()


def test_a_page_with_only_the_id_grid_has_no_answers() -> None:
    image, _ = render_sheet_with_geometry({}, "", layout=())
    layout = detect_layout(to_gray(image))

    assert layout is not None
    assert [block.kind for block in layout.blocks] == ["id"]
    problem = supported(layout)
    assert problem is not None and problem.code == "FORM_ANSWERS_UNSUPPORTED"
    built = build_profile([(layout, (3508, 2480))], "form")
    assert isinstance(built, Err)
    assert built.errors[0].code == "FORM_ANSWERS_UNSUPPORTED"


def test_hand_built_layouts_report_the_first_problem_they_have() -> None:
    id_block = _block("id", 8, 10)
    answers = _block("answer", 5, 20, 1)
    cases = {
        "two ID grids": (id_block, _block("id", 8, 10), answers),
        "ten-column ID": (_block("id", 10, 10), answers),
        "four choices": (id_block, _block("answer", 4, 20, 1)),
        "no answer block": (id_block, _block("other", 4, 4)),
        "101 questions": (id_block, _block("answer", 5, 100, 1), _block("answer", 5, 1, 101)),
    }

    codes = {
        name: _code(supported(FormLayout(24.0, 0.0, blocks))) for name, blocks in cases.items()
    }

    assert codes == {
        "two ID grids": "FORM_ID_UNSUPPORTED",
        "ten-column ID": "FORM_ID_UNSUPPORTED",
        "four choices": "FORM_ANSWERS_UNSUPPORTED",
        "no answer block": "FORM_ANSWERS_UNSUPPORTED",
        "101 questions": "FORM_QUESTIONS_UNSUPPORTED",
    }
    assert MAX_QUESTIONS == 100
