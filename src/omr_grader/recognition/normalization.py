"""Warp a scanned page into its profile frame and keep the reversible transforms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

Image = NDArray[np.uint8]
Matrix = NDArray[np.float32]
PointArray = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class NormalizedRaster:
    """Lossless normalized pixels and reversible source/normalized transforms."""

    pixels: Image
    png_bytes: bytes
    homography_forward: Matrix
    homography_inverse: Matrix
    confidence: float

    def __post_init__(self) -> None:
        if (
            self.pixels.dtype != np.uint8
            or self.pixels.ndim not in (2, 3)
            or self.pixels.ndim == 3
            and self.pixels.shape[2] not in (3, 4)
        ):
            raise ValueError("normalized raster must be uint8 gray or color")
        if self.pixels.shape[0] * self.pixels.shape[1] > 100_000_000:
            raise ValueError("normalized raster exceeds the pixel bound")
        if (
            not self.png_bytes
            or self.homography_forward.shape != (3, 3)
            or self.homography_inverse.shape != (3, 3)
            or self.homography_forward.dtype != np.float32
            or self.homography_inverse.dtype != np.float32
        ):
            raise ValueError("invalid normalization payload")
        if (
            not np.isfinite(self.homography_forward).all()
            or not np.isfinite(self.homography_inverse).all()
        ):
            raise ValueError("homographies must be finite")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("invalid normalization confidence")
        pixels = self.pixels.copy()
        forward = self.homography_forward.copy()
        inverse = self.homography_inverse.copy()
        pixels.setflags(write=False)
        forward.setflags(write=False)
        inverse.setflags(write=False)
        object.__setattr__(self, "pixels", pixels)
        object.__setattr__(self, "homography_forward", forward)
        object.__setattr__(self, "homography_inverse", inverse)


def _is_uint8_raster(value: object) -> bool:
    raster = np.asarray(value)
    return (
        raster.dtype == np.uint8
        and raster.ndim in (2, 3)
        and (raster.ndim != 3 or raster.shape[2] in (3, 4))
    )


def _uint8_raster(value: object) -> Image:
    raster = np.asarray(value)
    if not _is_uint8_raster(raster):
        raise ValueError("raster must be uint8 gray or BGR(A)")
    return cast(Image, raster)


def _float32_matrix(value: object) -> Matrix:
    matrix = np.asarray(value, dtype=np.float32)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("homography must be a finite 3x3 float32 matrix")
    return matrix


def _error(code: str, detail: str) -> ErrorInfo:
    return ErrorInfo(
        code,
        f"error.{code.lower()}",
        context={"manual_review": True, "detail": detail},
    )


def warp_page(
    image: Image,
    forward: NDArray[np.float64],
    normalized_size: tuple[int, int],
    confidence: float,
) -> Result[NormalizedRaster]:
    """Warp a page into its profile frame with a homography fitted on printed bubbles."""
    if not _is_uint8_raster(image):
        return Err((_error("PAGE_NOT_FOUND", "unsupported source raster"),))
    width, height = normalized_size
    if not 2 <= width <= 20_000 or not 2 <= height <= 20_000 or width * height > 100_000_000:
        raise ValueError("normalized_size is outside the safe raster bounds")
    matrix = np.asarray(forward, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        return Err((_error("PAGE_NOT_FOUND", "alignment homography is invalid"),))
    inverse = np.linalg.inv(matrix)
    normalized = _uint8_raster(
        cv2.warpPerspective(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255),
        )
    )
    encoded, png = cv2.imencode(".png", normalized, (cv2.IMWRITE_PNG_COMPRESSION, 1))
    if not bool(encoded):
        return Err((_error("PAGE_NOT_FOUND", "normalized raster could not be PNG encoded"),))
    return Ok(
        NormalizedRaster(
            normalized.copy(),
            bytes(png),
            _float32_matrix(matrix.astype(np.float32)),
            _float32_matrix(inverse.astype(np.float32)),
            float(min(1.0, max(0.0, confidence))),
        )
    )
