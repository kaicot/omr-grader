"""Pure perspective normalization and coordinate conversion for OMR pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.recognition.geometry import PageContour

Image = NDArray[np.uint8]
Matrix = NDArray[np.float32]
PointArray = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class RatioRoi:
    """A rectangular ROI expressed against normalized page width and height."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if not all(np.isfinite(value) for value in values) or self.x < 0 or self.y < 0:
            raise ValueError("ROI coordinates must be finite and non-negative")
        if (
            self.width <= 0
            or self.height <= 0
            or self.x + self.width > 1
            or self.y + self.height > 1
        ):
            raise ValueError("ROI must be contained in the normalized page")


@dataclass(frozen=True, slots=True)
class PixelRoi:
    """An integer, in-bounds rectangle in normalized pixel coordinates."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if min(self.x, self.y) < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("pixel ROI must have non-negative origin and positive size")


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


def normalize_page(
    image: Image,
    contour: PageContour,
    normalized_size: tuple[int, int],
) -> Result[NormalizedRaster]:
    """Perspective-warp a page to a canonical lossless PNG raster.

    The forward matrix maps source pixels to normalized pixels; the inverse maps
    normalized pixels back to the original scan. No file or session state is used.
    """
    if not _is_uint8_raster(image):
        return Err((_error("PAGE_NOT_FOUND", "unsupported source raster"),))
    width, height = normalized_size
    if not 2 <= width <= 20_000 or not 2 <= height <= 20_000 or width * height > 100_000_000:
        raise ValueError("normalized_size is outside the safe raster bounds")
    source = _float32_points(contour.corners)
    if (
        np.min(source[:, 0]) < 0
        or np.min(source[:, 1]) < 0
        or np.max(source[:, 0]) >= image.shape[1]
        or np.max(source[:, 1]) >= image.shape[0]
    ):
        return Err((_error("PAGE_NOT_FOUND", "page contour is outside source bounds"),))
    destination = np.array(
        ((0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1)),
        dtype=np.float32,
    )
    forward = _float32_matrix(cv2.getPerspectiveTransform(source, destination))
    inverse = _float32_matrix(cv2.getPerspectiveTransform(destination, source))
    normalized = _uint8_raster(
        cv2.warpPerspective(
            image,
            forward,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255),
        )
    )
    encoded, png = cv2.imencode(
        ".png",
        normalized,
        (cv2.IMWRITE_PNG_COMPRESSION, 3),
    )
    if not bool(encoded):
        return Err((_error("PAGE_NOT_FOUND", "normalized raster could not be PNG encoded"),))
    return Ok(NormalizedRaster(normalized.copy(), bytes(png), forward, inverse, contour.confidence))


def ratio_roi_to_pixels(roi: RatioRoi, normalized_size: tuple[int, int]) -> PixelRoi:
    """Convert an in-bounds ratio ROI using enclosing integer pixel boundaries."""
    width, height = normalized_size
    if width <= 0 or height <= 0:
        raise ValueError("normalized_size must be positive")
    left = int(np.floor(roi.x * width))
    top = int(np.floor(roi.y * height))
    right = int(np.ceil((roi.x + roi.width) * width))
    bottom = int(np.ceil((roi.y + roi.height) * height))
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    return PixelRoi(left, top, right - left, bottom - top)


def transform_points(points: PointArray, homography: Matrix) -> PointArray:
    """Apply a homogeneous transform to N source points without mutating either input."""
    source = _float32_points(points)
    matrix = _float32_matrix(homography)
    transformed = cv2.perspectiveTransform(source.reshape(-1, 1, 2), matrix)
    return _float32_points(np.asarray(transformed).reshape(-1, 2))


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


def _float32_points(value: object) -> PointArray:
    points = np.asarray(value, dtype=np.float32)
    if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
        raise ValueError("points must be a finite Nx2 float32 matrix")
    return points


def _error(code: str, detail: str) -> ErrorInfo:
    return ErrorInfo(
        code,
        f"error.{code.lower()}",
        context={"manual_review": True, "detail": detail},
    )
