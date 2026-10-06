from __future__ import annotations

import cv2
import numpy as np
import pytest

from omr_grader.recognition.bubbles import Bubbles, find_bubbles
from tests.helpers.omr_engine import reference_sheet
from tests.helpers.synthetic_omr import (
    NAME_BOX,
    add_handwriting,
    encode_jpeg,
    render_sheet,
    render_sheet_with_geometry,
    to_gray,
)

PRINTED_BUBBLES = 80 + 5 * 100


def _gaps(found: np.ndarray, truth: np.ndarray) -> tuple[float, float]:
    """Largest distance from a true center to its nearest find, and from a find to a center."""
    distance = np.hypot(
        found[:, None, 0] - truth[None, :, 0], found[:, None, 1] - truth[None, :, 1]
    )
    return float(distance.min(axis=0).max()), float(distance.min(axis=1).max())


def _assert_all_found(
    bubbles: Bubbles | None, truth: np.ndarray, tolerance: float = 1.0
) -> Bubbles:
    assert bubbles is not None
    assert len(bubbles.centers) == len(truth)
    missing, extra = _gaps(bubbles.centers, truth)
    assert missing < tolerance
    assert extra < tolerance
    return bubbles


def test_every_printed_ring_is_found_at_its_true_center() -> None:
    sheet = reference_sheet()

    bubbles = _assert_all_found(find_bubbles(sheet.gray), sheet.geometry.centers)

    assert len(bubbles.centers) == PRINTED_BUBBLES
    assert bubbles.centers.shape == (PRINTED_BUBBLES, 2)
    assert bubbles.radii.shape == (PRINTED_BUBBLES,)
    assert bubbles.radius == pytest.approx(float(np.median(bubbles.radii)))
    # Rings of radius 22 are measured on either edge of the 3 px stroke (or the hole inside).
    assert 18.0 < bubbles.radius < 28.0


def test_pen_marks_and_printed_numbers_do_not_move_or_hide_a_ring() -> None:
    marked = reference_sheet()
    clean, geometry = render_sheet_with_geometry({}, "", decorations=False)

    found_clean = _assert_all_found(find_bubbles(to_gray(clean)), geometry.centers)
    found_marked = _assert_all_found(find_bubbles(marked.gray), marked.geometry.centers)

    missing, extra = _gaps(found_marked.centers, found_clean.centers)
    assert max(missing, extra) < 0.5


def test_detected_bubbles_are_immutable_and_samples_are_copies() -> None:
    bubbles = find_bubbles(reference_sheet().gray)
    assert bubbles is not None

    samples = bubbles.radius_samples()
    samples[:] = 0.0

    assert bubbles.radii.min() > 0
    with pytest.raises(AttributeError):
        bubbles.radius = 1.0  # type: ignore[misc]


def test_faint_print_is_still_found() -> None:
    faint, geometry = render_sheet_with_geometry({}, "", ring_gray=205)

    _assert_all_found(find_bubbles(to_gray(faint)), geometry.centers)


@pytest.mark.parametrize("factor", (0.5, 1.4))
def test_scan_resolution_does_not_matter(factor: float) -> None:
    sheet = reference_sheet()
    interpolation = cv2.INTER_AREA if factor < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(sheet.gray, None, fx=factor, fy=factor, interpolation=interpolation)
    truth = (sheet.geometry.centers + 0.5) * factor - 0.5

    _assert_all_found(find_bubbles(resized), truth)


def test_a_skewed_page_keeps_every_ring() -> None:
    skewed, geometry = render_sheet_with_geometry({}, "", rotation=5.0)

    _assert_all_found(find_bubbles(to_gray(skewed)), geometry.centers)


def test_jpeg_compression_keeps_centers_within_a_pixel() -> None:
    sheet = reference_sheet()
    decoded = cv2.imdecode(
        np.frombuffer(encode_jpeg(sheet.image, 60), dtype=np.uint8), cv2.IMREAD_GRAYSCALE
    )

    _assert_all_found(find_bubbles(decoded), sheet.geometry.centers)


def test_handwritten_loops_add_circles_but_never_hide_a_printed_ring() -> None:
    sheet = reference_sheet()
    page = sheet.image.copy()
    add_handwriting(page, NAME_BOX, 1)

    bubbles = find_bubbles(to_gray(page))

    assert bubbles is not None
    assert PRINTED_BUBBLES <= len(bubbles.centers) < PRINTED_BUBBLES + 40
    missing, _ = _gaps(bubbles.centers, sheet.geometry.centers)
    assert missing < 1.0


def test_pages_without_enough_circles_have_no_bubbles() -> None:
    paper = np.full((600, 900), 245, dtype=np.uint8)
    table = paper.copy()
    for offset in range(40, 880, 80):
        cv2.line(table, (offset, 20), (offset, 580), (60,), 3)
    cv2.rectangle(table, (20, 20), (880, 580), (60,), 3)
    few_rings = render_sheet(layout=(2,), id_columns=0)

    assert find_bubbles(paper) is None
    assert find_bubbles(table) is None
    assert find_bubbles(to_gray(few_rings)) is None  # ten rings are fewer than twenty


@pytest.mark.parametrize(
    "image",
    (np.zeros((0, 0), dtype=np.uint8), np.zeros((50, 50, 3), dtype=np.uint8)),
    ids=("empty", "color"),
)
def test_only_nonempty_grayscale_rasters_are_searched(image: np.ndarray) -> None:
    assert find_bubbles(image) is None
