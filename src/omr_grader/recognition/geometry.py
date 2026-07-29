"""Bounded page detection and immutable page geometry values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

Image = NDArray[np.uint8]
PointArray = NDArray[np.float32]
_MAX_DETECTION_SIDE = 1_000
_MAX_DETECTION_PIXELS = _MAX_DETECTION_SIDE * _MAX_DETECTION_SIDE


@dataclass(frozen=True, slots=True)
class DetectionImage:
    """A bounded, private copy used only by page detection."""

    pixels: Image
    scale_x: float
    scale_y: float

    def __post_init__(self) -> None:
        if (
            self.pixels.dtype != np.uint8
            or self.pixels.ndim not in (2, 3)
            or self.pixels.ndim == 3
            and self.pixels.shape[2] not in (3, 4)
        ):
            raise ValueError("detection raster must be uint8 gray or color")
        if (
            self.pixels.shape[0] * self.pixels.shape[1] > _MAX_DETECTION_PIXELS
            or self.scale_x <= 0
            or self.scale_y <= 0
        ):
            raise ValueError("invalid bounded detection raster")
        pixels = self.pixels.copy()
        pixels.setflags(write=False)
        object.__setattr__(self, "pixels", pixels)


@dataclass(frozen=True, slots=True)
class PageContour:
    """Clockwise top-left ordered page corners in source-pixel coordinates."""

    corners: PointArray
    confidence: float
    margin_fraction: float

    def __post_init__(self) -> None:
        if self.corners.shape != (4, 2) or self.corners.dtype != np.float32:
            raise ValueError("page contour must contain four float32 corners")
        if not np.isfinite(self.corners).all() or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("invalid page contour confidence")
        if not 0.0 <= self.margin_fraction <= 1.0:
            raise ValueError("invalid page contour margin")
        corners = order_corners(self.corners).copy()
        corners.setflags(write=False)
        object.__setattr__(self, "corners", corners)


def bounded_detection_copy(
    image: Image, *, maximum_side: int = _MAX_DETECTION_SIDE
) -> Result[DetectionImage]:
    """Copy and downsample an input raster before contour processing.

    This prevents untrusted source dimensions from forcing OpenCV detection work at
    full resolution. The original image is never retained or mutated.
    """
    if maximum_side < 64:
        raise ValueError("maximum_side must be at least 64")
    if (
        image.dtype != np.uint8
        or image.ndim not in (2, 3)
        or image.ndim == 3
        and image.shape[2] not in (3, 4)
    ):
        return Err((_error("PAGE_NOT_FOUND", "unsupported raster format"),))
    height, width = image.shape[:2]
    if height < 2 or width < 2 or height * width > 100_000_000:
        return Err(
            (_error("PAGE_NOT_FOUND", "raster dimensions are invalid or exceed the input bound"),)
        )
    factor = min(1.0, maximum_side / max(height, width))
    target_width = max(1, round(width * factor))
    target_height = max(1, round(height * factor))
    if target_width * target_height > _MAX_DETECTION_PIXELS:
        return Err((_error("PAGE_NOT_FOUND", "detection raster exceeds the pixel bound"),))
    copied = _uint8_raster(
        cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_AREA)
        if factor < 1
        else image.copy()
    )
    return Ok(DetectionImage(copied, width / target_width, height / target_height))


def detect_page_contour(
    image: Image,
    *,
    expected_aspect_ratio: float | None = None,
    minimum_confidence: float = 0.55,
    minimum_margin_fraction: float = 0.002,
    candidate_margin: float = 0.05,
) -> Result[PageContour]:
    """Find one well-separated, page-like quadrilateral or require manual review."""
    if (
        not 0.0 <= minimum_confidence <= 1.0
        or not 0.0 <= minimum_margin_fraction <= 0.25
        or not 0.0 <= candidate_margin <= 1.0
        or expected_aspect_ratio is not None
        and expected_aspect_ratio <= 0
    ):
        raise ValueError("invalid page-detection thresholds")
    bounded = bounded_detection_copy(image)
    if isinstance(bounded, Err):
        return bounded
    detection = bounded.value
    gray = _gray_raster(
        cv2.cvtColor(detection.pixels, cv2.COLOR_BGR2GRAY)
        if detection.pixels.ndim == 3
        else detection.pixels
    )
    blurred = _gray_raster(cv2.GaussianBlur(gray, (5, 5), 0))
    edges = _gray_raster(cv2.Canny(blurred, 50, 150))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    height, width = gray.shape
    image_area = float(width * height)
    candidates: list[tuple[float, PointArray, float]] = []
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue
        try:
            corners = order_corners(_float32_points(approximation.reshape(4, 2), shape=(4, 2)))
        except ValueError:
            continue
        area = abs(float(cv2.contourArea(corners)))
        if area <= 0:
            continue
        area_fraction = area / image_area
        if area_fraction < 0.30:
            continue
        margin_fraction = _margin_fraction(corners, width, height)
        angle_score = _right_angle_score(corners)
        profile_score = _aspect_score(corners, expected_aspect_ratio)
        area_score = min(1.0, area_fraction / 0.70)
        margin_score = min(1.0, margin_fraction / max(minimum_margin_fraction, 1e-6))
        confidence = (
            0.30 * angle_score
            + 0.15  # Convexity is an explicit plausibility requirement above.
            + 0.20 * profile_score
            + 0.25 * area_score
            + 0.10 * margin_score
        )
        candidates.append((confidence, corners, margin_fraction))
    if not candidates:
        return Err((_error("PAGE_NOT_FOUND", "no quadrilateral page contour found"),))
    ranked = sorted(candidates, key=lambda item: -item[0])
    winner = ranked[0]
    runner_up = next(
        (
            candidate
            for candidate in ranked[1:]
            if not _same_contour(candidate[1], winner[1], width, height)
        ),
        None,
    )
    if (
        winner[0] < minimum_confidence
        or winner[2] <= minimum_margin_fraction
        or runner_up is not None
        and winner[0] - runner_up[0] <= candidate_margin
    ):
        return Err((_error("PAGE_NOT_FOUND", "page contour confidence or ranking is ambiguous"),))
    source_corners = _float32_points(
        winner[1] * np.array((detection.scale_x, detection.scale_y), dtype=np.float32),
        shape=(4, 2),
    )
    return Ok(PageContour(source_corners, winner[0], winner[2]))


def order_corners(corners: PointArray) -> PointArray:
    """Return corners as top-left, top-right, bottom-right, bottom-left."""
    if corners.shape != (4, 2) or corners.dtype != np.float32 or not np.isfinite(corners).all():
        raise ValueError("exactly four finite float32 corners are required")

    points = corners.astype(np.float64)
    span = float(np.max(np.ptp(points, axis=0)))
    minimum_distance = max(1e-6, span * 1e-6)
    pairwise_distances = np.linalg.norm(points[:, None] - points[None, :], axis=2)
    np.fill_diagonal(pairwise_distances, np.inf)
    if float(np.min(pairwise_distances)) <= minimum_distance:
        raise ValueError("page corners must be distinct")

    centroid = np.mean(points, axis=0)
    angles = np.arctan2(points[:, 1] - centroid[1], points[:, 0] - centroid[0])
    angle_indices = np.argsort(angles, kind="stable")
    ordered = points[angle_indices]
    ordered_angles = angles[angle_indices]
    angle_gaps = np.diff(np.concatenate((ordered_angles, ordered_angles[:1] + 2 * np.pi)))
    if float(np.min(angle_gaps)) <= 1e-6:
        raise ValueError("page corners must have unique centroid angles")

    edges = np.roll(ordered, -1, axis=0) - ordered
    if np.any(np.linalg.norm(edges, axis=1) <= minimum_distance):
        raise ValueError("page corners must not contain near-zero edges")
    turns = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    minimum_turn = span * span * 1e-6
    if np.any(np.abs(turns) <= minimum_turn) or not (
        np.all(turns > minimum_turn) or np.all(turns < -minimum_turn)
    ):
        raise ValueError("page corners must form a non-degenerate convex quadrilateral")

    signed_area = float(
        np.sum(
            ordered[:, 0] * np.roll(ordered[:, 1], -1) - ordered[:, 1] * np.roll(ordered[:, 0], -1)
        )
    )
    if abs(signed_area) <= minimum_turn:
        raise ValueError("page corners must form a non-degenerate quadrilateral")
    if signed_area < 0:
        ordered = ordered[::-1]
    edges = np.roll(ordered, -1, axis=0) - ordered

    edge_midpoints = (ordered[:, 1] + np.roll(ordered[:, 1], -1)) / 2
    top_tolerance = max(1e-6, span * 1e-6)
    top_edges = np.flatnonzero(edge_midpoints <= np.min(edge_midpoints) + top_tolerance)
    if len(top_edges) == 1:
        top_edge = int(top_edges[0])
    elif len(top_edges) == 2:
        top_slopes = edges[top_edges, 1]
        descending = top_edges[top_slopes > top_tolerance]
        if len(descending) != 1:
            raise ValueError("page corners have an ambiguous top edge")
        top_edge = int(descending[0])
    else:
        raise ValueError("page corners have an ambiguous top edge")

    top_left = ordered[top_edge]
    top_right = ordered[(top_edge + 1) % 4]
    if top_left[0] >= top_right[0] - minimum_distance:
        raise ValueError("page corners have an ambiguous top edge")
    return np.roll(ordered, -top_edge, axis=0).astype(np.float32)


def _margin_fraction(corners: PointArray, width: int, height: int) -> float:
    left = float(np.min(corners[:, 0]))
    top = float(np.min(corners[:, 1]))
    right = float(width - 1 - np.max(corners[:, 0]))
    bottom = float(height - 1 - np.max(corners[:, 1]))
    return max(0.0, min(left, top, right, bottom) / min(width, height))


def _right_angle_score(corners: PointArray) -> float:
    vectors = np.roll(corners, -1, axis=0) - corners
    lengths = np.linalg.norm(vectors, axis=1)
    if np.any(lengths <= 0):
        return 0.0
    cosines = np.abs(
        np.sum(vectors * np.roll(vectors, -1, axis=0), axis=1) / (lengths * np.roll(lengths, -1))
    )
    return max(0.0, 1.0 - float(np.mean(cosines)))


def _aspect_score(corners: PointArray, expected: float | None) -> float:
    if expected is None:
        return 1.0
    top = float(np.linalg.norm(corners[1] - corners[0]))
    bottom = float(np.linalg.norm(corners[2] - corners[3]))
    left = float(np.linalg.norm(corners[3] - corners[0]))
    right = float(np.linalg.norm(corners[2] - corners[1]))
    width = (top + bottom) / 2
    height = (left + right) / 2
    if width <= 0 or height <= 0:
        return 0.0
    ratio = width / height
    return max(0.0, 1.0 - abs(ratio - expected) / max(ratio, expected))


def _same_contour(first: PointArray, second: PointArray, width: int, height: int) -> bool:
    diagonal = float(np.hypot(width, height))
    if diagonal <= 0:
        return False
    mean_distance = float(np.mean(np.linalg.norm(first - second, axis=1))) / diagonal
    first_min, first_max = np.min(first, axis=0), np.max(first, axis=0)
    second_min, second_max = np.min(second, axis=0), np.max(second, axis=0)
    intersection_size = np.maximum(
        0.0, np.minimum(first_max, second_max) - np.maximum(first_min, second_min)
    )
    intersection = float(intersection_size[0] * intersection_size[1])
    first_area = float(np.prod(first_max - first_min))
    second_area = float(np.prod(second_max - second_min))
    union = first_area + second_area - intersection
    overlap = intersection / union if union > 0 else 0.0
    return mean_distance <= 0.03 or overlap >= 0.90


def _uint8_raster(value: object) -> Image:
    raster = np.asarray(value)
    if (
        raster.dtype != np.uint8
        or raster.ndim not in (2, 3)
        or raster.ndim == 3
        and raster.shape[2] not in (3, 4)
    ):
        raise ValueError("raster must be uint8 gray or BGR(A)")
    return cast(Image, raster)


def _gray_raster(value: object) -> Image:
    raster = _uint8_raster(value)
    if raster.ndim != 2:
        raise ValueError("raster must be grayscale")
    return raster


def _float32_points(value: object, *, shape: tuple[int, int]) -> PointArray:
    points = np.asarray(value, dtype=np.float32)
    if points.shape != shape or not np.isfinite(points).all():
        raise ValueError("points must have the expected finite float32 shape")
    return points


def _error(code: str, detail: str) -> ErrorInfo:
    return ErrorInfo(
        code,
        f"error.{code.lower()}",
        context={"manual_review": True, "detail": detail},
    )
