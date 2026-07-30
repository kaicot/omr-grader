"""Pure, value-only recognition pipeline for one decoded scan page."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, TypeGuard, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import ProcessingStatus, SourceKind, StudentIdStatus
from omr_grader.domain.errors import Err, ErrorContextValue, ErrorInfo, Ok, Result
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
from omr_grader.recognition.geometry import PageContour, detect_page_contour
from omr_grader.recognition.grid_reader import _has_frozen_profile_invariants, read_grid
from omr_grader.recognition.normalization import normalize_page
from omr_grader.recognition.orientation import rotate_right_angle, select_orientation
from omr_grader.recognition.overlay import render_overlay
from omr_grader.recognition.registration import register_profile_grid
from omr_grader.recognition.thresholds import RecognitionThresholds

_RASTER = NDArray[np.uint8]
_HEADER_DIMENSIONS = tuple[int, int]
_MAX_COLOR_CHANNELS: Final = 3


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
    gray = _gray(image)
    orientation = select_orientation(
        gray,
        _orientation_scorer(task.profile),
        minimum_confidence=0.50,
        tie_margin=0.05,
        preferred_rotation=0,
    )
    if isinstance(orientation, Err):
        return _failure(task.page_ref, orientation.errors[0])
    rotated = rotate_right_angle(image, orientation.value.rotation_degrees)
    contour = _page_contour(rotated, task.page_ref, task.profile)
    if isinstance(contour, Err):
        return _failure(task.page_ref, contour.errors[0])
    normalized = normalize_page(
        rotated,
        contour.value,
        (task.profile.page.source_width, task.profile.page.source_height)
        if task.profile.page is not None
        else (0, 0),
    )
    if isinstance(normalized, Err):
        return _failure(task.page_ref, normalized.errors[0])
    raster = normalized.value
    registered_profile = register_profile_grid(raster.pixels, task.profile)
    recognition = read_grid(raster.pixels, registered_profile, task.thresholds)
    if isinstance(recognition, Err):
        return _failure(task.page_ref, recognition.errors[0])
    grid = recognition.value
    status = (
        ProcessingStatus.NEEDS_MANUAL_REVIEW
        if grid.needs_manual_review
        else ProcessingStatus.PROCESSED
    )
    page = AutomaticPage(
        1,
        task.page_ref,
        status,
        orientation.value.rotation_degrees,
        _decimal(orientation.value.confidence),
        _decimal(raster.confidence),
        (int(raster.pixels.shape[1]), int(raster.pixels.shape[0])),
        _matrix_text(raster.homography_forward),
        _matrix_text(raster.homography_inverse),
        grid.student_id,
        grid.answers,
        grid.evidence,
    )
    overlay = render_overlay(raster.pixels, grid.evidence)
    if isinstance(overlay, Err):
        return _failure(task.page_ref, overlay.errors[0])
    artifacts = RecognitionArtifacts(raster.png_bytes, _coordinates(page), _png(overlay.value))
    return PipelineSuccess(page, artifacts)


def _page_contour(image: _RASTER, page_ref: PageRef, profile: Profile) -> Result[PageContour]:
    expected_aspect_ratio = (
        float(profile.page.aspect_ratio) if profile.page is not None else None
    )
    if page_ref.source_kind is SourceKind.PDF and expected_aspect_ratio is not None:
        height, width = image.shape[:2]
        actual_aspect_ratio = width / height
        aspect_error = abs(actual_aspect_ratio - expected_aspect_ratio) / expected_aspect_ratio
        if aspect_error <= 0.05:
            corners = np.array(
                (
                    (0.0, 0.0),
                    (float(width - 1), 0.0),
                    (float(width - 1), float(height - 1)),
                    (0.0, float(height - 1)),
                ),
                dtype=np.float32,
            )
            return Ok(PageContour(corners, 1.0 - aspect_error, 0.0))
    return detect_page_contour(image, expected_aspect_ratio=expected_aspect_ratio)


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


def _orientation_scorer(profile: Profile) -> Callable[[_RASTER], float]:
    page = cast(Page, profile.page)
    expected = float(page.aspect_ratio)

    def score(image: _RASTER) -> float:
        height, width = image.shape
        if height <= 0 or width <= 0:
            return 0.0
        ratio = width / height
        aspect_score = max(0.0, 1.0 - abs(ratio - expected) / max(expected, ratio))
        contour = detect_page_contour(image, expected_aspect_ratio=expected)
        contour_score = contour.value.confidence if not isinstance(contour, Err) else 0.0
        landmark_score = _landmark_score(image, profile)
        return 0.35 * aspect_score + 0.35 * contour_score + 0.30 * landmark_score

    return score


def _landmark_score(image: _RASTER, profile: Profile) -> float:
    """Measure configured landmark ink against each region's rotated control."""
    height, width = image.shape
    signals: list[float] = []
    id_signal = 0.0
    for region in profile.regions:
        occupied = _region_occupancy(image, region, width, height)
        control = _region_occupancy(image, region, width, height, rotated_control=True)
        signal = max(0.0, min(1.0, (occupied - control) / 0.08))
        signals.append(signal)
        if region.kind == "id":
            id_signal = signal
    answer_signals = [
        signal
        for region, signal in zip(profile.regions, signals, strict=True)
        if region.kind == "answer"
    ]
    return 0.70 * id_signal + 0.30 * float(np.mean(answer_signals))


def _region_occupancy(
    image: _RASTER,
    region: ProfileRegion,
    width: int,
    height: int,
    *,
    rotated_control: bool = False,
) -> float:
    box = region.bbox_ratio
    x = float(box.x)
    y = float(box.y)
    box_width = float(box.w)
    box_height = float(box.h)
    if rotated_control:
        x, y = 1.0 - x - box_width, 1.0 - y - box_height
    left = int(x * width)
    top = int(y * height)
    right = max(left + 1, int((x + box_width) * width))
    bottom = max(top + 1, int((y + box_height) * height))
    return float(np.mean(image[top:bottom, left:right] < 180))


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
