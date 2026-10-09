"""Non-destructive recognition evidence overlays for normalized images."""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import AnswerStatus, CellStatus, KeyQuestionStatus
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerRecognition,
    CellEvidence,
    PixelRect,
)

# BGR values are intentionally distinct for exported OpenCV images.
NORMAL_COLOR: Final = (0, 170, 0)
BLANK_COLOR: Final = (120, 120, 120)
REVIEW_COLOR: Final = (0, 140, 255)
CORRECT_COLOR: Final = (255, 0, 0)
INCORRECT_COLOR: Final = (0, 0, 255)
MAX_OVERLAY_CELLS: Final = 580
MAX_OVERLAY_IMAGE_PIXELS: Final = 100_000_000


def render_overlay(
    image: NDArray[np.generic], evidence: tuple[CellEvidence, ...]
) -> Result[NDArray[np.uint8]]:
    """Copy ``image`` and draw colored boxes plus an ASCII status icon/label.

    The source array is never modified.  Evidence is rendered in ascending
    index order so equivalent inputs yield byte-identical overlays.
    """
    if (
        not isinstance(image, np.ndarray)
        or image.size == 0
        or image.ndim not in (2, 3)
        or image.shape[0] <= 0
        or image.shape[1] <= 0
        or image.shape[0] * image.shape[1] > MAX_OVERLAY_IMAGE_PIXELS
    ):
        return _error("INVALID_OVERLAY_IMAGE", "image")
    if image.ndim == 3 and image.shape[2] not in (3, 4):
        return _error("INVALID_OVERLAY_IMAGE", "image")
    if not np.issubdtype(image.dtype, np.number):
        return _error("INVALID_OVERLAY_IMAGE", "image")
    if type(evidence) is not tuple or len(evidence) > MAX_OVERLAY_CELLS:
        return _error("INVALID_OVERLAY_EVIDENCE", "evidence")
    # Questions the form does not print carry no geometry and are not drawn.
    if not all(
        isinstance(item, CellEvidence) and (item.pixel_rect is None) == (item.ratio_rect is None)
        for item in evidence
    ):
        return _error("INVALID_OVERLAY_EVIDENCE", "evidence")
    indices = tuple(item.index for item in evidence)
    if len(set(indices)) != len(indices) or any(
        type(index) is not int or not 0 <= index < MAX_OVERLAY_CELLS for index in indices
    ):
        return _error("INVALID_OVERLAY_EVIDENCE", "evidence")

    output: NDArray[np.uint8] = np.clip(
        image.astype(np.float32, copy=False), np.float32(0), np.float32(255)
    ).astype(np.uint8, copy=True)
    if output.ndim == 2:
        output = np.asarray(cv2.cvtColor(output, cv2.COLOR_GRAY2BGR), dtype=np.uint8)
    elif output.shape[2] == 4:
        output = output[..., :3].copy()
    for cell in sorted(evidence, key=lambda item: item.index):
        rect = cell.pixel_rect
        if rect is None:
            continue
        if (
            rect.x < 0
            or rect.y < 0
            or rect.x + rect.w > output.shape[1]
            or rect.y + rect.h > output.shape[0]
        ):
            return _error("INVALID_OVERLAY_RECT", "evidence")
        color = _color(cell)
        thickness = max(1, min(rect.w, rect.h) // 12)
        cv2.rectangle(
            output, (rect.x, rect.y), (rect.x + rect.w - 1, rect.y + rect.h - 1), color, thickness
        )
        text = _label(cell)
        baseline = min(output.shape[0] - 1, rect.y + max(10, rect.h // 2))
        font_scale = max(0.30, min(0.80, min(rect.w, rect.h) / 36))
        cv2.putText(
            output,
            text,
            (rect.x + 1, baseline),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            color,
            max(1, thickness // 2),
            cv2.LINE_AA,
        )
    return Ok(output)


def scale_overlay_evidence(
    evidence: tuple[CellEvidence, ...],
    source_size: tuple[int, int],
    target_size: tuple[int, int],
) -> tuple[CellEvidence, ...]:
    """Scale normalized-image pixel rectangles to one output raster size."""
    source_width, source_height = source_size
    target_width, target_height = target_size
    if (
        any(type(value) is not int for value in (*source_size, *target_size))
        or min(source_width, source_height, target_width, target_height) <= 0
    ):
        raise ValueError("overlay sizes must contain positive integers")
    scale_x = target_width / source_width
    scale_y = target_height / source_height
    scaled: list[CellEvidence] = []
    for cell in evidence:
        rect = cell.pixel_rect
        if rect is None:
            scaled.append(cell)
            continue
        left = min(target_width - 1, max(0, round(rect.x * scale_x)))
        top = min(target_height - 1, max(0, round(rect.y * scale_y)))
        right = min(target_width, max(left + 1, round((rect.x + rect.w) * scale_x)))
        bottom = min(target_height, max(top + 1, round((rect.y + rect.h) * scale_y)))
        scaled.append(
            replace(
                cell,
                pixel_rect=PixelRect(left, top, right - left, bottom - top),
            )
        )
    return tuple(scaled)


def render_scored_overlay_scaled(
    image: NDArray[np.generic],
    evidence: tuple[CellEvidence, ...],
    answers: tuple[AnswerRecognition, ...],
    key_entries: tuple[AnswerKeyEntry, ...],
    target_size: tuple[int, int],
) -> Result[NDArray[np.uint8]]:
    """Resize first, then redraw all overlays using scaled destination coordinates."""
    if not isinstance(image, np.ndarray) or image.ndim not in (2, 3) or image.size == 0:
        return _error("INVALID_OVERLAY_IMAGE", "image")
    if type(target_size) is not tuple or len(target_size) != 2:
        return _error("INVALID_OVERLAY_IMAGE", "target_size")
    target_width, target_height = target_size
    if (
        any(type(value) is not int for value in target_size)
        or target_width <= 0
        or target_height <= 0
    ):
        return _error("INVALID_OVERLAY_IMAGE", "target_size")
    source_height, source_width = image.shape[:2]
    try:
        scaled_evidence = scale_overlay_evidence(
            evidence,
            (source_width, source_height),
            target_size,
        )
    except (TypeError, ValueError):
        return _error("INVALID_OVERLAY_EVIDENCE", "evidence")
    by_index = {cell.index: cell for cell in scaled_evidence}
    scaled_answers = tuple(
        replace(
            answer,
            cells=tuple(by_index.get(cell.index, cell) for cell in answer.cells),
        )
        for answer in answers
    )
    prepared = np.clip(image.astype(np.float32, copy=False), np.float32(0), np.float32(255)).astype(
        np.uint8, copy=False
    )
    resized = np.asarray(
        cv2.resize(prepared, target_size, interpolation=cv2.INTER_AREA),
        dtype=np.uint8,
    )
    return render_scored_overlay(resized, scaled_evidence, scaled_answers, key_entries)


def render_scored_overlay(
    image: NDArray[np.generic],
    evidence: tuple[CellEvidence, ...],
    answers: tuple[AnswerRecognition, ...],
    key_entries: tuple[AnswerKeyEntry, ...],
) -> Result[NDArray[np.uint8]]:
    """Draw the grading result on the clean page, with no cell boxes or labels.

    A correct answer gets a thick blue circle. A wrong choice gets a thick red circle,
    the right choice a thin blue one, and the question number a red slash, as a teacher
    marks a lost question by hand. Rows still under review show orange circles. Unasked questions are not drawn. ``evidence`` is accepted for
    call compatibility; only the answer cells are drawn.
    """
    del evidence
    if (
        not isinstance(image, np.ndarray)
        or image.size == 0
        or image.ndim not in (2, 3)
        or image.shape[0] * image.shape[1] > MAX_OVERLAY_IMAGE_PIXELS
        or not np.issubdtype(image.dtype, np.number)
        or image.ndim == 3  # noqa: RUF021  precedence is intended; keep as written
        and image.shape[2] not in (3, 4)
    ):
        return _error("INVALID_OVERLAY_IMAGE", "image")
    if (
        type(answers) is not tuple
        or type(key_entries) is not tuple
        or not all(isinstance(item, AnswerRecognition) for item in answers)
        or not all(isinstance(item, AnswerKeyEntry) for item in key_entries)
    ):
        return _error("INVALID_OVERLAY_EVIDENCE", "answers")
    output: NDArray[np.uint8] = np.clip(
        image.astype(np.float32, copy=False), np.float32(0), np.float32(255)
    ).astype(np.uint8, copy=True)
    if output.ndim == 2:
        output = np.asarray(cv2.cvtColor(output, cv2.COLOR_GRAY2BGR), dtype=np.uint8)
    elif output.shape[2] == 4:
        output = output[..., :3].copy()
    keys = {entry.question: entry for entry in key_entries}
    for answer in answers:
        entry = keys.get(answer.question)
        status = answer.value.status
        if entry is None or entry.status is KeyQuestionStatus.UNASKED:
            continue
        if status is AnswerStatus.UNASKED:
            continue
        cells = {
            cell.choice: cell.pixel_rect
            for cell in answer.cells
            if cell.pixel_rect is not None and cell.choice is not None
        }
        if len(cells) != len(answer.cells):
            continue
        selected = answer.value.choices
        right = entry.answer.choices
        if status is AnswerStatus.UNCERTAIN:
            for choice in selected:
                _ring(output, cells[choice], REVIEW_COLOR, thick=True)
            for choice in right:
                _ring(output, cells[choice], CORRECT_COLOR, thick=False)
            continue
        if entry.status is KeyQuestionStatus.ALL or selected == right:
            for choice in selected:
                _ring(output, cells[choice], CORRECT_COLOR, thick=True)
            continue
        for choice in selected:
            _ring(output, cells[choice], INCORRECT_COLOR, thick=True)
        for choice in right:
            _ring(output, cells[choice], CORRECT_COLOR, thick=False)
        _slash(output, cells[min(cells)])
    return Ok(output)


def _ring(
    output: NDArray[np.uint8], rect: PixelRect, color: tuple[int, int, int], *, thick: bool
) -> None:
    center = (rect.x + rect.w // 2, rect.y + rect.h // 2)
    radius = max(3, round(0.4 * min(rect.w, rect.h)))
    width = max(2, radius // 4) if thick else max(1, radius // 9)
    cv2.circle(output, center, radius, color, width, cv2.LINE_AA)


def _slash(output: NDArray[np.uint8], first_choice: PixelRect) -> None:
    """Red slash over the question number printed one cell left of the first choice.

    The number stays readable through a single stroke, where a cross hid its digits.
    """
    cx = first_choice.x + first_choice.w // 2 - first_choice.w
    cy = first_choice.y + first_choice.h // 2
    dx = max(3, round(0.4 * first_choice.w))
    dy = max(3, round(0.3 * first_choice.h))
    width = max(2, round(first_choice.w / 14))
    cv2.line(output, (cx - dx, cy + dy), (cx + dx, cy - dy), INCORRECT_COLOR, width, cv2.LINE_AA)


def _color(cell: CellEvidence) -> tuple[int, int, int]:
    if cell.status is CellStatus.NORMAL:
        return NORMAL_COLOR
    if cell.status in {CellStatus.MULTIPLE, CellStatus.UNCERTAIN}:
        return REVIEW_COLOR
    return BLANK_COLOR


def _label(cell: CellEvidence) -> str:
    icon = (
        "+"
        if cell.status is CellStatus.NORMAL
        else "!"
        if cell.status is not CellStatus.BLANK
        else "-"
    )
    value = cell.choice if cell.choice is not None else cell.digit
    return f"{icon}{cell.index}:{value}"


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))


__all__ = [
    "BLANK_COLOR",
    "CORRECT_COLOR",
    "INCORRECT_COLOR",
    "NORMAL_COLOR",
    "REVIEW_COLOR",
    "render_overlay",
    "render_scored_overlay",
    "render_scored_overlay_scaled",
    "scale_overlay_evidence",
]
