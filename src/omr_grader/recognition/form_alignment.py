"""Align one scanned page to a profile frame using its printed bubbles.

Every page is fitted on hundreds of printed circles instead of the paper edge, so
skew, scale, offset and right-angle rotation are recovered together. A page that does
not fit well enough is reported instead of being read on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.profile import Profile, ProfileRegion
from omr_grader.recognition.bubbles import Bubbles, find_bubbles
from omr_grader.recognition.form_layout import (
    FormLayout,
    LatticeBlock,
    layout_from_bubbles,
    rotate_points,
)

ROTATIONS: Final = (0, 180, 90, 270)
_MATCH_RADIUS: Final = 0.45
_MAX_ANCHOR_ANGLE: Final = np.radians(10.0)
_MIN_INLIER_RATIO: Final = 0.5
_REFINEMENTS: Final = 4
_EXPLAINED_RADIUS: Final = 0.6


@dataclass(frozen=True, slots=True)
class PageAlignment:
    """Right-angle rotation plus homography from the rotated page to the profile frame."""

    rotation: int
    forward: NDArray[np.float64]
    inverse: NDArray[np.float64]
    inliers: int
    nodes: int
    residual: float
    bubble_radius: float
    runner_up_inliers: int
    unexplained: int = 0
    """Printed circles inside the frame that no form bubble explains (a larger form)."""

    @property
    def inlier_ratio(self) -> float:
        return self.inliers / self.nodes if self.nodes else 0.0

    @property
    def confidence(self) -> float:
        """0..1: share of bubbles matched, discounted when another rotation fits almost as well."""
        if not self.nodes:
            return 0.0
        margin = (self.inliers - self.runner_up_inliers) / self.nodes
        return float(max(0.0, min(1.0, self.inlier_ratio * min(1.0, 2 * margin + 0.5))))


def profile_nodes(profile: Profile) -> tuple[NDArray[np.float64], tuple[ProfileRegion, ...]]:
    """Bubble centers of every profile region in frame pixels, region by region."""
    if profile.page is None:
        raise ValueError("profile has no frame size")
    width, height = profile.page.source_width, profile.page.source_height
    centers: list[tuple[float, float]] = []
    for region in profile.regions:
        box = region.bbox_ratio
        x, y, w, h = float(box.x), float(box.y), float(box.w), float(box.h)
        for row in range(region.grid.rows):
            for col in range(region.grid.cols):
                centers.append(
                    (
                        (x + w * (col + 0.5) / region.grid.cols) * width,
                        (y + h * (row + 0.5) / region.grid.rows) * height,
                    )
                )
    return np.asarray(centers, dtype=np.float64), profile.regions


def align_page(gray: NDArray[np.uint8], profile: Profile) -> PageAlignment | None:
    """Return the best rotation and homography, or ``None`` when the form is not found."""
    if profile.page is None:
        return None
    frame_nodes, regions = profile_nodes(profile)
    bubbles = find_bubbles(gray, enough=int(0.8 * len(frame_nodes)))
    if bubbles is None:
        return None
    height, width = gray.shape
    results: list[tuple[int, NDArray[np.float64], float, int]] = []
    for rotation in ROTATIONS:
        rotated = rotate_points(bubbles.centers, rotation, width, height)
        fit = _fit(rotated, bubbles.radius, frame_nodes, regions)
        if fit is not None:
            results.append((rotation, *fit))
    if not results:
        return None
    results.sort(key=lambda item: -item[3])
    rotation, forward, residual, inliers = results[0]
    runner_up = results[1][3] if len(results) > 1 else 0
    if inliers < _MIN_INLIER_RATIO * len(frame_nodes):
        return None
    scale = float(np.sqrt(abs(np.linalg.det(_jacobian(forward, frame_nodes.mean(axis=0))))))
    bubble_radius = bubbles.radius * scale
    mapped = _apply(forward, rotate_points(bubbles.centers, rotation, width, height))
    frame_width, frame_height = profile.page.source_width, profile.page.source_height
    inside = mapped[
        (mapped[:, 0] >= 0)
        & (mapped[:, 0] < frame_width)
        & (mapped[:, 1] >= 0)
        & (mapped[:, 1] < frame_height)
    ]
    explained = _CircleIndex(frame_nodes, _EXPLAINED_RADIUS * bubble_radius).covers(inside)
    return PageAlignment(
        rotation,
        forward,
        _inverse(forward),
        inliers,
        len(frame_nodes),
        residual,
        bubble_radius,
        runner_up,
        int(np.count_nonzero(~explained)),
    )


def _fit(
    points: NDArray[np.float64],
    radius: float,
    frame_nodes: NDArray[np.float64],
    regions: tuple[ProfileRegion, ...],
) -> tuple[NDArray[np.float64], float, int] | None:
    """Anchor on one matching lattice, then refine a homography on every printed circle."""
    layout = layout_from_bubbles(Bubbles(points, np.full(len(points), radius), radius), None)
    if layout is None:
        return None
    index = _CircleIndex(points, _MATCH_RADIUS * radius)
    # Candidate anchors are compared on every fourth bubble; only the winner is refined.
    sample = frame_nodes[::4]
    best: tuple[int, NDArray[np.float64]] | None = None
    region_slices = _region_slices(regions)
    for region, (start, stop) in zip(regions, region_slices, strict=True):
        target = frame_nodes[start:stop]
        for block in _same_shape(layout, region):
            source = block.nodes.reshape(-1, 2)
            similarity = _similarity(target, source)
            if similarity is None:
                continue
            guess = _inverse(similarity)
            inliers = len(index.pairs(_apply(similarity, sample)))
            if best is None or inliers > best[0]:
                best = (inliers, guess)
    if best is None:
        return None
    forward = best[1]
    residual = float("inf")
    inliers = 0
    for _ in range(_REFINEMENTS):
        pairs = index.pairs(_apply(_inverse(forward), frame_nodes))
        if len(pairs) < 8:
            return None
        matched_points = points[[point for point, _ in pairs]]
        matched_nodes = frame_nodes[[node for _, node in pairs]]
        homography, _ = cv2.findHomography(matched_points, matched_nodes, cv2.RANSAC, 0.35 * radius)
        if homography is None:
            return None
        forward = homography.astype(np.float64)
        mapped = _apply(forward, matched_points)
        errors = np.hypot(*(mapped - matched_nodes).T)
        residual = float(np.median(errors))
        inliers = len(index.pairs(_apply(_inverse(forward), frame_nodes)))
    return forward, residual, inliers


def _same_shape(layout: FormLayout, region: ProfileRegion) -> list[LatticeBlock]:
    return [
        block
        for block in layout.blocks
        if block.cols == region.grid.cols and block.rows == region.grid.rows
    ]


def _region_slices(regions: tuple[ProfileRegion, ...]) -> list[tuple[int, int]]:
    slices: list[tuple[int, int]] = []
    cursor = 0
    for region in regions:
        count = region.grid.rows * region.grid.cols
        slices.append((cursor, cursor + count))
        cursor += count
    return slices


def _similarity(
    source: NDArray[np.float64], target: NDArray[np.float64]
) -> NDArray[np.float64] | None:
    """Rotation+scale+translation from source to target, refusing large rotations."""
    if len(source) != len(target) or len(source) < 3:
        return None
    matrix, _ = cv2.estimateAffinePartial2D(source, target, method=cv2.LMEDS)
    if matrix is None:
        return None
    angle = float(np.arctan2(matrix[1, 0], matrix[0, 0]))
    if abs(angle) > _MAX_ANCHOR_ANGLE:
        return None
    return np.vstack([matrix, [0.0, 0.0, 1.0]])


class _CircleIndex:
    """Printed circles bucketed on a grid one match radius wide, for nearest lookups."""

    def __init__(self, points: NDArray[np.float64], limit: float) -> None:
        self._points = points
        self._limit = limit
        self._cell = max(limit, 1.0)
        self._buckets: dict[tuple[int, int], list[int]] = {}
        for index in range(len(points)):
            key = (int(points[index, 0] // self._cell), int(points[index, 1] // self._cell))
            self._buckets.setdefault(key, []).append(index)

    def pairs(self, page_nodes: NDArray[np.float64]) -> list[tuple[int, int]]:
        """Mutual nearest printed circle within the match radius for each projected node."""
        node_best: dict[int, tuple[float, int]] = {}
        point_best: dict[int, tuple[float, int]] = {}
        for node in range(len(page_nodes)):
            x, y = float(page_nodes[node, 0]), float(page_nodes[node, 1])
            if not (np.isfinite(x) and np.isfinite(y)):
                continue
            gx, gy = int(x // self._cell), int(y // self._cell)
            for bx in (gx - 1, gx, gx + 1):
                for by in (gy - 1, gy, gy + 1):
                    for point in self._buckets.get((bx, by), ()):
                        dx = float(self._points[point, 0]) - x
                        dy = float(self._points[point, 1]) - y
                        distance = (dx * dx + dy * dy) ** 0.5
                        if distance >= self._limit:
                            continue
                        if node not in node_best or distance < node_best[node][0]:
                            node_best[node] = (distance, point)
                        if point not in point_best or distance < point_best[point][0]:
                            point_best[point] = (distance, node)
        return [
            (point, node) for node, (_, point) in node_best.items() if point_best[point][1] == node
        ]

    def covers(self, queries: NDArray[np.float64]) -> NDArray[np.bool_]:
        """Whether each query lies within the match radius of some indexed circle."""
        hits = np.zeros(len(queries), dtype=bool)
        limit = self._limit * self._limit
        for row in range(len(queries)):
            x, y = float(queries[row, 0]), float(queries[row, 1])
            if not (np.isfinite(x) and np.isfinite(y)):
                continue
            gx, gy = int(x // self._cell), int(y // self._cell)
            hits[row] = any(
                (float(self._points[point, 0]) - x) ** 2 + (float(self._points[point, 1]) - y) ** 2
                < limit
                for bx in (gx - 1, gx, gx + 1)
                for by in (gy - 1, gy, gy + 1)
                for point in self._buckets.get((bx, by), ())
            )
        return hits


def _inverse(matrix: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.asarray(np.linalg.inv(matrix), dtype=np.float64)


def _apply(matrix: NDArray[np.float64], points: NDArray[np.float64]) -> NDArray[np.float64]:
    homogeneous = np.c_[points, np.ones(len(points))] @ matrix.T
    return homogeneous[:, :2] / homogeneous[:, 2:3]


def _jacobian(matrix: NDArray[np.float64], frame_point: NDArray[np.float64]) -> NDArray[np.float64]:
    """Local linear part of the forward map, evaluated at the page point mapped to ``frame_point``."""
    page_point = _apply(_inverse(matrix), frame_point[None, :])[0]
    step = 1.0
    base = _apply(matrix, page_point[None, :])[0]
    dx = _apply(matrix, (page_point + [step, 0.0])[None, :])[0] - base
    dy = _apply(matrix, (page_point + [0.0, step])[None, :])[0] - base
    return np.column_stack([dx, dy]) / step


__all__ = ["ROTATIONS", "PageAlignment", "align_page", "profile_nodes"]
