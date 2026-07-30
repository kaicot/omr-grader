"""Deterministic, pure orientation selection for scanned OMR pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

_GRAY = NDArray[np.uint8]
Raster = NDArray[np.uint8]
_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True, slots=True)
class OrientationScore:
    """The score assigned to one clockwise rotation."""

    rotation_degrees: int
    score: float

    def __post_init__(self) -> None:
        if self.rotation_degrees not in _ROTATIONS or not np.isfinite(self.score):
            raise ValueError("invalid orientation score")


@dataclass(frozen=True, slots=True)
class OrientationDecision:
    """An unambiguous orientation selected from all four right-angle rotations."""

    rotation_degrees: int
    confidence: float
    scores: tuple[OrientationScore, OrientationScore, OrientationScore, OrientationScore]

    def __post_init__(self) -> None:
        if self.rotation_degrees not in _ROTATIONS or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("invalid orientation decision")
        if tuple(item.rotation_degrees for item in self.scores) != _ROTATIONS:
            raise ValueError("orientation scores must cover 0, 90, 180, and 270 degrees")


def rotate_right_angle(image: Raster, rotation_degrees: int) -> Raster:
    """Return a fresh clockwise right-angle rotation without interpolation."""
    raster = _uint8_raster(image)
    if rotation_degrees not in _ROTATIONS:
        raise ValueError("rotation_degrees must be one of 0, 90, 180, 270")
    if rotation_degrees == 0:
        return raster.copy()
    code = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }[rotation_degrees]
    return _uint8_raster(cv2.rotate(raster, code))


def select_orientation(
    image: _GRAY,
    scorer: Callable[[_GRAY], float],
    *,
    minimum_confidence: float = 0.60,
    tie_margin: float = 0.05,
    preferred_rotation: int | None = None,
) -> Result[OrientationDecision]:
    """Score all rotations and return a decision only when it is unambiguous.

    ``scorer`` must return a normalized [0, 1] compatibility score where larger is
    better.  Ties and weak winners become a typed ``ORIENTATION_UNCERTAIN`` error,
    so callers cannot accidentally treat an arbitrary rotation as normal.
    """
    if image.ndim != 2 or image.dtype != np.uint8:
        return Err((_error("ORIENTATION_UNCERTAIN", "image must be an 8-bit grayscale raster"),))
    if not 0.0 <= minimum_confidence <= 1.0 or not 0.0 <= tie_margin <= 1.0:
        raise ValueError("confidence thresholds must be in [0, 1]")
    if preferred_rotation is not None and preferred_rotation not in _ROTATIONS:
        raise ValueError("preferred_rotation must be one of 0, 90, 180, 270 or None")

    scores: list[OrientationScore] = []
    for rotation in _ROTATIONS:
        value = float(scorer(rotate_right_angle(image, rotation)))
        if not np.isfinite(value) or not 0.0 <= value <= 1.0:
            return Err(
                (_error("ORIENTATION_UNCERTAIN", "orientation scorer returned an invalid score"),)
            )
        scores.append(OrientationScore(rotation, value))
    ranked = sorted(scores, key=lambda item: (-item.score, item.rotation_degrees))
    winner, runner_up = ranked[:2]
    confidence = winner.score
    winner_margin = winner.score - runner_up.score
    if confidence < minimum_confidence or winner_margin <= tie_margin:
        if preferred_rotation is not None and confidence >= minimum_confidence:
            by_rotation = {item.rotation_degrees: item for item in scores}
            preferred = by_rotation[preferred_rotation]
            opposite = by_rotation[(preferred_rotation + 180) % 360]
            perpendicular = (
                by_rotation[(preferred_rotation + 90) % 360],
                by_rotation[(preferred_rotation + 270) % 360],
            )
            axis_score = max(preferred.score, opposite.score)
            cross_axis_score = max(item.score for item in perpendicular)
            if (
                preferred.score >= minimum_confidence
                and abs(preferred.score - opposite.score) > 1e-6
                and axis_score - preferred.score <= tie_margin
                and axis_score - cross_axis_score > tie_margin
            ):
                return Ok(
                    OrientationDecision(
                        preferred_rotation,
                        preferred.score,
                        (scores[0], scores[1], scores[2], scores[3]),
                    )
                )
        return Err(
            (
                _error(
                    "ORIENTATION_UNCERTAIN",
                    "orientation confidence is low or ambiguous; manual review required",
                ),
            )
        )
    return Ok(
        OrientationDecision(
            winner.rotation_degrees,
            confidence,
            (scores[0], scores[1], scores[2], scores[3]),
        )
    )


def _uint8_raster(value: object) -> Raster:
    raster = np.asarray(value)
    if (
        raster.dtype != np.uint8
        or raster.ndim not in (2, 3)
        or raster.ndim == 3
        and raster.shape[2] not in (3, 4)
    ):
        raise ValueError("image must be an 8-bit grayscale or BGR(A) raster")
    return cast(Raster, raster)


def _error(code: str, message: str) -> ErrorInfo:
    return ErrorInfo(
        code,
        f"error.{code.lower()}",
        context={"manual_review": True, "detail": message},
    )
