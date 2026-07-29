"""Pure profile-grid OMR recognition over a normalized page image."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, cast

import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import AnswerStatus, CellStatus, FieldStatus, StudentIdStatus
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import (
    AnswerRecognition,
    AnswerValue,
    CellEvidence,
    IdCell,
    PixelRect,
    RatioRect,
    StudentIdRecognition,
)
from omr_grader.domain.profile import Profile, ProfileRegion
from omr_grader.recognition.thresholds import RecognitionThresholds, validate_thresholds

MAX_IMAGE_PIXELS: Final = 100_000_000


@dataclass(frozen=True, slots=True)
class GridRecognition:
    """Recognition values and exhaustive, ordered cell evidence for one page."""

    student_id: StudentIdRecognition
    answers: tuple[AnswerRecognition, ...]
    evidence: tuple[CellEvidence, ...]
    needs_manual_review: bool


def read_grid(
    image: NDArray[np.generic], profile: Profile, thresholds: RecognitionThresholds
) -> Result[GridRecognition]:
    """Read the fixed 8x10 ID and Q1..Q100 answer grids from a normalized image."""
    validated = validate_thresholds(thresholds)
    if isinstance(validated, Err):
        return validated
    gray = _grayscale(image)
    if gray is None:
        return _error("INVALID_NORMALIZED_IMAGE", "image")
    height, width = gray.shape
    if type(profile) is not Profile:
        return _error("INVALID_PROFILE", "profile")
    try:
        frozen_profile = _has_frozen_profile_invariants(profile)
        answer_regions = _answer_regions_with_starts(profile)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return _error("INVALID_FROZEN_PROFILE", "profile.regions")
    if not frozen_profile or answer_regions is None:
        return _error("INVALID_FROZEN_PROFILE", "profile.regions")

    evidence_index = 0
    id_cells: list[IdCell] = []
    try:
        id_region = profile.id_region
        for column in range(8):
            candidates: list[CellEvidence] = []
            for digit in range(10):
                rect, ratio = _cell_rect(id_region, column, digit, width, height)
                score = _fill_score(gray, rect, thresholds)
                candidates.append(
                    CellEvidence(
                        evidence_index,
                        None,
                        digit,
                        None,
                        rect,
                        ratio,
                        _score_text(score),
                        False,
                        CellStatus.BLANK,
                    )
                )
                evidence_index += 1
            id_cells.append(_id_cell(candidates, thresholds))

        answers: list[AnswerRecognition] = []
        for region, start in answer_regions:
            for row in range(region.grid.rows):
                question = start + row
                candidates = []
                for choice_offset in range(5):
                    rect, ratio = _cell_rect(region, choice_offset, row, width, height)
                    score = _fill_score(gray, rect, thresholds)
                    candidates.append(
                        CellEvidence(
                            evidence_index,
                            question,
                            None,
                            choice_offset + 1,
                            rect,
                            ratio,
                            _score_text(score),
                            False,
                            CellStatus.BLANK,
                        )
                    )
                    evidence_index += 1
                answers.append(_answer(question, candidates, thresholds))
    except (ArithmeticError, StopIteration, TypeError, ValueError):
        return _error("INVALID_PROFILE_GEOMETRY", "profile.regions")

    id_result = _student_id(id_cells, thresholds)
    all_evidence = tuple(cell for item in id_cells for cell in item.candidates) + tuple(
        cell for item in answers for cell in item.cells
    )
    manual = (
        (not thresholds.has_valid_calibration_provenance)
        or id_result.status is not StudentIdStatus.NORMAL
        or any(answer.value.status is AnswerStatus.UNCERTAIN for answer in answers)
    )
    return Ok(GridRecognition(id_result, tuple(answers), all_evidence, manual))


def recognize_grid(
    image: NDArray[np.generic], profile: Profile, thresholds: RecognitionThresholds
) -> Result[GridRecognition]:
    """Compatibility-free descriptive alias for the public grid reader."""
    return read_grid(image, profile, thresholds)


def _grayscale(image: NDArray[np.generic]) -> NDArray[np.uint8] | None:
    if (
        not isinstance(image, np.ndarray)
        or image.size == 0
        or not np.issubdtype(image.dtype, np.number)
    ):
        return None
    if image.ndim == 2:
        if image.shape[0] <= 0 or image.shape[1] <= 0 or image.size > MAX_IMAGE_PIXELS:
            return None
        source: NDArray[np.float32] = image.astype(np.float32, copy=False)
    elif image.ndim == 3 and image.shape[2] in (3, 4):
        if (
            image.shape[0] <= 0
            or image.shape[1] <= 0
            or image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS
        ):
            return None
        source = image[..., :3].astype(np.float32, copy=False)
        source = np.rint(
            source[..., 0] * np.float32(0.114)
            + source[..., 1] * np.float32(0.587)
            + source[..., 2] * np.float32(0.299)
        )
    else:
        return None
    return np.clip(source, np.float32(0), np.float32(255)).astype(np.uint8, copy=False)


def _answer_regions_with_starts(
    profile: Profile,
) -> tuple[tuple[ProfileRegion, int], ...] | None:
    regions = profile.answer_regions
    explicit = tuple(region.question_start for region in regions)
    if all(start is None for start in explicit):
        next_question = 1
        mapped: list[tuple[ProfileRegion, int]] = []
        for region in regions:
            mapped.append((region, next_question))
            next_question += region.grid.rows
    elif all(type(start) is int for start in explicit):
        mapped = sorted(
            ((region, cast(int, region.question_start)) for region in regions),
            key=lambda item: item[1],
        )
    else:
        return None
    questions = tuple(
        question for region, start in mapped for question in range(start, start + region.grid.rows)
    )
    return tuple(mapped) if questions == tuple(range(1, 101)) else None


def _has_frozen_profile_invariants(profile: Profile) -> bool:
    id_regions = tuple(region for region in profile.regions if region.kind == "id")
    if len(id_regions) != 1:
        return False
    id_region = id_regions[0]
    if id_region.grid.cols != 8 or id_region.grid.rows != 10:
        return False
    answer_regions = profile.answer_regions
    if not answer_regions or any(region.grid.cols != 5 for region in answer_regions):
        return False
    if not all(_valid_ratio_region(region) for region in (id_region, *answer_regions)):
        return False
    return _answer_regions_with_starts(profile) is not None


def _valid_ratio_region(region: ProfileRegion) -> bool:
    bbox = region.bbox_ratio
    values = (bbox.x, bbox.y, bbox.w, bbox.h)
    return (
        all(value.is_finite() for value in values)
        and bbox.x >= 0
        and bbox.y >= 0
        and bbox.w > 0
        and bbox.h > 0
        and bbox.x + bbox.w <= 1
        and bbox.y + bbox.h <= 1
    )


def _cell_rect(
    region: ProfileRegion, column: int, row: int, width: int, height: int
) -> tuple[PixelRect, RatioRect]:
    bbox = region.bbox_ratio
    x_ratio = bbox.x + bbox.w * Decimal(column) / Decimal(region.grid.cols)
    y_ratio = bbox.y + bbox.h * Decimal(row) / Decimal(region.grid.rows)
    w_ratio = bbox.w / Decimal(region.grid.cols)
    h_ratio = bbox.h / Decimal(region.grid.rows)
    left = round(x_ratio * width)
    top = round(y_ratio * height)
    right = round((x_ratio + w_ratio) * width)
    bottom = round((y_ratio + h_ratio) * height)
    if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
        raise ValueError("cell is outside normalized image")
    return (
        PixelRect(left, top, right - left, bottom - top),
        RatioRect(
            canonical_fraction(x_ratio),
            canonical_fraction(y_ratio),
            canonical_fraction(w_ratio),
            canonical_fraction(h_ratio),
        ),
    )


def _fill_score(
    image: NDArray[np.uint8], rect: PixelRect, thresholds: RecognitionThresholds
) -> float:
    margin_x = int(rect.w * thresholds.inner_margin_ratio)
    margin_y = int(rect.h * thresholds.inner_margin_ratio)
    left, right = rect.x + margin_x, rect.x + rect.w - margin_x
    top, bottom = rect.y + margin_y, rect.y + rect.h - margin_y
    if right <= left or bottom <= top:
        raise ValueError("cell marking area is empty")
    inner = image[top:bottom, left:right]
    ring = np.concatenate(
        (
            image[rect.y : top, rect.x : rect.x + rect.w].reshape(-1),
            image[bottom : rect.y + rect.h, rect.x : rect.x + rect.w].reshape(-1),
            image[top:bottom, rect.x : left].reshape(-1),
            image[top:bottom, right : rect.x + rect.w].reshape(-1),
        )
    )
    if ring.size == 0:
        raise ValueError("cell background area is empty")
    # The upper quartile resists dark ink leaking into the ring.  The cutoff
    # requires meaningful contrast from that local background, rather than
    # treating absolute darkness as a mark.
    background = float(np.percentile(ring, 75))
    median = float(np.median(ring))
    spread = float(np.median(np.abs(ring.astype(np.float32) - median)))
    required_contrast = max(12.0, 3.0 * spread)
    adaptive_cutoff = background - required_contrast
    otsu_cutoff = _otsu_threshold(inner)
    local_cutoff = min(float(thresholds.dark_pixel_threshold), adaptive_cutoff)
    if otsu_cutoff is not None:
        local_cutoff = min(
            local_cutoff,
            otsu_cutoff + max(8.0, (background - otsu_cutoff) * 0.25),
        )
    # A near-black ring cannot establish the required contrast.  In
    # particular, this rejects uniformly saturated dark cells instead of
    # accepting them through an absolute-darkness fallback.
    if background <= required_contrast or local_cutoff < 0.0:
        return 0.0
    return float(np.count_nonzero(inner.astype(np.float32) <= local_cutoff) / inner.size)


def _otsu_threshold(values: NDArray[np.uint8]) -> float | None:
    histogram = np.bincount(values.reshape(-1), minlength=256).astype(np.float64)
    total = float(values.size)
    if total <= 0:
        raise ValueError("empty Otsu sample")
    levels = np.arange(256, dtype=np.float64)
    cumulative_weight = np.cumsum(histogram)
    cumulative_mean = np.cumsum(histogram * levels)
    total_mean = cumulative_mean[-1]
    denominator = cumulative_weight * (total - cumulative_weight)
    variance = np.zeros(256, dtype=np.float64)
    valid = denominator > 0
    variance[valid] = (
        total_mean * cumulative_weight[valid] - cumulative_mean[valid] * total
    ) ** 2 / denominator[valid]
    peak = float(np.max(variance))
    return float(np.argmax(variance)) if peak > 0.0 else None


def _id_cell(candidates: list[CellEvidence], thresholds: RecognitionThresholds) -> IdCell:
    selected, status = _classify(candidates, thresholds)
    updated = _evidence_with_status(candidates, selected, status)
    digit = str(updated[selected[0]].digit) if status is FieldStatus.NORMAL else None
    return IdCell(digit, status, updated)


def _answer(
    question: int, candidates: list[CellEvidence], thresholds: RecognitionThresholds
) -> AnswerRecognition:
    selected, field_status = _classify(candidates, thresholds)
    status = AnswerStatus(field_status.value)
    updated = _evidence_with_status(candidates, selected, field_status)
    choices = tuple(cast(int, updated[index].choice) for index in selected)
    return AnswerRecognition(question, AnswerValue(choices, status), updated)


def _classify(
    candidates: list[CellEvidence], thresholds: RecognitionThresholds
) -> tuple[tuple[int, ...], FieldStatus]:
    scores = tuple(float(cast(str, item.fill_score)) for item in candidates)
    selected = tuple(
        index for index, score in enumerate(scores) if score >= thresholds.mark_threshold
    )
    ordered = sorted(scores, reverse=True)
    close = (
        len(ordered) > 1
        and ordered[0] >= thresholds.mark_threshold
        and (ordered[0] - ordered[1] <= thresholds.ambiguity_margin)
    )
    boundary = any(
        abs(score - thresholds.mark_threshold) <= thresholds.ambiguity_margin for score in scores
    )
    if not thresholds.has_valid_calibration_provenance or close or boundary:
        # Preserve potential selections as evidence while refusing automation.
        return selected, FieldStatus.UNCERTAIN
    if not selected:
        return (), FieldStatus.BLANK
    if len(selected) == 1:
        return selected, FieldStatus.NORMAL
    return selected, FieldStatus.MULTIPLE


def _evidence_with_status(
    candidates: list[CellEvidence], selected: tuple[int, ...], status: FieldStatus
) -> tuple[CellEvidence, ...]:
    selected_set = set(selected)
    return tuple(
        CellEvidence(
            item.index,
            item.question,
            item.digit,
            item.choice,
            item.pixel_rect,
            item.ratio_rect,
            item.fill_score,
            offset in selected_set,
            CellStatus(status.value)
            if offset in selected_set or status is FieldStatus.UNCERTAIN
            else CellStatus.BLANK,
        )
        for offset, item in enumerate(candidates)
    )


def _student_id(cells: list[IdCell], thresholds: RecognitionThresholds) -> StudentIdRecognition:
    complete = thresholds.has_valid_calibration_provenance and all(
        cell.status is FieldStatus.NORMAL for cell in cells
    )
    if complete:
        return StudentIdRecognition(
            "".join(cell.selected_digit or "" for cell in cells),
            StudentIdStatus.NORMAL,
            tuple(cells),
        )
    return StudentIdRecognition(None, StudentIdStatus.INVALID, tuple(cells))


def canonical_fraction(value: Decimal | float | int) -> str:
    """Render a finite ratio or score with half-even rounding to 12 decimals."""
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("fraction must be finite")
    rounded = decimal_value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _score_text(value: float) -> str:
    return canonical_fraction(value)


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))
