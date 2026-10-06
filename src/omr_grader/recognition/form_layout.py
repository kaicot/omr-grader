"""Template-free OMR layout detection from the printed bubbles of a scan.

Bubbles are grouped into regular lattices. A lattice of ten rows is a student-ID grid,
a five-column lattice is an answer block, and answer blocks are numbered from the
left-most column band downwards. Each lattice is fitted with an affine map, so filled
or missing circles are recovered from their neighbours.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.recognition.bubbles import Bubbles, find_bubbles

ANSWER_CHOICES: Final = 5
RIGHT_ANGLES: Final = (0, 90, 180, 270)
ID_DIGITS: Final = 10
_CLUSTER_TOLERANCE: Final = 0.6
_MAX_BRIDGED_ROWS: Final = 2
_DIGIT_GAP: Final = 1.5
"""Gray levels by which the last printed digit must outweigh the first to tell the side.

Real forms print (5) 3.6-11.8 levels darker than (1) in every answer block; rings without
printed digits differ by less than 0.1, so they leave the decision to question numbers.
"""


@dataclass(frozen=True, slots=True)
class LatticeBlock:
    """One regular grid of bubbles; ``nodes`` has shape (rows, cols, 2) in source pixels."""

    kind: str
    cols: int
    rows: int
    nodes: NDArray[np.float64]
    detected: int
    residual: float
    question_start: int | None = None


@dataclass(frozen=True, slots=True)
class FormLayout:
    """Every bubble lattice of one page with question numbering."""

    radius: float
    skew_radians: float
    blocks: tuple[LatticeBlock, ...]
    rotation: int = 0
    """Clockwise right angle applied to the source page before ``blocks`` were measured."""

    @property
    def answer_blocks(self) -> tuple[LatticeBlock, ...]:
        return tuple(
            sorted(
                (block for block in self.blocks if block.kind == "answer"),
                key=lambda block: block.question_start or 0,
            )
        )

    @property
    def id_blocks(self) -> tuple[LatticeBlock, ...]:
        return tuple(block for block in self.blocks if block.kind == "id")

    @property
    def question_count(self) -> int:
        return sum(block.rows for block in self.answer_blocks)

    @property
    def signature(self) -> tuple[object, ...]:
        """Shape fingerprint used to recognise the same printed form again."""
        return (
            tuple(block.cols for block in self.id_blocks),
            tuple((block.cols, block.rows) for block in self.answer_blocks),
        )


def detect_layout(gray: NDArray[np.uint8]) -> FormLayout | None:
    """Find the bubble layout of a page scanned at any right angle.

    Lattices only line up in two of the four right angles. Of those two, the upright one
    has the lightest printed digit, (1), in the first column of the answer blocks and the
    heavier (5) in the last. Forms without printed digits fall back to question numbers
    printed left of the first choice.
    """
    bubbles = find_bubbles(gray)
    if bubbles is None:
        return None
    height, width = gray.shape
    best: tuple[tuple[int, int, int, float], FormLayout] | None = None
    for rotation in RIGHT_ANGLES:
        points = rotate_points(bubbles.centers, rotation, width, height)
        turned = _turn(gray, rotation)
        layout = layout_from_bubbles(
            Bubbles(points, bubbles.radius_samples(), bubbles.radius), turned
        )
        if layout is None:
            continue
        layout = FormLayout(layout.radius, layout.skew_radians, layout.blocks, rotation)
        complete = int(len(layout.id_blocks) == 1 and bool(layout.answer_blocks))
        score = (
            complete,
            layout.question_count,
            _first_digit_lighter(layout, turned),
            _numbers_on_left(layout, turned),
        )
        if best is None or score > best[0]:
            best = (score, layout)
    return None if best is None else best[1]


def rotate_points(
    points: NDArray[np.float64], rotation: int, width: int, height: int
) -> NDArray[np.float64]:
    """Map source pixels into the image produced by a clockwise right-angle rotation."""
    x, y = points[:, 0], points[:, 1]
    if rotation == 0:
        return points.copy()
    if rotation == 90:
        return np.c_[height - 1 - y, x]
    if rotation == 180:
        return np.c_[width - 1 - x, height - 1 - y]
    if rotation == 270:
        return np.c_[y, width - 1 - x]
    raise ValueError("rotation must be one of 0, 90, 180, 270")


def _turn(gray: NDArray[np.uint8], rotation: int) -> NDArray[np.uint8]:
    if rotation == 0:
        return gray
    code = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }[rotation]
    return np.asarray(cv2.rotate(gray, code), dtype=np.uint8)


def _first_digit_lighter(layout: FormLayout, gray: NDArray[np.uint8]) -> int:
    """1 when the first column prints clearly less ink than the last, -1 for the opposite.

    Each column prints one digit; the lower quartile over rows ignores marked bubbles and
    the median over blocks ignores a block marked almost everywhere. Without a clear gap,
    as on rings with no printed digits, the answer is 0 and question numbers decide.
    """
    blocks = [block for block in layout.answer_blocks if block.cols >= 2]
    if not blocks:
        return 0
    reach = max(2, int(layout.radius))
    yy, xx = np.mgrid[-reach : reach + 1, -reach : reach + 1]
    inside = xx * xx + yy * yy <= (0.6 * layout.radius) ** 2
    height, width = gray.shape

    def column_ink(block: LatticeBlock, column: int) -> float:
        values: list[float] = []
        for row in range(block.rows):
            x, y = int(round(block.nodes[row, column, 0])), int(round(block.nodes[row, column, 1]))
            if reach <= x < width - reach and reach <= y < height - reach:
                patch = gray[y - reach : y + reach + 1, x - reach : x + reach + 1]
                values.append(255.0 - float(np.mean(patch[inside])))
        return float(np.percentile(values, 25)) if values else 0.0

    gap = float(
        np.median([column_ink(block, block.cols - 1) - column_ink(block, 0) for block in blocks])
    )
    if gap >= _DIGIT_GAP:
        return 1
    if gap <= -_DIGIT_GAP:
        return -1
    return 0


def _numbers_on_left(layout: FormLayout, gray: NDArray[np.uint8]) -> float:
    """Ink just left of each answer block minus ink just right of it (question numbers)."""
    height, width = gray.shape
    total = 0.0
    for block in layout.answer_blocks:
        if block.cols < 2:
            continue
        step = block.nodes[:, 1] - block.nodes[:, 0]
        sides = []
        for anchor, direction in ((block.nodes[:, 0], -1.0), (block.nodes[:, -1], 1.0)):
            samples: list[float] = []
            for index in range(len(anchor)):
                x, y = float(anchor[index, 0]), float(anchor[index, 1])
                dx, dy = float(step[index, 0]), float(step[index, 1])
                for fraction in (0.75, 1.0, 1.25):
                    sx = int(round(x + direction * fraction * dx))
                    sy = int(round(y + direction * fraction * dy))
                    if 0 <= sx < width and 0 <= sy < height:
                        samples.append(255.0 - float(gray[sy, sx]))
            sides.append(float(np.mean(samples)) if samples else 0.0)
        total += sides[0] - sides[1]
    return total


def layout_from_bubbles(bubbles: Bubbles, gray: NDArray[np.uint8] | None) -> FormLayout | None:
    """Group detected bubbles into lattices; ``gray`` resolves 5x10 grids when given."""
    radius = bubbles.radius
    points = bubbles.centers
    skew = _skew(points, radius)
    cos, sin = np.cos(-skew), np.sin(-skew)
    upright = np.c_[
        points[:, 0] * cos - points[:, 1] * sin, points[:, 0] * sin + points[:, 1] * cos
    ]
    tolerance = _CLUSTER_TOLERANCE * radius
    columns = [
        (center, members)
        for center, members in _clusters(upright[:, 0], tolerance)
        if len(members) >= 4
    ]
    if len(columns) < 2:
        return None
    xs = [center for center, _ in columns]
    column_pitch = _pitch(np.asarray(xs), 2.5 * radius, 6 * radius) or 3.5 * radius
    blocks: list[LatticeBlock] = []
    for column_run in _lattice_runs(xs, column_pitch, max_bridged=0):
        if len(column_run) < 2:
            continue
        low, high = min(column_run) - 1, max(column_run) + 1
        members = np.concatenate([m for center, m in columns if low <= center <= high])
        rows = [
            center
            for center, row_members in _clusters(upright[members, 1], tolerance)
            if len(row_members) >= max(2, int(0.4 * len(column_run)))
        ]
        if len(rows) < 2:
            continue
        row_pitch = _pitch(np.asarray(sorted(rows)), 2.0 * radius, 7 * radius)
        if row_pitch is None:
            continue
        for row_run in _lattice_runs(sorted(rows), row_pitch, max_bridged=_MAX_BRIDGED_ROWS):
            block = _fit_block(
                np.asarray(column_run), np.asarray(row_run), members, upright, points, radius
            )
            if block is not None:
                blocks.append(block)
    if not blocks:
        return None
    classified = [_classify(block, gray, radius) for block in blocks]
    return FormLayout(radius, float(skew), _number(classified, column_pitch, skew))


def _fit_block(
    column_centers: NDArray[np.float64],
    row_centers: NDArray[np.float64],
    members: NDArray[np.intp],
    upright: NDArray[np.float64],
    points: NDArray[np.float64],
    radius: float,
) -> LatticeBlock | None:
    tolerance = _CLUSTER_TOLERANCE * radius
    assigned: list[tuple[int, int, int]] = []
    for index in members:
        col = int(np.argmin(np.abs(column_centers - upright[index, 0])))
        row = int(np.argmin(np.abs(row_centers - upright[index, 1])))
        if (
            abs(column_centers[col] - upright[index, 0]) < tolerance
            and abs(row_centers[row] - upright[index, 1]) < tolerance
        ):
            assigned.append((col, row, int(index)))
    if len(assigned) < 6:
        return None
    # Rows at either end holding few printed circles are handwriting or headers.
    counts = np.bincount([row for _, row, _ in assigned], minlength=len(row_centers))
    first, last = 0, len(row_centers)
    while last - first > 2 and counts[first] < 0.5 * len(column_centers):
        first += 1
    while last - first > 2 and counts[last - 1] < 0.5 * len(column_centers):
        last -= 1
    kept = [(col, row - first, index) for col, row, index in assigned if first <= row < last]
    rows = last - first
    if rows < 2 or len(kept) < 6:
        return None
    grid = np.asarray([(col, row) for col, row, _ in kept], dtype=np.float64)
    image = points[[index for _, _, index in kept]]
    transform, residual = _fit_affine(grid, image)
    cols = len(column_centers)
    lattice = np.stack(np.meshgrid(np.arange(cols), np.arange(rows)), axis=-1).astype(np.float64)
    nodes = np.c_[lattice.reshape(-1, 2), np.ones(rows * cols)] @ transform
    return LatticeBlock("grid", cols, rows, nodes.reshape(rows, cols, 2), len(kept), residual)


def _fit_affine(
    grid: NDArray[np.float64], image: NDArray[np.float64]
) -> tuple[NDArray[np.float64], float]:
    design = np.c_[grid, np.ones(len(grid))]
    keep = np.ones(len(grid), dtype=bool)
    transform = np.zeros((3, 2))
    residuals = np.zeros(len(grid))
    for _ in range(3):
        transform, *_ = np.linalg.lstsq(design[keep], image[keep], rcond=None)
        residuals = np.hypot(*(design @ transform - image).T)
        keep = residuals < max(2.0, 3 * float(np.median(residuals[keep])))
    return transform, float(np.median(residuals[keep]))


def _classify(block: LatticeBlock, gray: NDArray[np.uint8] | None, radius: float) -> LatticeBlock:
    if block.rows == ID_DIGITS and block.cols == ANSWER_CHOICES:
        kind = _glyph_orientation(block, gray, radius)
    elif block.rows == ID_DIGITS:
        kind = "id"
    elif block.cols == ANSWER_CHOICES:
        kind = "answer"
    else:
        kind = "other"
    return LatticeBlock(kind, block.cols, block.rows, block.nodes, block.detected, block.residual)


def _glyph_orientation(block: LatticeBlock, gray: NDArray[np.uint8] | None, radius: float) -> str:
    """Answer blocks repeat one printed digit down a column; ID grids repeat it along a row."""
    if gray is None:
        return "answer"
    half = max(2, int(0.8 * radius))

    def patch(x: float, y: float) -> NDArray[np.float64]:
        crop = gray[int(y) - half : int(y) + half, int(x) - half : int(x) + half].astype(np.float64)
        if crop.shape != (2 * half, 2 * half):
            return np.zeros((2 * half, 2 * half))
        return (crop - crop.mean()) / (crop.std() + 1e-6)

    patches = [[patch(x, y) for x, y in row] for row in block.nodes]
    down = [
        float((patches[r][c] * patches[r + 1][c]).mean())
        for r in range(block.rows - 1)
        for c in range(block.cols)
    ]
    across = [
        float((patches[r][c] * patches[r][c + 1]).mean())
        for r in range(block.rows)
        for c in range(block.cols - 1)
    ]
    return "answer" if np.median(down) > np.median(across) else "id"


def _number(
    blocks: Sequence[LatticeBlock], column_pitch: float, skew: float
) -> tuple[LatticeBlock, ...]:
    """Number answer rows from the left-most column band, top to bottom."""
    cos, sin = np.cos(-skew), np.sin(-skew)

    def upright(block: LatticeBlock) -> tuple[float, float]:
        x, y = block.nodes[0, 0]
        return float(x * cos - y * sin), float(x * sin + y * cos)

    answers = sorted((b for b in blocks if b.kind == "answer"), key=lambda b: upright(b)[0])
    bands: list[list[LatticeBlock]] = []
    for block in answers:
        if bands and upright(block)[0] - upright(bands[-1][0])[0] < 2 * column_pitch:
            bands[-1].append(block)
        else:
            bands.append([block])
    numbered: dict[int, LatticeBlock] = {}
    start = 1
    for band in bands:
        for block in sorted(band, key=lambda b: upright(b)[1]):
            numbered[id(block)] = LatticeBlock(
                block.kind,
                block.cols,
                block.rows,
                block.nodes,
                block.detected,
                block.residual,
                start,
            )
            start += block.rows
    return tuple(numbered.get(id(block), block) for block in blocks)


def _skew(points: NDArray[np.float64], radius: float) -> float:
    angles: list[float] = []
    for x, y in points:
        delta = points - (x, y)
        near = (
            (delta[:, 0] > 2.5 * radius)
            & (delta[:, 0] < 4.5 * radius)
            & (np.abs(delta[:, 1]) < 0.8 * radius)
        )
        angles.extend(np.arctan2(delta[near, 1], delta[near, 0]).tolist())
    return float(np.median(angles)) if angles else 0.0


def _clusters(
    values: NDArray[np.float64], tolerance: float
) -> list[tuple[float, NDArray[np.intp]]]:
    order = np.argsort(values)
    groups: list[list[int]] = [[int(order[0])]]
    for previous, current in zip(order[:-1], order[1:], strict=True):
        if values[current] - values[previous] <= tolerance:
            groups[-1].append(int(current))
        else:
            groups.append([int(current)])
    return [(float(np.mean(values[group])), np.asarray(group, dtype=np.intp)) for group in groups]


def _pitch(sorted_values: NDArray[np.float64], low: float, high: float) -> float | None:
    gaps = np.diff(sorted_values)
    gaps = gaps[(gaps > low) & (gaps < high)]
    return float(np.median(gaps)) if gaps.size else None


def _lattice_runs(centers: Sequence[float], pitch: float, max_bridged: int) -> list[list[float]]:
    """Split sorted centers into evenly spaced runs, bridging up to ``max_bridged`` gaps."""
    runs: list[list[float]] = [[centers[0]]]
    for value in centers[1:]:
        gap = value - runs[-1][-1]
        steps = int(round(gap / pitch))
        if steps <= 0:
            continue
        if steps - 1 <= max_bridged and abs(gap - steps * pitch) < 0.35 * pitch:
            previous = runs[-1][-1]
            runs[-1].extend(previous + gap * (k + 1) / steps for k in range(steps - 1))
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


__all__ = [
    "RIGHT_ANGLES",
    "rotate_points",
    "ANSWER_CHOICES",
    "ID_DIGITS",
    "FormLayout",
    "LatticeBlock",
    "detect_layout",
    "layout_from_bubbles",
]
