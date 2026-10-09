"""Lossless right-angle rotation of scanned OMR pages.

Choosing the rotation is part of bubble alignment (``form_alignment``): every right
angle is tried there and the one whose printed bubbles fit the form wins.
"""

from __future__ import annotations

from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

Raster = NDArray[np.uint8]
_ROTATIONS = (0, 90, 180, 270)


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


def _uint8_raster(value: object) -> Raster:
    raster = np.asarray(value)
    if (
        raster.dtype != np.uint8
        or raster.ndim not in (2, 3)
        or raster.ndim == 3  # noqa: RUF021  precedence is intended; keep as written
        and raster.shape[2] not in (3, 4)
    ):
        raise ValueError("image must be an 8-bit grayscale or BGR(A) raster")
    return cast(Raster, raster)


__all__ = ["rotate_right_angle"]
