"""Bubble-disk OMR recognition over a page warped into its profile frame."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, cast

import cv2
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
MAX_QUESTIONS: Final = 100
ID_COLUMNS: Final = 8
ID_DIGITS: Final = 10
CHOICES: Final = 5
_UPSIDE_DOWN_MARGIN: Final = 0.01
"""Upright pages print (1) 0.025-0.042 lighter than (5) in the unmarked disk density."""
_REMAP_ROWS: Final = 30_000
"""OpenCV remap maps must stay under 32767 rows."""


@dataclass(frozen=True, slots=True)
class GridRecognition:
    """Recognition values and exhaustive, ordered cell evidence for one page."""

    student_id: StudentIdRecognition
    answers: tuple[AnswerRecognition, ...]
    evidence: tuple[CellEvidence, ...]
    needs_manual_review: bool


def read_grid(
    image: NDArray[np.generic],
    profile: Profile,
    thresholds: RecognitionThresholds,
    *,
    bubble_radius: float,
    trusted: bool = True,
    adjacent: tuple[NDArray[np.float64], NDArray[np.int64]] | None = None,
) -> Result[GridRecognition]:
    """Read the 8x10 ID grid and every answer row of a page warped into the profile frame.

    ``bubble_radius`` is the printed bubble radius in frame pixels. Questions after the
    last row printed on the form are returned as unasked with geometry-free evidence.
    When ``trusted`` is false (a weak page alignment) or the page reads upside down, every
    answer becomes uncertain and the ID invalid: grading only scores reviewed values.
    ``adjacent`` holds printed bubbles right next to the answer regions (frame centers and
    choice columns); a mark on any of them withholds the page the same way, because the
    profile then leaves out a question row someone answered.
    """
    validated = validate_thresholds(thresholds)
    if isinstance(validated, Err):
        return validated
    gray = _grayscale(image)
    if gray is None:
        return _error("INVALID_NORMALIZED_IMAGE", "image")
    if type(profile) is not Profile:
        return _error("INVALID_PROFILE", "profile")
    if not (np.isfinite(bubble_radius) and bubble_radius > 1.0):
        return _error("INVALID_BUBBLE_RADIUS", "bubble_radius")
    try:
        frozen_profile = _has_frozen_profile_invariants(profile)
        answer_regions = _answer_regions_with_starts(profile)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return _error("INVALID_FROZEN_PROFILE", "profile.regions")
    if not frozen_profile or answer_regions is None:
        return _error("INVALID_FROZEN_PROFILE", "profile.regions")

    height, width = gray.shape
    try:
        ring = _ring_radius(gray, _answer_centers(answer_regions, width, height), bubble_radius)
        density = _density_map(gray, ring, thresholds.ink_floor)
        disk = _disk(thresholds.disk_ratio * ring)
        id_region = profile.id_region
        id_geometry = [
            [_cell(id_region, column, digit, width, height) for digit in range(ID_DIGITS)]
            for column in range(ID_COLUMNS)
        ]
        rows: list[tuple[int, list[tuple[PixelRect, RatioRect, tuple[float, float]]]]] = [
            (start + row, [_cell(region, choice, row, width, height) for choice in range(CHOICES)])
            for region, start in answer_regions
            for row in range(region.grid.rows)
        ]
        id_raw = np.array(
            [[_fill(density, cell[2], disk) for cell in column] for column in id_geometry]
        )
        answer_raw = np.array(
            [[_fill(density, cell[2], disk) for cell in cells] for _, cells in rows]
        )
    except (ArithmeticError, TypeError, ValueError, cv2.error):
        # OpenCV refuses some sizes of an odd profile frame; that page is reported, not raised.
        return _error("INVALID_PROFILE_GEOMETRY", "profile.regions")
    # Printed digits inside the bubbles carry ink of their own, darker on some printers;
    # only what lies above the unmarked level of the same digit counts as a mark.
    id_marks = id_raw - _unmarked_level(id_raw)[None, :]
    answer_levels = _unmarked_level(answer_raw)
    answer_marks = answer_raw - answer_levels[None, :]
    # The printed (1) carries the least ink of the five digits. A first column clearly
    # heavier than the last means the form is read upside down, so nothing is trusted.
    upside_down = float(answer_levels[0] - answer_levels[-1]) > _UPSIDE_DOWN_MARGIN
    stray = _marked_outside(density, disk, adjacent, answer_levels, thresholds)

    evidence_index = 0
    id_cells: list[IdCell] = []
    try:
        for column, cells in enumerate(id_geometry):
            candidates: list[CellEvidence] = []
            for digit, (rect, ratio, _) in enumerate(cells):
                candidates.append(
                    CellEvidence(
                        evidence_index,
                        None,
                        digit,
                        None,
                        rect,
                        ratio,
                        _score_text(id_marks[column, digit]),
                        False,
                        CellStatus.BLANK,
                    )
                )
                evidence_index += 1
            id_cells.append(_id_cell(candidates, thresholds))

        answers: list[AnswerRecognition] = []
        printed: set[int] = set()
        for row_index, (question, cells) in enumerate(rows):
            printed.add(question)
            candidates = []
            for choice_offset, (rect, ratio, _) in enumerate(cells):
                candidates.append(
                    CellEvidence(
                        evidence_index,
                        question,
                        None,
                        choice_offset + 1,
                        rect,
                        ratio,
                        _score_text(answer_marks[row_index, choice_offset]),
                        False,
                        CellStatus.BLANK,
                    )
                )
                evidence_index += 1
            answers.append(_answer(question, candidates, thresholds))
        for question in range(1, MAX_QUESTIONS + 1):
            if question in printed:
                continue
            placeholders = tuple(
                CellEvidence(
                    evidence_index + offset,
                    question,
                    None,
                    offset + 1,
                    None,
                    None,
                    None,
                    False,
                    CellStatus.BLANK,
                )
                for offset in range(CHOICES)
            )
            evidence_index += CHOICES
            answers.append(
                AnswerRecognition(question, AnswerValue((), AnswerStatus.UNASKED), placeholders)
            )
    except (ArithmeticError, StopIteration, TypeError, ValueError):
        return _error("INVALID_PROFILE_GEOMETRY", "profile.regions")

    answers.sort(key=lambda item: item.question)
    if upside_down or stray or not trusted:
        answers = [_withheld_answer(answer) for answer in answers]
        id_cells = [_withheld_id_cell(cell) for cell in id_cells]
    id_result = _student_id(id_cells, thresholds)
    all_evidence = tuple(cell for item in id_result.cells for cell in item.candidates) + tuple(
        cell for item in answers for cell in item.cells
    )
    manual = (
        upside_down
        or stray
        or (not thresholds.has_valid_calibration_provenance)
        or id_result.status is not StudentIdStatus.NORMAL
        or any(answer.value.status is AnswerStatus.UNCERTAIN for answer in answers)
    )
    return Ok(GridRecognition(id_result, tuple(answers), all_evidence, manual))


def _withheld_answer(answer: AnswerRecognition) -> AnswerRecognition:
    """Keep what was read as review evidence while confirming none of it."""
    if answer.value.status in {AnswerStatus.UNASKED, AnswerStatus.UNCERTAIN}:
        return answer
    selected = tuple(index for index, cell in enumerate(answer.cells) if cell.selected)
    cells = _evidence_with_status(list(answer.cells), selected, FieldStatus.UNCERTAIN)
    return AnswerRecognition(
        answer.question, AnswerValue(answer.value.choices, AnswerStatus.UNCERTAIN), cells
    )


def _withheld_id_cell(cell: IdCell) -> IdCell:
    if cell.status is FieldStatus.UNCERTAIN:
        return cell
    selected = tuple(index for index, item in enumerate(cell.candidates) if item.selected)
    return IdCell(
        None,
        FieldStatus.UNCERTAIN,
        _evidence_with_status(list(cell.candidates), selected, FieldStatus.UNCERTAIN),
    )


def question_count(profile: Profile) -> int:
    """Number of questions printed on the form described by ``profile``."""
    return sum(region.grid.rows for region in profile.answer_regions)


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


def _answer_centers(
    answer_regions: tuple[tuple[ProfileRegion, int], ...], width: int, height: int
) -> NDArray[np.float64]:
    centers: list[tuple[float, float]] = []
    for region, _ in answer_regions:
        for row in range(region.grid.rows):
            for column in range(region.grid.cols):
                centers.append(_cell(region, column, row, width, height)[2])
    return np.asarray(centers, dtype=np.float64)


def _ring_radius(gray: NDArray[np.uint8], centers: NDArray[np.float64], guess: float) -> float:
    """Radius of the printed bubble ring, measured on the page itself.

    The median over every answer bubble of the mean brightness around a circle is
    darkest on the printed ring. Filled bubbles are a minority, so they do not move
    the median, and the result does not depend on how circles were first detected.
    """
    radii = np.arange(0.55 * guess, 1.45 * guess, 0.25, dtype=np.float64)
    angles = np.linspace(0.0, 2.0 * np.pi, 48, endpoint=False)
    # remap needs maps under 32767 rows: one row per (bubble, radius), so the bubbles are
    # sampled in chunks whatever the frame resolution.
    chunk = max(1, _REMAP_ROWS // len(radii))
    means: list[NDArray[np.float64]] = []
    for start in range(0, len(centers), chunk):
        part = centers[start : start + chunk]
        xs = part[:, 0, None, None] + radii[None, :, None] * np.cos(angles)[None, None, :]
        ys = part[:, 1, None, None] + radii[None, :, None] * np.sin(angles)[None, None, :]
        sampled = cv2.remap(
            gray,
            xs.reshape(-1, len(angles)).astype(np.float32),
            ys.reshape(-1, len(angles)).astype(np.float32),
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        ).reshape(len(part), len(radii), len(angles))
        means.append(sampled.astype(np.float64).mean(axis=2))
    profile = np.median(np.concatenate(means), axis=0)
    return float(radii[int(np.argmin(profile))])


def _marked_outside(
    density: NDArray[np.float32],
    disk: tuple[NDArray[np.float64], NDArray[np.float64]],
    adjacent: tuple[NDArray[np.float64], NDArray[np.int64]] | None,
    levels: NDArray[np.float64],
    thresholds: RecognitionThresholds,
) -> bool:
    """Whether a printed bubble next to an answer region carries a mark of its own."""
    if adjacent is None or not len(adjacent[0]):
        return False
    nodes, columns = adjacent
    for index in range(len(nodes)):
        center = (float(nodes[index, 0]), float(nodes[index, 1]))
        try:
            fill = _fill(density, center, disk)
        except ValueError:
            continue
        if fill - float(levels[int(columns[index])]) >= thresholds.mark_threshold:
            return True
    return False


def _density_map(gray: NDArray[np.uint8], radius: float, ink_floor: float) -> NDArray[np.float32]:
    """Ink density in 0..1 relative to the local paper white.

    A closing wider than a bubble removes marks and print, leaving the paper's own
    brightness, so shading and uneven scanner light do not change the reading. Paper is
    0 and anything at or below ``ink_floor`` of the paper brightness is 1; light pencil
    counts in proportion to its darkness.
    """
    # The paper level is smooth by construction, so it is estimated on a reduced copy.
    factor = max(1, int(radius // 4))
    small = cv2.resize(
        gray,
        (max(1, gray.shape[1] // factor), max(1, gray.shape[0] // factor)),
        interpolation=cv2.INTER_AREA,
    )
    size = int(4.4 * radius / factor) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    paper_small = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    paper_small = cv2.GaussianBlur(paper_small, (0, 0), radius / factor)
    paper = cv2.resize(paper_small, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    relative = gray.astype(np.float32) / np.maximum(paper.astype(np.float32), 1.0)
    return np.asarray(np.clip((1.0 - relative) / (1.0 - ink_floor), 0.0, 1.0), dtype=np.float32)


def _unmarked_level(raw: NDArray[np.float64]) -> NDArray[np.float64]:
    """Typical density of each printed digit when it is left unmarked.

    ``raw`` has one row per question (or ID column) and one column per printed digit.
    The strongest cell of every row is left out, so a student who marks the same choice
    everywhere cannot turn that choice into the reference.
    """
    strongest = np.argmax(raw, axis=1)
    rest = [
        raw[row, col]
        for row in range(raw.shape[0])
        for col in range(raw.shape[1])
        if col != strongest[row]
    ]
    fallback = float(np.median(rest)) if rest else 0.0
    levels = []
    for col in range(raw.shape[1]):
        values = [raw[row, col] for row in range(raw.shape[0]) if strongest[row] != col]
        levels.append(float(np.median(values)) if values else fallback)
    return np.asarray(levels, dtype=np.float64)


def _disk(radius: float) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    reach = int(np.ceil(radius))
    yy, xx = np.mgrid[-reach : reach + 1, -reach : reach + 1]
    inside = xx * xx + yy * yy <= radius * radius
    return xx[inside].astype(np.float64), yy[inside].astype(np.float64)


def _fill(
    density: NDArray[np.float32],
    center: tuple[float, float],
    disk: tuple[NDArray[np.float64], NDArray[np.float64]],
) -> float:
    height, width = density.shape
    xs = np.rint(center[0] + disk[0]).astype(np.intp)
    ys = np.rint(center[1] + disk[1]).astype(np.intp)
    inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    if not inside.any():
        raise ValueError("bubble lies outside the normalized image")
    return float(density[ys[inside], xs[inside]].sum() / disk[0].size)


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
    if not questions or len(questions) > MAX_QUESTIONS:
        return None
    return tuple(mapped) if questions == tuple(range(1, len(questions) + 1)) else None


def _has_frozen_profile_invariants(profile: Profile) -> bool:
    id_regions = tuple(region for region in profile.regions if region.kind == "id")
    if len(id_regions) != 1:
        return False
    id_region = id_regions[0]
    if id_region.grid.cols != ID_COLUMNS or id_region.grid.rows != ID_DIGITS:
        return False
    answer_regions = profile.answer_regions
    if not answer_regions or any(region.grid.cols != CHOICES for region in answer_regions):
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


def _cell(
    region: ProfileRegion, column: int, row: int, width: int, height: int
) -> tuple[PixelRect, RatioRect, tuple[float, float]]:
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
    center = (
        float((x_ratio + w_ratio / 2) * width),
        float((y_ratio + h_ratio / 2) * height),
    )
    return (
        PixelRect(left, top, right - left, bottom - top),
        RatioRect(
            canonical_fraction(x_ratio),
            canonical_fraction(y_ratio),
            canonical_fraction(w_ratio),
            canonical_fraction(h_ratio),
        ),
        center,
    )


def _decide(
    candidates: list[CellEvidence], thresholds: RecognitionThresholds
) -> tuple[tuple[int, ...], FieldStatus]:
    """One clear mark with every rival unmarked, or blank; anything in between is reviewed."""
    scores = tuple(float(cast(str, item.fill_score)) for item in candidates)
    if not thresholds.has_valid_calibration_provenance:
        plausible = tuple(i for i, s in enumerate(scores) if s > thresholds.blank_ceiling)
        return plausible, FieldStatus.UNCERTAIN
    marked = tuple(i for i, s in enumerate(scores) if s >= thresholds.mark_threshold)
    gray_zone = tuple(
        i for i, s in enumerate(scores) if thresholds.blank_ceiling < s < thresholds.mark_threshold
    )
    if not marked and not gray_zone:
        return (), FieldStatus.BLANK
    if gray_zone:
        return tuple(sorted((*marked, *gray_zone))), FieldStatus.UNCERTAIN
    if len(marked) == 1:
        return marked, FieldStatus.NORMAL
    return marked, FieldStatus.MULTIPLE


def _id_cell(candidates: list[CellEvidence], thresholds: RecognitionThresholds) -> IdCell:
    selected, status = _decide(candidates, thresholds)
    updated = _evidence_with_status(candidates, selected, status)
    digit = str(updated[selected[0]].digit) if status is FieldStatus.NORMAL else None
    return IdCell(digit, status, updated)


def _answer(
    question: int, candidates: list[CellEvidence], thresholds: RecognitionThresholds
) -> AnswerRecognition:
    selected, field_status = _decide(candidates, thresholds)
    status = AnswerStatus(field_status.value)
    updated = _evidence_with_status(candidates, selected, field_status)
    choices = tuple(cast(int, updated[index].choice) for index in selected)
    return AnswerRecognition(question, AnswerValue(choices, status), updated)


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
    """Digits must run from the first column; only trailing columns may stay blank."""
    last = max(
        (index for index, cell in enumerate(cells) if cell.status is not FieldStatus.BLANK),
        default=-1,
    )
    active = cells[: last + 1]
    complete = (
        thresholds.has_valid_calibration_provenance
        and bool(active)
        and all(cell.status is FieldStatus.NORMAL for cell in active)
    )
    if complete:
        return StudentIdRecognition(
            "".join(cell.selected_digit or "" for cell in active),
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
    return canonical_fraction(min(1.0, max(0.0, float(value))))


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))
