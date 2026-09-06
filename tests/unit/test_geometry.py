from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.recognition.geometry import (
    PageContour,
    bounded_detection_copy,
    detect_page_contour,
    order_corners,
)
from omr_grader.recognition.normalization import (
    PixelRoi,
    RatioRoi,
    normalize_page,
    ratio_roi_to_pixels,
    transform_points,
)
from omr_grader.recognition.orientation import rotate_right_angle, select_orientation


def test_detection_copy_is_bounded_and_independent() -> None:
    source = np.zeros((100, 400), dtype=np.uint8)

    result = bounded_detection_copy(source, maximum_side=100)

    assert isinstance(result, Ok)
    assert result.value.pixels.shape == (25, 100)
    assert not result.value.pixels.flags.writeable
    source[0, 0] = 255
    assert result.value.pixels[0, 0] == 0


def test_page_contour_requires_margin_and_reports_no_contour() -> None:
    blank = np.full((300, 400), 255, dtype=np.uint8)
    assert isinstance(detect_page_contour(blank), Err)

    image = blank.copy()
    cv2.rectangle(image, (20, 20), (379, 279), 0, 3)
    found = detect_page_contour(image, minimum_margin_fraction=0.01)
    assert isinstance(found, Ok)
    assert found.value.margin_fraction >= 0.01

    edge_to_edge = blank.copy()
    cv2.rectangle(edge_to_edge, (0, 0), (399, 299), 0, 3)
    rejected = detect_page_contour(edge_to_edge, minimum_margin_fraction=0.01)
    assert isinstance(rejected, Err)


def test_perspective_normalization_is_lossless_and_transform_is_invertible() -> None:
    source = np.full((120, 160), 255, dtype=np.uint8)
    cv2.rectangle(source, (30, 25), (130, 95), 0, -1)
    contour = PageContour(
        np.array(((20, 20), (140, 15), (145, 105), (15, 100)), dtype=np.float32), 0.9, 0.1
    )

    result = normalize_page(source, contour, (200, 100))

    assert isinstance(result, Ok)
    raster = result.value
    decoded = cv2.imdecode(np.frombuffer(raster.png_bytes, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert np.array_equal(decoded, raster.pixels)
    original = np.array(((20.0, 20.0), (140.0, 15.0), (145.0, 105.0), (15.0, 100.0)))
    restored = transform_points(
        transform_points(original, raster.homography_forward), raster.homography_inverse
    )
    assert np.allclose(restored, original, atol=1e-6)


def test_ratio_roi_conversion_encloses_bounds_and_rejects_out_of_page_values() -> None:
    pixels = ratio_roi_to_pixels(RatioRoi(0.1, 0.2, 0.3, 0.4), (101, 99))
    assert pixels == PixelRoi(10, 19, 31, 41)
    assert pixels.x >= 0 and pixels.y >= 0
    assert pixels.x + pixels.width <= 101 and pixels.y + pixels.height <= 99
    with pytest.raises(ValueError):
        RatioRoi(0.9, 0.1, 0.2, 0.1)


def test_contour_selection_rejects_competing_and_threshold_cases() -> None:
    image = np.full((300, 400), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (185, 280), 0, 3)
    cv2.rectangle(image, (215, 20), (380, 280), 0, 3)
    competing = detect_page_contour(image, minimum_confidence=0.9)
    assert isinstance(competing, Err)
    assert competing.errors[0].code == "PAGE_NOT_FOUND"

    near_margin = np.full((300, 400), 255, dtype=np.uint8)
    cv2.rectangle(near_margin, (2, 2), (397, 297), 0, 3)
    rejected = detect_page_contour(near_margin, minimum_margin_fraction=0.02)
    assert isinstance(rejected, Err)
    assert rejected.errors[0].code == "PAGE_NOT_FOUND"


def test_orientation_considers_all_right_angles_and_refuses_ties_or_low_scores() -> None:
    image = np.arange(12, dtype=np.uint8).reshape(3, 4)
    assert np.array_equal(rotate_right_angle(image, 90), np.rot90(image, -1))
    assert np.array_equal(rotate_right_angle(image, 180), np.rot90(image, 2))
    assert np.array_equal(rotate_right_angle(image, 270), np.rot90(image, 1))

    def scorer(raster: np.ndarray[tuple[int, ...], np.dtype[np.uint8]]) -> float:
        return {0: 0.8, 8: 0.1, 11: 0.2, 3: 0.3}[int(raster[0, 0])]

    chosen = select_orientation(image, scorer)
    assert isinstance(chosen, Ok)
    assert chosen.value.rotation_degrees == 0
    assert tuple(item.rotation_degrees for item in chosen.value.scores) == (0, 90, 180, 270)

    assert isinstance(select_orientation(image, lambda _: 0.7), Err)
    assert isinstance(select_orientation(image, lambda _: 0.59), Err)
    boundary = select_orientation(image, lambda raster: 0.649 if int(raster[0, 0]) == 0 else 0.60)
    assert isinstance(boundary, Err)


def test_geometry_rejects_malformed_empty_and_oversize_rasters() -> None:
    for malformed in (
        np.zeros((4, 4), dtype=np.float32),
        np.zeros((4, 4, 2), dtype=np.uint8),
        np.empty((0, 4), dtype=np.uint8),
    ):
        result = bounded_detection_copy(malformed)
        assert isinstance(result, Err)
        assert result.errors[0].code == "PAGE_NOT_FOUND"

    oversized = np.broadcast_to(np.zeros((1, 1), dtype=np.uint8), (10_001, 10_000))
    result = bounded_detection_copy(oversized)
    assert isinstance(result, Err)
    assert result.errors[0].code == "PAGE_NOT_FOUND"

    contour = PageContour(np.array(((0, 0), (3, 0), (3, 3), (0, 3)), dtype=np.float32), 1, 0)
    assert isinstance(normalize_page(np.zeros((4, 4), dtype=np.float32), contour, (4, 4)), Err)
    with pytest.raises(ValueError, match="homography"):
        transform_points(np.array(((0, 0),), dtype=np.float32), np.eye(2, dtype=np.float32))


def test_corner_ordering_handles_diamonds_and_rotated_pages_without_reusing_points() -> None:
    diamond = np.array(((5, 0), (10, 5), (5, 10), (0, 5)), dtype=np.float32)
    rotated = np.array(((9, 2), (12, 9), (3, 12), (0, 5)), dtype=np.float32)

    assert np.array_equal(
        order_corners(diamond),
        np.array(((5, 0), (10, 5), (5, 10), (0, 5)), dtype=np.float32),
    )
    ordered = order_corners(rotated)
    assert np.array_equal(ordered[0], np.array((0, 5), dtype=np.float32))
    assert len({tuple(point) for point in ordered}) == 4
    assert (
        float(
            np.sum(
                ordered[:, 0] * np.roll(ordered[:, 1], -1)
                - ordered[:, 1] * np.roll(ordered[:, 0], -1)
            )
        )
        > 0
    )


@pytest.mark.parametrize(
    "corners",
    (
        np.array(((0, 0), (10, 0), (10, 10), (0, 0)), dtype=np.float32),
        np.array(((0, 0), (10, 0), (10, 10), (0.000001, 0.000001)), dtype=np.float32),
    ),
)
def test_corner_ordering_rejects_duplicate_and_near_zero_edges(corners: np.ndarray) -> None:
    with pytest.raises(ValueError):
        order_corners(corners)


def test_page_contour_canonicalizes_every_permutation_and_freezes_corners() -> None:
    expected = np.array(((10, 20), (110, 0), (120, 100), (0, 110)), dtype=np.float32)
    for permutation in itertools.permutations(expected):
        contour = PageContour(np.array(permutation, dtype=np.float32), 0.8, 0.1)
        assert np.array_equal(contour.corners, expected)
        assert not contour.corners.flags.writeable


@pytest.mark.parametrize(
    "corners, expected",
    (
        (
            np.array(((10, 10), (110, 25), (120, 120), (0, 100)), dtype=np.float32),
            np.array(((10, 10), (110, 25), (120, 120), (0, 100)), dtype=np.float32),
        ),
        (
            np.array(((10, 25), (110, 0), (120, 100), (0, 120)), dtype=np.float32),
            np.array(((10, 25), (110, 0), (120, 100), (0, 120)), dtype=np.float32),
        ),
    ),
)
def test_corner_ordering_uses_the_top_edge_for_both_mild_skew_directions(
    corners: np.ndarray, expected: np.ndarray
) -> None:
    for permutation in itertools.permutations(corners):
        assert np.array_equal(order_corners(np.array(permutation, dtype=np.float32)), expected)


@pytest.mark.parametrize(
    "corners",
    (
        np.array(((0, 0), (100, 0), (100, 0.00001), (0, 100)), dtype=np.float32),
        np.array(((0, 0), (10, 10), (20, 20), (0, 30)), dtype=np.float32),
    ),
)
def test_corner_ordering_rejects_ambiguous_or_degenerate_quadrilaterals(
    corners: np.ndarray,
) -> None:
    with pytest.raises(ValueError):
        order_corners(corners)
