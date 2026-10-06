"""Pure, value-only recognition pipeline for one decoded scan page."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, TypeGuard, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import ProcessingStatus, StudentIdStatus
from omr_grader.domain.errors import Err, ErrorContextValue, ErrorInfo
from omr_grader.domain.models import (
    AutomaticPage,
    EvidenceSummary,
    PageFailure,
    PageRef,
    StudentIdRecognition,
)
from omr_grader.domain.profile import Page, Profile, ProfileRegion
from omr_grader.ingestion.images import (
    MAX_DECODED_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_SOURCE_BYTES,
    preflight_tiff,
)
from omr_grader.recognition.form_alignment import PageAlignment, align_page
from omr_grader.recognition.grid_reader import _has_frozen_profile_invariants, read_grid
from omr_grader.recognition.normalization import warp_page
from omr_grader.recognition.orientation import rotate_right_angle
from omr_grader.recognition.overlay import render_overlay
from omr_grader.recognition.thresholds import RecognitionThresholds

_RASTER = NDArray[np.uint8]
_HEADER_DIMENSIONS = tuple[int, int]
_MAX_COLOR_CHANNELS: Final = 3
_TRUSTED_INLIER_RATIO: Final = 0.65
"""Share of form bubbles that must sit on printed circles for an automatic read.

All 24 baseline pages matched 81-98% of their 580 bubbles.
"""
_TRUSTED_ROTATION_MARGIN: Final = 0.05
"""Lead of the chosen rotation over the runner-up, as a share of form bubbles (baseline 12-14%)."""
_TRUSTED_EXTRA_CIRCLES: Final = 0.2
"""Printed circles the profile does not explain, as a share of its bubbles.

A page of a larger form fits a smaller profile on every bubble the profile has, so only
its extra printed circles show that questions would silently go unread. Baseline pages
left at most 1% (100 questions) and 2.7% (50 questions) unexplained; 100-question pages
read with the 50-question profile left 68-77%.
"""


@dataclass(frozen=True, slots=True)
class PipelineInput:
    """Pickle-safe inputs for one page; raster bytes must be an encoded image."""

    page_ref: PageRef
    encoded_raster: bytes
    profile: Profile
    thresholds: RecognitionThresholds

    def __post_init__(self) -> None:
        if not isinstance(self.page_ref, PageRef) or type(self.encoded_raster) is not bytes:
            raise TypeError("pipeline input must contain value-only page data")
        if not self.encoded_raster or not isinstance(self.profile, Profile):
            raise ValueError("pipeline input is incomplete")
        if len(self.encoded_raster) > MAX_SOURCE_BYTES:
            raise ValueError("encoded_raster exceeds the source byte quota")
        tiff_error = _tiff_preflight_error(self.encoded_raster)
        if tiff_error is not None:
            raise ValueError(tiff_error.code)
        dimensions = _header_dimensions(self.encoded_raster)
        if dimensions is None or _dimension_error(*dimensions) is not None:
            raise ValueError("encoded_raster header is invalid or exceeds decode quotas")


@dataclass(frozen=True, slots=True)
class RecognitionArtifacts:
    """Unpublished output bytes. The coordinator chooses durable artifact paths."""

    normalized_png: bytes
    coordinates_json: bytes
    overlay_png: bytes

    def __post_init__(self) -> None:
        if any(
            type(item) is not bytes or not item
            for item in (self.normalized_png, self.coordinates_json, self.overlay_png)
        ):
            raise ValueError("recognition artifacts must be nonempty bytes")


@dataclass(frozen=True, slots=True)
class PipelineSuccess:
    page: AutomaticPage
    artifacts: RecognitionArtifacts

    def __post_init__(self) -> None:
        if self.page.processing_status not in {
            ProcessingStatus.PROCESSED,
            ProcessingStatus.NEEDS_MANUAL_REVIEW,
        }:
            raise ValueError("success must contain a recognized page")


@dataclass(frozen=True, slots=True)
class PipelineFailure:
    page: AutomaticPage
    failure: PageFailure

    def __post_init__(self) -> None:
        if self.page.processing_status not in {
            ProcessingStatus.FAILED,
            ProcessingStatus.UNPROCESSABLE,
        }:
            raise ValueError("failure must contain a failed page")
        if self.failure.page_ref != self.page.page_ref or self.failure.errors != self.page.errors:
            raise ValueError("failure diagnostics must match page diagnostics")


type PipelineResult = PipelineSuccess | PipelineFailure


def recognize_page(task: PipelineInput) -> PipelineResult:
    """Recognize one encoded page without touching session state or filesystem."""
    if not _has_valid_frozen_profile(task.profile):
        return _failure(task.page_ref, _error("INVALID_FROZEN_PROFILE", "profile.regions"))
    decoded = _decode(task.encoded_raster)
    if isinstance(decoded, ErrorInfo):
        return _failure(task.page_ref, decoded)
    image = decoded
    alignment = align_page(_gray(image), task.profile)
    if alignment is None:
        return _failure(
            task.page_ref,
            _error("FORM_NOT_FOUND", "encoded_raster", "printed bubbles do not match the form"),
        )
    page = cast(Page, task.profile.page)
    raster = warp_page(
        rotate_right_angle(image, alignment.rotation),
        alignment.forward,
        (page.source_width, page.source_height),
        alignment.inlier_ratio,
    )
    if isinstance(raster, Err):
        return _failure(task.page_ref, raster.errors[0])
    normalized = raster.value
    recognition = read_grid(
        normalized.pixels,
        task.profile,
        task.thresholds,
        bubble_radius=alignment.bubble_radius,
        trusted=_alignment_is_trusted(alignment),
    )
    if isinstance(recognition, Err):
        return _failure(task.page_ref, recognition.errors[0])
    grid = recognition.value
    status = (
        ProcessingStatus.NEEDS_MANUAL_REVIEW
        if grid.needs_manual_review
        else ProcessingStatus.PROCESSED
    )
    page_result = AutomaticPage(
        1,
        task.page_ref,
        status,
        alignment.rotation,
        _decimal(alignment.confidence),
        _decimal(normalized.confidence),
        (int(normalized.pixels.shape[1]), int(normalized.pixels.shape[0])),
        _matrix_text(normalized.homography_forward),
        _matrix_text(normalized.homography_inverse),
        grid.student_id,
        grid.answers,
        grid.evidence,
    )
    overlay = render_overlay(normalized.pixels, grid.evidence)
    if isinstance(overlay, Err):
        return _failure(task.page_ref, overlay.errors[0])
    artifacts = RecognitionArtifacts(
        normalized.png_bytes, _coordinates(page_result), _png(overlay.value)
    )
    return PipelineSuccess(page_result, artifacts)


def _alignment_is_trusted(alignment: PageAlignment) -> bool:
    """Few matched bubbles, a near-tie between rotations or many printed circles the
    profile does not have send the page to review."""
    margin = (alignment.inliers - alignment.runner_up_inliers) / alignment.nodes
    return (
        alignment.inlier_ratio >= _TRUSTED_INLIER_RATIO
        and margin >= _TRUSTED_ROTATION_MARGIN
        and alignment.unexplained <= _TRUSTED_EXTRA_CIRCLES * alignment.nodes
    )


def _is_uint8_raster(value: object) -> TypeGuard[_RASTER]:
    return isinstance(value, np.ndarray) and value.dtype == np.dtype(np.uint8)


def _decode(encoded: bytes) -> _RASTER | ErrorInfo:
    tiff_error = _tiff_preflight_error(encoded)
    if tiff_error is not None:
        return tiff_error
    dimensions = _header_dimensions(encoded)
    if dimensions is None:
        return _error("IMAGE_DECODE_FAILED", "encoded_raster", "invalid image header")
    reason = _dimension_error(*dimensions)
    if reason is not None:
        return _error("IMAGE_DECODE_FAILED", "encoded_raster", reason)
    try:
        decoded = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as error:
        return _error("IMAGE_DECODE_FAILED", "encoded_raster", type(error).__name__)
    if (
        not _is_uint8_raster(decoded)
        or decoded.ndim != 3
        or decoded.shape[2] != _MAX_COLOR_CHANNELS
        or decoded.size == 0
    ):
        return _error(
            "IMAGE_DECODE_FAILED", "encoded_raster", "decoder returned an invalid color raster"
        )
    height, width = decoded.shape[:2]
    reason = _dimension_error(width, height)
    if reason is not None or decoded.nbytes > MAX_DECODED_BYTES:
        return _error(
            "IMAGE_DECODE_FAILED",
            "encoded_raster",
            reason or "decoded image byte quota exceeded",
        )
    return decoded


def _gray(image: _RASTER) -> _RASTER:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if not _is_uint8_raster(gray) or gray.ndim != 2 or gray.size == 0:
        raise ValueError("grayscale conversion returned an invalid raster")
    return gray


def _has_valid_frozen_profile(profile: object) -> bool:
    """Validate the frozen grid contract before any profile field is consumed."""
    if type(profile) is not Profile:
        return False
    try:
        page = profile.page
        return (
            type(page) is Page
            and type(profile.schema_version) is int
            and profile.schema_version == 1
            and type(profile.profile_name) is str
            and bool(profile.profile_name.strip())
            and type(profile.regions) is tuple
            and bool(profile.regions)
            and all(type(region) is ProfileRegion for region in profile.regions)
            and type(profile.sha256) is str
            and len(profile.sha256) == 64
            and all(character in "0123456789abcdef" for character in profile.sha256)
            and page.orientation in {"landscape", "portrait", "square"}
            and type(page.aspect_ratio) is Decimal
            and page.aspect_ratio.is_finite()
            and page.aspect_ratio > 0
            and (
                page.orientation == "landscape"
                and page.aspect_ratio > 1
                or page.orientation == "portrait"
                and page.aspect_ratio < 1
                or page.orientation == "square"
                and page.aspect_ratio == 1
            )
            and type(page.source_width) is int
            and type(page.source_height) is int
            and page.source_width > 0
            and page.source_height > 0
            and _has_frozen_profile_invariants(profile)
        )
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return False


def _dimension_error(width: int, height: int) -> str | None:
    if width < 1 or height < 1:
        return "invalid image dimensions"
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        return "image dimension quota exceeded"
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS:
        return "image pixel quota exceeded"
    if pixels * _MAX_COLOR_CHANNELS > MAX_DECODED_BYTES:
        return "decoded image byte quota exceeded"
    return None


def _header_dimensions(encoded: bytes) -> _HEADER_DIMENSIONS | None:
    if encoded.startswith(b"\x89PNG\r\n\x1a\n") and len(encoded) >= 24:
        return struct.unpack(">II", encoded[16:24])
    if encoded[:2] == b"BM" and len(encoded) >= 26:
        width, height = struct.unpack("<ii", encoded[18:26])
        return abs(width), abs(height)
    if encoded[:2] == b"\xff\xd8":
        return _jpeg_dimensions(encoded)
    if encoded[:2] in {b"II", b"MM"}:
        tiff = preflight_tiff(encoded)
        return tiff.value.dimensions if not isinstance(tiff, Err) else None
    return None


def _jpeg_dimensions(encoded: bytes) -> _HEADER_DIMENSIONS | None:
    offset = 2
    while offset + 9 <= len(encoded):
        if encoded[offset] != 0xFF:
            return None
        while offset < len(encoded) and encoded[offset] == 0xFF:
            offset += 1
        if offset >= len(encoded):
            return None
        marker = encoded[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(encoded):
            return None
        length = int.from_bytes(encoded[offset : offset + 2], "big")
        if length < 2 or offset + length > len(encoded):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            return (
                int.from_bytes(encoded[offset + 5 : offset + 7], "big"),
                int.from_bytes(encoded[offset + 3 : offset + 5], "big"),
            )
        offset += length
    return None


def _tiff_preflight_error(encoded: bytes) -> ErrorInfo | None:
    """Apply the ingestion TIFF policy to worker payloads without decoding them."""
    if encoded[:2] not in {b"II", b"MM"}:
        return None
    preflight = preflight_tiff(encoded)
    return preflight.errors[0] if isinstance(preflight, Err) else None


def _failure(page_ref: PageRef, error: ErrorInfo) -> PipelineFailure:
    student_id = StudentIdRecognition(None, StudentIdStatus.UNREADABLE, ())
    page = AutomaticPage(
        1,
        page_ref,
        ProcessingStatus.UNPROCESSABLE,
        None,
        None,
        None,
        None,
        None,
        None,
        student_id,
        (),
        (),
        (error,),
    )
    return PipelineFailure(page, PageFailure(1, page_ref, (error,), EvidenceSummary(80, 500, 0)))


def _error(code: str, field_path: str, detail: str | None = None) -> ErrorInfo:
    context: dict[str, ErrorContextValue] = {"manual_review": True}
    if detail is not None:
        context["detail"] = detail
    return ErrorInfo(code, f"error.{code.lower()}", field_path, context)


def _decimal(value: float) -> str:
    rounded = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _matrix_text(matrix: NDArray[np.float32]) -> tuple[str, ...]:
    return tuple(_decimal(float(value)) for value in matrix.reshape(-1))


def _png(image: _RASTER) -> bytes:
    ok, encoded = cv2.imencode(".png", image, (cv2.IMWRITE_PNG_COMPRESSION, 0))
    if not ok or not _is_uint8_raster(encoded) or encoded.ndim != 1 or encoded.size == 0:
        raise ValueError("PNG encoding failed")
    return encoded.tobytes()


def _coordinates(page: AutomaticPage) -> bytes:
    return (
        json.dumps(page.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
