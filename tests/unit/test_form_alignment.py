from __future__ import annotations

import cv2
import numpy as np
import pytest

from omr_grader.domain.errors import Ok
from omr_grader.domain.profile import Profile, parse_profile_bytes
from omr_grader.recognition.form_alignment import (
    ROTATIONS,
    PageAlignment,
    align_page,
    profile_nodes,
)
from omr_grader.recognition.orientation import rotate_right_angle
from tests.helpers.omr_engine import reference_sheet
from tests.helpers.synthetic_omr import (
    NAME_BOX,
    add_handwriting,
    render_sheet_with_geometry,
    to_gray,
)

CODES = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _frame_errors(
    alignment: PageAlignment, profile: Profile, page_centers: np.ndarray
) -> tuple[float, float]:
    """Median and worst frame-pixel distance between mapped true centers and profile cells."""
    nodes = profile_nodes(profile)[0]
    mapped = cv2.perspectiveTransform(page_centers.reshape(-1, 1, 2), alignment.forward)
    errors = np.hypot(*(mapped.reshape(-1, 2) - nodes).T)
    return float(np.median(errors)), float(errors.max())


def _assert_aligned(
    alignment: PageAlignment | None,
    profile: Profile,
    page_centers: np.ndarray,
    *,
    rotation: int = 0,
    worst: float = 1.0,
) -> PageAlignment:
    assert alignment is not None
    assert alignment.rotation == rotation
    assert alignment.nodes == 580
    assert alignment.inlier_ratio > 0.99
    assert alignment.residual < 0.3
    assert alignment.inliers > alignment.runner_up_inliers
    median, largest = _frame_errors(alignment, profile, page_centers)
    assert median < 0.4
    assert largest < worst
    return alignment


def _legacy_profile() -> Profile:
    payload = b"""{"profile_name": "old", "regions": [
        {"name": "id", "type": "id", "bbox_ratio": {"x": 0, "y": 0, "w": 0.2, "h": 0.4},
         "grid": {"cols": 8, "rows": 10}},
        {"name": "a", "type": "answer", "bbox_ratio": {"x": 0.3, "y": 0, "w": 0.2, "h": 0.8},
         "grid": {"cols": 5, "rows": 20}}]}"""
    parsed = parse_profile_bytes(payload)
    assert isinstance(parsed, Ok)
    return parsed.value


def test_an_upright_page_aligns_to_its_own_profile_on_every_printed_ring() -> None:
    sheet = reference_sheet()

    alignment = _assert_aligned(
        align_page(sheet.gray, sheet.profile), sheet.profile, sheet.geometry.centers, worst=0.8
    )

    assert alignment.inlier_ratio == 1.0
    assert 0.5 < alignment.confidence <= 1.0
    assert alignment.runner_up_inliers < 0.95 * alignment.inliers
    assert 8.0 < alignment.bubble_radius < 24.0
    assert np.allclose(alignment.forward @ alignment.inverse, np.eye(3), atol=1e-6)
    assert alignment.forward.shape == (3, 3)


@pytest.mark.parametrize(
    "options",
    (
        {"rotation": 1.5},
        {"rotation": -4.0},
        {"scale": 0.95},
        {"scale": 1.05},
        {"shift": (95.0, -80.0)},
        {"rotation": 1.0, "scale": 0.98, "shift": (-60.0, 45.0)},
    ),
    ids=("skew+1.5", "skew-4", "scale0.95", "scale1.05", "shift", "combined"),
)
def test_skew_scale_and_shift_are_recovered(options: dict[str, object]) -> None:
    sheet = reference_sheet()
    image, geometry = render_sheet_with_geometry(
        sheet.answers,
        sheet.student_id,
        **options,  # type: ignore[arg-type]
    )

    alignment = align_page(to_gray(image), sheet.profile)

    _assert_aligned(alignment, sheet.profile, geometry.centers, worst=1.2)


@pytest.mark.parametrize("turn", (90, 180, 270))
def test_a_page_turned_by_a_right_angle_gets_the_clockwise_rotation_that_uprights_it(
    turn: int,
) -> None:
    sheet = reference_sheet()
    turned = cv2.rotate(sheet.gray, CODES[turn])

    alignment = _assert_aligned(
        align_page(turned, sheet.profile),
        sheet.profile,
        sheet.geometry.centers,  # in the upright frame the alignment maps from
        rotation=(360 - turn) % 360,
        worst=0.8,
    )

    assert np.array_equal(rotate_right_angle(turned, alignment.rotation), sheet.gray)
    assert alignment.confidence > 0.5


def test_a_scan_at_another_resolution_is_aligned_to_the_same_profile() -> None:
    sheet = reference_sheet()
    small = cv2.resize(sheet.gray, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA)

    alignment = align_page(small, sheet.profile)

    _assert_aligned(alignment, sheet.profile, (sheet.geometry.centers + 0.5) * 0.6 - 0.5)


def test_handwriting_on_the_page_does_not_disturb_the_alignment() -> None:
    sheet = reference_sheet()
    page = sheet.image.copy()
    add_handwriting(page, NAME_BOX, 5)

    alignment = align_page(to_gray(page), sheet.profile)

    _assert_aligned(alignment, sheet.profile, sheet.geometry.centers, worst=0.8)


def test_pages_that_are_not_the_form_are_not_aligned() -> None:
    sheet = reference_sheet()
    rng = np.random.default_rng(2)
    scattered = np.full((2480, 3508), 245, dtype=np.uint8)
    for _ in range(400):
        center = (int(rng.integers(60, 3400)), int(rng.integers(60, 2400)))
        cv2.circle(scattered, center, 22, (110,), 3, cv2.LINE_AA)
    lines = np.full((2480, 3508), 245, dtype=np.uint8)
    for row in range(150, 2400, 75):
        cv2.putText(lines, "lorem ipsum dolor sit amet", (100, row), 0, 1.6, (30,), 2, cv2.LINE_AA)

    assert align_page(np.full((2480, 3508), 245, dtype=np.uint8), sheet.profile) is None
    assert align_page(scattered, sheet.profile) is None
    assert align_page(lines, sheet.profile) is None


def test_a_page_holding_too_little_of_the_form_is_not_aligned() -> None:
    sheet = reference_sheet()
    one_block, _ = render_sheet_with_geometry({}, "", layout=(20,))

    # 180 of the 580 printed bubbles: far below the half that is needed.
    assert align_page(to_gray(one_block), sheet.profile) is None


def test_a_smaller_form_fits_only_partly_and_is_reported_as_such() -> None:
    sheet = reference_sheet()
    fifty = reference_sheet((20, 20, 10))

    alignment = align_page(fifty.gray, sheet.profile)

    assert alignment is not None
    assert alignment.rotation == 0
    assert alignment.nodes == 580
    # 330 of the 580 cells are printed: above the 50% floor, below the 65% review line.
    assert 320 <= alignment.inliers <= 345
    assert 0.5 <= alignment.inlier_ratio < 0.65


def test_a_profile_without_a_frame_size_cannot_align_pages() -> None:
    sheet = reference_sheet()
    legacy = _legacy_profile()

    assert legacy.page is None
    assert align_page(sheet.gray, legacy) is None
    with pytest.raises(ValueError, match="frame size"):
        profile_nodes(legacy)


def test_profile_nodes_list_every_cell_region_by_region() -> None:
    sheet = reference_sheet()

    nodes, regions = profile_nodes(sheet.profile)

    assert regions == sheet.profile.regions
    assert nodes.shape == (580, 2)
    page = sheet.profile.page
    assert page is not None
    assert nodes[:, 0].min() > 0 and nodes[:, 0].max() < page.source_width
    assert nodes[:, 1].min() > 0 and nodes[:, 1].max() < page.source_height
    # The ID grid comes first, row by row: eight cells of a row, then the next row.
    assert np.all(np.diff(nodes[:8, 0]) > 0) and np.ptp(nodes[:8, 1]) < 1e-6
    assert nodes[8, 1] > nodes[0, 1] and nodes[8, 0] == pytest.approx(nodes[0, 0])


def test_scores_follow_from_the_counts() -> None:
    eye = np.eye(3)
    clear = PageAlignment(0, eye, eye, 580, 580, 0.1, 16.0, 500)
    tie = PageAlignment(180, eye, eye, 580, 580, 0.1, 16.0, 580)
    half = PageAlignment(0, eye, eye, 290, 580, 0.1, 16.0, 0)
    empty = PageAlignment(0, eye, eye, 0, 0, 0.0, 16.0, 0)

    assert clear.inlier_ratio == 1.0
    assert clear.confidence == pytest.approx(2 * 80 / 580 + 0.5)
    assert tie.confidence == pytest.approx(0.5)  # another rotation fits just as well
    assert half.inlier_ratio == 0.5
    assert half.confidence == pytest.approx(0.5)
    assert empty.inlier_ratio == 0.0 and empty.confidence == 0.0
    assert ROTATIONS == (0, 180, 90, 270)
