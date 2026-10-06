"""Content-based global registration of profile grids to printed scan geometry."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.profile import BoundingBoxRatio, Profile, ProfileRegion

_MIN_SCORE_GAIN: Final = 1.15
_SCALE_STEPS: Final = tuple(value / 500 for value in range(450, 531))
_MAX_OFFSET_RATIO: Final = 0.06
_ALTERNATIVE_SCALE_SEPARATION: Final = 0.012
_ALTERNATIVE_OFFSET_SEPARATION: Final = 8


def register_profile_grid(image: NDArray[np.uint8], profile: Profile) -> Profile:
    """Fit one bounded scale/translation transform to every configured region."""
    if profile.page is None or not profile.regions or image.size == 0:
        return profile
    ink = _ink(image)
    height, width = ink.shape[:2]

    scale_x, offset_x = _fit_horizontal(ink, profile.regions, width, height)
    scale_y, offset_y = _fit_vertical(
        ink,
        profile.regions,
        width,
        height,
        scale_x,
        offset_x,
    )
    if (scale_x, offset_x, scale_y, offset_y) == (1.0, 0, 1.0, 0):
        return profile
    moved = _moved(profile, width, height, scale_x, offset_x, scale_y, offset_y)
    return profile if moved is None else moved


def registration_candidates(
    image: NDArray[np.uint8], profile: Profile, limit: int = 4
) -> tuple[Profile, ...]:
    """Return the strongest distinct horizontal fits, each with its own vertical fit.

    The printed table repeats its vertical rules, so a grid shrunk by about a tenth can
    outscore the true fit. Callers re-read a weak page with these alternatives and keep
    one only when it reads confidently.
    """
    if profile.page is None or not profile.regions or image.size == 0 or limit <= 0:
        return ()
    ink = _ink(image)
    height, width = ink.shape[:2]
    projection, lines = _horizontal_signal(ink, profile.regions, width, height)
    scales = np.asarray(_SCALE_STEPS, dtype=np.float64)
    reach = round(width * _MAX_OFFSET_RATIO)
    offsets = np.arange(-reach, reach + 1)
    coordinates = np.rint(
        scales[:, None, None] * np.asarray(lines, dtype=np.float64)[None, None, :]
        + offsets[None, :, None]
    ).astype(np.int64)
    inside = ((coordinates >= 0) & (coordinates < projection.size)).all(axis=2)
    sampled = projection[np.clip(coordinates, 0, projection.size - 1)].sum(axis=2)
    scores = np.where(inside, sampled, -1.0)
    fits: list[tuple[float, int]] = []
    candidates: list[Profile] = []
    for flat in np.argsort(scores, axis=None, kind="stable")[::-1]:
        scale_index, offset_index = np.unravel_index(flat, scores.shape)
        if scores[scale_index, offset_index] <= 0 or len(fits) == limit:
            break
        scale_x, offset_x = float(scales[scale_index]), int(offsets[offset_index])
        if any(
            abs(scale_x - scale) <= _ALTERNATIVE_SCALE_SEPARATION
            and abs(offset_x - offset) <= _ALTERNATIVE_OFFSET_SEPARATION
            for scale, offset in fits
        ):
            continue
        fits.append((scale_x, offset_x))
        scale_y, offset_y = _fit_vertical(
            ink, profile.regions, width, height, scale_x, offset_x
        )
        moved = _moved(profile, width, height, scale_x, offset_x, scale_y, offset_y)
        if moved is not None:
            candidates.append(moved)
    return tuple(candidates)


def _ink(image: NDArray[np.uint8]) -> NDArray[np.float32]:
    gray = (
        image
        if image.ndim == 2
        else np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), dtype=np.uint8)
    )
    _, thresholded = cv2.threshold(
        gray,
        0,
        1,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )
    return np.asarray(thresholded, dtype=np.float32)


def _moved(
    profile: Profile,
    width: int,
    height: int,
    scale_x: float,
    offset_x: int,
    scale_y: float,
    offset_y: int,
) -> Profile | None:
    regions: list[ProfileRegion] = []
    for region in profile.regions:
        box = region.bbox_ratio
        transformed = BoundingBoxRatio(
            _decimal(scale_x * float(box.x) + offset_x / width),
            _decimal(scale_y * float(box.y) + offset_y / height),
            _decimal(scale_x * float(box.w)),
            _decimal(scale_y * float(box.h)),
        )
        if (
            transformed.x < 0
            or transformed.y < 0
            or transformed.w <= 0
            or transformed.h <= 0
            or transformed.x + transformed.w > 1
            or transformed.y + transformed.h > 1
        ):
            return None
        regions.append(replace(region, bbox_ratio=transformed))
    return replace(profile, regions=tuple(regions))


def _horizontal_signal(
    ink: NDArray[np.float32],
    regions: tuple[ProfileRegion, ...],
    width: int,
    height: int,
) -> tuple[NDArray[np.float32], tuple[float, ...]]:
    minimum_y = max(
        0,
        round(min(float(region.bbox_ratio.y) for region in regions) * height) - 20,
    )
    maximum_y = min(
        height,
        round(
            max(float(region.bbox_ratio.y + region.bbox_ratio.h) for region in regions)
            * height
        )
        + 20,
    )
    projection = ink[minimum_y:maximum_y].mean(axis=0)
    projection = np.convolve(projection, np.ones(5, dtype=np.float32) / 5, mode="same")
    lines = tuple(
        coordinate
        for region in regions
        for coordinate in (
            float(region.bbox_ratio.x) * width,
            float(region.bbox_ratio.x + region.bbox_ratio.w) * width,
        )
    )
    return projection, lines


def _fit_horizontal(
    ink: NDArray[np.float32],
    regions: tuple[ProfileRegion, ...],
    width: int,
    height: int,
) -> tuple[float, int]:
    projection, lines = _horizontal_signal(ink, regions, width, height)
    limit = round(width * _MAX_OFFSET_RATIO)
    offsets = range(-limit, limit + 1, 2)
    baseline = _line_score(projection, lines, 1.0, 0)
    best = max(
        (
            (_line_score(projection, lines, scale, offset), scale, offset)
            for scale in _SCALE_STEPS
            for offset in offsets
        ),
        key=lambda item: (round(item[0], 6), -abs(item[1] - 1.0), -abs(item[2])),
    )
    if baseline <= 0 or best[0] < baseline * _MIN_SCORE_GAIN:
        return 1.0, 0
    return best[1], best[2]


def _fit_vertical(
    ink: NDArray[np.float32],
    regions: tuple[ProfileRegion, ...],
    width: int,
    height: int,
    scale_x: float,
    offset_x: int,
) -> tuple[float, int]:
    answer_regions = tuple(region for region in regions if region.kind == "answer")
    if not answer_regions:
        return 1.0, 0
    boundary_signals: list[NDArray[np.float32]] = []
    for region in answer_regions:
        box = region.bbox_ratio
        left = max(0, round(scale_x * float(box.x) * width + offset_x))
        right = min(width, round(scale_x * float(box.x + box.w) * width + offset_x))
        if right - left < 8:
            return 1.0, 0
        for boundary in (left, right):
            boundary_signals.append(
                ink[:, max(0, boundary - 2) : min(width, boundary + 3)].mean(axis=1)
            )
    signal = np.mean(boundary_signals, axis=0)
    smooth = np.convolve(signal, np.ones(11, dtype=np.float32) / 11, mode="same")
    maximum = float(smooth.max())
    if maximum <= 0:
        return 1.0, 0
    scaled = np.asarray(np.clip(smooth / maximum * 255, 0, 255), dtype=np.uint8)
    _, mask = cv2.threshold(
        scaled,
        0,
        255,
        cv2.THRESH_BINARY | cv2.THRESH_OTSU,
    )
    mask = cv2.morphologyEx(
        mask.reshape(-1, 1),
        cv2.MORPH_CLOSE,
        np.ones((31, 1), dtype=np.uint8),
    ).ravel()
    indices = np.flatnonzero(mask)
    if indices.size == 0:
        return 1.0, 0
    starts = np.r_[0, np.flatnonzero(np.diff(indices) > 1) + 1]
    ends = np.r_[starts[1:] - 1, indices.size - 1]
    runs = tuple(
        (int(indices[start]), int(indices[end]))
        for start, end in zip(starts, ends, strict=True)
    )
    detected_top, detected_bottom = max(
        runs,
        key=lambda run: run[1] - run[0],
    )
    endpoint_inset = max(1, round(height * 0.004))
    detected_top += endpoint_inset
    detected_bottom -= endpoint_inset
    if detected_bottom - detected_top < height * 0.50:
        return 1.0, 0

    expected_tops = tuple(
        float(region.bbox_ratio.y) * height for region in answer_regions
    )
    expected_bottoms = tuple(
        float(region.bbox_ratio.y + region.bbox_ratio.h) * height
        for region in answer_regions
    )
    expected_top = float(np.median(expected_tops))
    expected_bottom = float(np.median(expected_bottoms))
    scale = (detected_bottom - detected_top) / (expected_bottom - expected_top)
    if not 0.90 <= scale <= 1.06:
        return 1.0, 0
    expected_center = (expected_top + expected_bottom) / 2
    detected_center = (detected_top + detected_bottom) / 2
    offset = round(detected_center - scale * expected_center)
    if abs(offset) > round(height * _MAX_OFFSET_RATIO):
        return 1.0, 0
    return scale, offset


def _line_score(
    projection: NDArray[np.float32],
    lines: tuple[float, ...],
    scale: float,
    offset: int,
    weights: tuple[float, ...] | None = None,
) -> float:
    coordinates = tuple(round(scale * line + offset) for line in lines)
    if any(coordinate < 0 or coordinate >= projection.size for coordinate in coordinates):
        return -1.0
    line_weights = weights if weights is not None else (1.0,) * len(coordinates)
    return float(
        sum(
            float(projection[coordinate]) * weight
            for coordinate, weight in zip(coordinates, line_weights, strict=True)
        )
    )


def _decimal(value: float) -> Decimal:
    return Decimal(f"{value:.8f}")
