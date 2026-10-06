"""Printed answer-bubble detection, the landmark every v4 recognition step is built on."""

from __future__ import annotations

from dataclasses import dataclass
from math import pi
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

# Detection runs where a bubble radius is about this many pixels, whatever the scan DPI.
_WORK_RADIUS: Final = 15.5
_FIRST_PASS_LONG_SIDE: Final = 2600
_MIN_WORK_RADIUS: Final = 6.0
_MAX_WORK_RADIUS: Final = 45.0
_MIN_CIRCULARITY: Final = 0.70
_MIN_DISK_FILL: Final = 0.60
_MIN_BUBBLES: Final = 20
_RADIUS_BAND: Final = (0.75, 1.35)
_GAMMAS: Final = (3.0, 5.0)


@dataclass(frozen=True, slots=True)
class Bubbles:
    """Circle-like printed marks found on one page, in source pixel coordinates."""

    centers: NDArray[np.float64]
    radii: NDArray[np.float64]
    radius: float

    def radius_samples(self) -> NDArray[np.float64]:
        return self.radii.copy()


def find_bubbles(gray: NDArray[np.uint8], *, enough: int | None = None) -> Bubbles | None:
    """Return the printed bubbles of a page, or ``None`` when too few circles exist.

    Faint print (a bright scanner setting or light toner) loses the thin gray rings, so
    detection darkens mid-tones with two gamma curves and keeps the richer result. With
    ``enough``, the second curve is skipped once the first already finds that many.
    """
    if gray.ndim != 2 or gray.size == 0:
        return None
    best: Bubbles | None = None
    for gamma in _GAMMAS:
        found = _bubbles(gray, gamma)
        if found is not None and (best is None or len(found.centers) > len(best.centers)):
            best = found
        if best is not None and enough is not None and len(best.centers) >= enough:
            break
    return best


def _bubbles(gray: NDArray[np.uint8], gamma: float) -> Bubbles | None:
    darkened = np.asarray(cv2.LUT(gray, _gamma_table(gamma)), dtype=np.uint8)
    scale = min(1.0, _FIRST_PASS_LONG_SIDE / max(gray.shape))
    found = _circles(darkened, scale)
    if found is None:
        return None
    radius = _dominant_radius(found[:, 2])
    if abs(radius - _WORK_RADIUS) > 0.3 * _WORK_RADIUS:
        # Re-run where the printed circles have the size the thresholds were tuned for.
        rescaled = _circles(darkened, scale * _WORK_RADIUS / radius)
        if rescaled is not None:
            found, scale = rescaled, scale * _WORK_RADIUS / radius
            radius = _dominant_radius(found[:, 2])
    low, high = _RADIUS_BAND
    kept = found[(found[:, 2] > low * radius) & (found[:, 2] < high * radius)]
    kept = _dedupe(kept, radius)
    if len(kept) < _MIN_BUBBLES:
        return None
    # The median of the kept rings is a continuous estimate; the histogram peak is binned.
    return Bubbles(kept[:, :2] / scale, kept[:, 2] / scale, float(np.median(kept[:, 2])) / scale)


def _gamma_table(gamma: float) -> NDArray[np.uint8]:
    levels = np.arange(256, dtype=np.float64) / 255.0
    return np.asarray(np.rint(255.0 * levels**gamma), dtype=np.uint8)


def _circles(gray: NDArray[np.uint8], scale: float) -> NDArray[np.float64] | None:
    work = (
        gray
        if abs(scale - 1.0) < 1e-9
        else cv2.resize(
            gray,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )
    )
    binary = cv2.adaptiveThreshold(
        work, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12
    )
    contours, _ = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    found: list[tuple[float, float, float]] = []
    low, high = 2 * _MIN_WORK_RADIUS, 2 * _MAX_WORK_RADIUS
    for contour in contours:
        _, _, width, height = cv2.boundingRect(contour)
        if not (low <= width <= high and low <= height <= high):
            continue
        if not 0.75 <= width / height <= 1.33:
            continue
        area = float(cv2.contourArea(contour))
        perimeter = float(cv2.arcLength(contour, True))
        if area < 60 or perimeter <= 0:
            continue
        (center_x, center_y), radius = cv2.minEnclosingCircle(contour)
        circularity = 4 * pi * area / (perimeter * perimeter)
        if circularity > _MIN_CIRCULARITY and area / (pi * radius * radius) > _MIN_DISK_FILL:
            found.append((float(center_x), float(center_y), float(radius)))
    if len(found) < _MIN_BUBBLES:
        return None
    return np.asarray(found, dtype=np.float64)


def _dominant_radius(radii: NDArray[np.float64]) -> float:
    histogram, edges = np.histogram(radii, bins=np.arange(_MIN_WORK_RADIUS, 4 * _MAX_WORK_RADIUS))
    peak = int(np.argmax(histogram))
    return float((edges[peak] + edges[peak + 1]) / 2)


def _dedupe(found: NDArray[np.float64], radius: float) -> NDArray[np.float64]:
    """Keep one circle per bubble (outer ring and inner hole contours coincide)."""
    ordered = found[np.argsort(-found[:, 2])]
    kept: list[NDArray[np.float64]] = []
    limit = (0.6 * radius) ** 2
    for item in ordered:
        if kept and np.min(np.sum((np.asarray(kept)[:, :2] - item[:2]) ** 2, axis=1)) <= limit:
            continue
        kept.append(item)
    return np.asarray(kept, dtype=np.float64).reshape(-1, 3)


__all__ = ["Bubbles", "find_bubbles"]
