"""Synthetic OMR answer sheets that look like scans of a printed exam card.

The sheet is an A4 landscape page at about 300 DPI (3508 x 2480): white paper with mild
noise, one student-ID grid (digits 0-9 printed down every column) and answer blocks of
five choices (digits 1-5 printed across every row). Bubbles are thin gray rings with a
small printed digit inside; filled marks are black disks or small dots. Every block sits
in a thin table border, and every answer row carries a bold question number left of its
first choice.

``render_sheet`` returns a BGR ``uint8`` image. ``render_sheet_with_geometry`` also returns
the true position of every printed ring after the optional rotation, scale and shift, so a
test can compare what the engine found with what was drawn. Rotation is in clockwise
degrees: multiples of 90 are lossless quarter turns, anything else turns the page about
its center. A page turned clockwise by ``turn`` degrees needs ``(360 - turn) % 360`` more
clockwise degrees to be upright, which is the rotation an aligner must report.

Neighboring answer blocks leave 160 px of clear space between their outermost rings. The
question numbers are what tells an upright page from an upside-down one, so a sheet drawn
with ``decorations=False`` has no preferred side.

The module depends on OpenCV and NumPy only, never on the application code under test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict, Unpack

import cv2
import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]
Points = NDArray[np.float64]
Choice = int | Sequence[int] | None
Layout = Sequence[int | Sequence[int]]

PAGE_WIDTH: Final = 3508
PAGE_HEIGHT: Final = 2480
PAPER_VALUE: Final = 245
RING_RADIUS: Final = 22
RING_THICKNESS: Final = 3
RING_GRAY: Final = 110
MARK_GRAY: Final = 25

DISK_RADIUS: Final = 16.0
"""A solid pen mark: it fills the measured bubble disk."""
DOT_RADIUS: Final = 10.0
"""A small deliberate dot (about 1.7 mm): clearly above the mark threshold."""
SPECK_RADIUS: Final = 6.5
"""A speck (about 1.1 mm): too small to count and too big to ignore, a review case.

Dots of 8 px sit right on the default mark threshold, so they are not used as fixtures.
"""
MARK_STYLES: Final = {"disk": DISK_RADIUS, "dot": DOT_RADIUS, "speck": SPECK_RADIUS}

ID_ROWS: Final = 10
ID_ORIGIN: Final = (133, 330)
ID_PITCH: Final = (82, 78)
CHOICES: Final = 5
ANSWER_ORIGIN_X: Final = 928
ANSWER_ORIGIN_Y: Final = 330
ANSWER_PITCH: Final = (81, 99)
BAND_GAP: Final = 160
"""Clear space between the outermost rings of neighboring answer blocks."""
BAND_PITCH: Final = 4 * ANSWER_PITCH[0] + BAND_GAP + 2 * (RING_RADIUS + 1)
"""Distance between the first columns of neighboring answer bands."""
BAND_LIMIT: Final = 5
STACK_GAP_ROWS: Final = 5
"""Row pitches between the last row of a block and the first row of the block under it."""
DEFAULT_LAYOUT: Final[tuple[int, ...]] = (20, 20, 20, 20, 20)
NAME_BOX: Final = (93, 1200, 747, 1708)
"""Empty framed area under the ID grid (x0, y0, x1, y1) where a student writes by hand."""

_BORDER_MARGIN: Final = 62
_TABLE_LEFT: Final = 112
"""Border distance left of the first choice: room for a right-aligned question number."""
_TABLE_RIGHT: Final = 41
"""Border distance right of the last choice."""
_BORDER_GRAY: Final = 80
_LABEL_GRAY: Final = 40
_LABEL_RIGHT: Final = 50
"""Printed question numbers end this far left of the first choice."""
_LABEL_SCALE: Final = 0.9
_LABEL_THICKNESS: Final = 2
_FONT: Final = cv2.FONT_HERSHEY_SIMPLEX
_DIGIT_SCALE: Final = 0.6
_SHIFT: Final = 4
"""Sub-pixel bits for ``cv2.circle``: centers and radii are drawn to 1/16 pixel."""
_DIGITS: Final = "0123456789"


class SheetOptions(TypedDict, total=False):
    """Keyword options shared by ``render_sheet`` and ``render_sheet_with_geometry``."""

    layout: Layout
    id_columns: int
    mark_style: str
    mark_radius: float | None
    rotation: float
    scale: float
    shift: tuple[float, float]
    ring_gray: int
    noise: float
    blur: float
    jitter: bool
    decorations: bool
    digits: bool
    header_rings: bool
    header_label: str | None
    header_in_table: bool
    stack_gap_rows: float
    seed: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SheetGeometry:
    """True ring positions of a rendered sheet, in the coordinates of the returned image."""

    width: int
    height: int
    radius: float
    id_nodes: Points
    block_nodes: tuple[Points, ...]
    block_starts: tuple[int, ...]
    turn: int
    """Clockwise degrees the page was turned (0, 90, 180 or 270)."""

    @property
    def upright_rotation(self) -> int:
        """Clockwise degrees that bring the turned page back upright."""
        return (360 - self.turn) % 360

    @property
    def question_count(self) -> int:
        return sum(nodes.shape[0] for nodes in self.block_nodes)

    @property
    def centers(self) -> Points:
        """Every ring center: the ID grid row by row, then each answer block in question order.

        This is the order in which a generated profile lists its cells.
        """
        parts = [
            self.id_nodes.reshape(-1, 2),
            *(nodes.reshape(-1, 2) for nodes in self.block_nodes),
        ]
        return np.concatenate(parts)

    def answer_center(self, question: int, choice: int) -> tuple[float, float]:
        for start, nodes in zip(self.block_starts, self.block_nodes, strict=True):
            if start <= question < start + nodes.shape[0]:
                x, y = nodes[question - start, choice - 1]
                return float(x), float(y)
        raise ValueError(f"question {question} is not printed on this sheet")

    def id_center(self, column: int, digit: int) -> tuple[float, float]:
        x, y = self.id_nodes[digit, column]
        return float(x), float(y)


@dataclass(frozen=True, slots=True)
class _Block:
    origin: tuple[int, int]
    rows: int
    start: int


def choices_of(choice: Choice) -> tuple[int, ...]:
    """The sorted choices a mark specification stands for: ``()`` blank, one, or a double."""
    values = () if choice is None else ((choice,) if isinstance(choice, int) else tuple(choice))
    if any(not 1 <= value <= CHOICES for value in values):
        raise ValueError("choices run from 1 to 5")
    return tuple(sorted(set(values)))


def sample_answers(count: int = 100) -> dict[int, Choice]:
    """A deterministic mix for ``count`` questions: single marks, some blanks, some doubles."""
    answers: dict[int, Choice] = {}
    for question in range(1, count + 1):
        if question % 11 == 0:
            answers[question] = None
        elif question % 17 == 0:
            first = question % 5 + 1
            answers[question] = (first, first + 1) if first < CHOICES else (1, CHOICES)
        else:
            answers[question] = (question * 3 + question // 7) % CHOICES + 1
    return answers


def render_sheet(
    answers: Mapping[int, Choice] | None = None,
    student_id: str = "",
    **options: Unpack[SheetOptions],
) -> Image:
    """Render one sheet; see ``render_sheet_with_geometry`` for the options."""
    image, _ = render_sheet_with_geometry(answers, student_id, **options)
    return image


def render_sheet_with_geometry(
    answers: Mapping[int, Choice] | None = None,
    student_id: str = "",
    *,
    layout: Layout = DEFAULT_LAYOUT,
    id_columns: int = 8,
    mark_style: str = "disk",
    mark_radius: float | None = None,
    rotation: float = 0.0,
    scale: float = 1.0,
    shift: tuple[float, float] = (0.0, 0.0),
    ring_gray: int = RING_GRAY,
    noise: float = 2.0,
    blur: float = 0.7,
    jitter: bool = True,
    decorations: bool = True,
    digits: bool = True,
    header_rings: bool = False,
    header_label: str | None = None,
    header_in_table: bool = False,
    stack_gap_rows: float = STACK_GAP_ROWS,
    seed: int = 0,
    width: int = PAGE_WIDTH,
    height: int = PAGE_HEIGHT,
) -> tuple[Image, SheetGeometry]:
    """Draw a sheet and return it with the true ring positions.

    ``answers`` maps a question (numbered left band first, top to bottom) to one choice
    (1-5), several choices (a double mark) or ``None`` (blank). ``student_id`` marks one
    digit per column from the first column; any character that is not a digit leaves its
    column blank. ``layout`` lists the rows of each answer block left to right; a nested
    sequence stacks blocks in one band. ``id_columns`` is the ID grid width (0 omits it).
    ``mark_style`` is ``"disk"``, ``"dot"`` or ``"speck"``; ``mark_radius`` overrides it.
    ``rotation`` is clockwise degrees, ``scale`` and ``shift`` (pixels) move the page
    content in place. ``noise`` is the standard deviation of the scanner noise, ``blur``
    the optical blur in pixels, ``jitter`` offsets marks the way a hand would.
    ``digits=False`` prints empty rings, as on forms without numbered bubbles.
    ``header_rings`` prints an unnumbered row of choice rings one row above every answer
    block, as some forms label their columns; ``header_label`` prints a word in that row's
    number cell and ``header_in_table`` draws the block's table around the header too.
    ``stack_gap_rows`` is the distance, in row pitches, between blocks stacked in a band.
    """
    if mark_radius is not None:
        radius = float(mark_radius)
    elif mark_style in MARK_STYLES:
        radius = MARK_STYLES[mark_style]
    else:
        raise ValueError(f"unknown mark style {mark_style!r}")
    if len(layout) > BAND_LIMIT:
        raise ValueError(f"a sheet holds at most {BAND_LIMIT} answer bands")
    if id_columns < 0 or len(student_id) > id_columns:
        raise ValueError("student_id does not fit the ID grid")
    blocks = _blocks(layout, stack_gap_rows)
    rng = np.random.default_rng(seed)

    page: Image = np.full((height, width), PAPER_VALUE, dtype=np.uint8)
    id_nodes = _id_nodes(id_columns)
    block_nodes = tuple(_block_nodes(block) for block in blocks)
    _draw_printing(
        page,
        id_nodes,
        blocks,
        block_nodes,
        ring_gray,
        decorations,
        digits,
        header_rings,
        header_label,
        header_in_table,
    )
    _draw_marks(page, id_nodes, blocks, block_nodes, answers or {}, student_id, radius, jitter, rng)

    quarter, fine = _split_rotation(rotation)
    matrix = _fine_matrix(fine, scale, shift, width, height)
    if not np.allclose(matrix, [[1, 0, 0], [0, 1, 0]]):
        page = np.asarray(
            cv2.warpAffine(
                page,
                matrix,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(255,),
            ),
            dtype=np.uint8,
        )
    if blur > 0:
        page = np.asarray(cv2.GaussianBlur(page, (0, 0), blur), dtype=np.uint8)
    if noise > 0:
        spread = max(1, round(noise * 1.7321))
        grain = rng.integers(-spread, spread + 1, size=page.shape, dtype=np.int16)
        page = np.clip(page.astype(np.int16) + grain, 0, 255).astype(np.uint8)
    image = np.asarray(cv2.cvtColor(page, cv2.COLOR_GRAY2BGR), dtype=np.uint8)

    id_nodes = _map_points(id_nodes, matrix)
    block_nodes = tuple(_map_points(nodes, matrix) for nodes in block_nodes)
    size = (width, height)
    for _ in range(quarter):
        image = np.asarray(cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE), dtype=np.uint8)
        id_nodes = _turn_points(id_nodes, size[1])
        block_nodes = tuple(_turn_points(nodes, size[1]) for nodes in block_nodes)
        size = (size[1], size[0])
    geometry = SheetGeometry(
        size[0],
        size[1],
        RING_RADIUS * scale,
        id_nodes,
        block_nodes,
        tuple(block.start for block in blocks),
        quarter * 90,
    )
    return image, geometry


def turn_points(points: Points, rotation: int, width: int, height: int) -> Points:
    """Map pixels of a ``width`` x ``height`` image through a clockwise right-angle turn."""
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be one of 0, 90, 180, 270")
    result = np.asarray(points, dtype=np.float64)
    for _ in range(rotation // 90):
        result = _turn_points(result, height)
        width, height = height, width
    return result


def paint_mark(
    image: Image,
    center: tuple[float, float],
    radius: float = DISK_RADIUS,
    value: int = MARK_GRAY,
) -> None:
    """Fill a dark disk centered on ``center`` (in place); works on gray or BGR images."""
    _circle(image, center, radius, _color(image, value), -1)


def paint_ring(
    image: Image,
    center: tuple[float, float],
    radius: float = RING_RADIUS,
    value: int = RING_GRAY,
    thickness: int = RING_THICKNESS,
    digit: int | None = None,
) -> None:
    """Draw one printed bubble: a thin ring with an optional small digit inside."""
    _circle(image, center, radius, _color(image, value), thickness)
    if digit is not None:
        _digit(image, str(digit), (round(center[0]), round(center[1])), value)


def add_handwriting(image: Image, box: tuple[int, int, int, int], seed: int = 0) -> None:
    """Scribble handwriting-like strokes, loops and script text inside ``box`` (in place).

    The loops are about as big as a bubble on purpose: they are circle-like marks that do
    not belong to any printed lattice.
    """
    left, top, right, bottom = box
    rng = np.random.default_rng(seed)
    color = _color(image, 45)
    low = np.array([left, top], dtype=np.float64)
    high = np.array([right, bottom], dtype=np.float64)
    for _ in range(10):
        point = rng.uniform(low, high)
        heading = float(rng.uniform(0.0, 2.0 * np.pi))
        trail = [point]
        for _ in range(int(rng.integers(12, 30))):
            heading += float(rng.normal(0.0, 0.5))
            point = np.clip(point + 14.0 * np.array([np.cos(heading), np.sin(heading)]), low, high)
            trail.append(point)
        stroke = np.round(np.asarray(trail)).astype(np.int32)
        cv2.polylines(image, [stroke], False, color, 4, cv2.LINE_AA)
    for _ in range(8):
        center = rng.uniform(low + 40, high - 40)
        axes = (int(rng.integers(16, 29)), int(rng.integers(16, 29)))
        cv2.ellipse(
            image,
            (round(float(center[0])), round(float(center[1]))),
            axes,
            float(rng.uniform(0, 180)),
            0,
            360,
            color,
            3,
            cv2.LINE_AA,
        )
    cv2.putText(
        image,
        "Hong Gildong 0000",
        (left + 10, top + 90),
        cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,
        1.6,
        color,
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        "OOO 8888 oo",
        (left + 10, top + 190),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.8,
        color,
        3,
        cv2.LINE_AA,
    )


def apply_shading(image: Image, darkest: float = 0.6) -> Image:
    """Uneven scanner light: a smooth left-to-right ramp from ``darkest`` up to full brightness."""
    ramp = np.linspace(darkest, 1.0, image.shape[1], dtype=np.float32)
    factor = ramp[None, :] if image.ndim == 2 else ramp[None, :, None]
    return np.clip(image.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def to_gray(image: Image) -> Image:
    """Grayscale version of a BGR image (a gray image is returned as is)."""
    if image.ndim == 2:
        return image
    return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), dtype=np.uint8)


def encode_png(image: Image, compression: int = 1) -> bytes:
    """Encode an image as PNG bytes (fast, lightly compressed by default)."""
    ok, encoded = cv2.imencode(".png", image, (cv2.IMWRITE_PNG_COMPRESSION, compression))
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return bytes(encoded.tobytes())


def encode_jpeg(image: Image, quality: int = 90) -> bytes:
    """Encode an image as JPEG bytes."""
    ok, encoded = cv2.imencode(".jpg", image, (cv2.IMWRITE_JPEG_QUALITY, quality))
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return bytes(encoded.tobytes())


def write_png(path: Path, image: Image) -> Path:
    """Write ``image`` as a PNG file and return ``path``."""
    path.write_bytes(encode_png(image))
    return path


def _blocks(layout: Layout, stack_gap_rows: float = STACK_GAP_ROWS) -> list[_Block]:
    blocks: list[_Block] = []
    start = 1
    for band, entry in enumerate(layout):
        stack = (entry,) if isinstance(entry, int) else tuple(entry)
        y = ANSWER_ORIGIN_Y
        for rows in stack:
            if rows < 1:
                raise ValueError("every answer block needs at least one row")
            blocks.append(_Block((ANSWER_ORIGIN_X + band * BAND_PITCH, y), rows, start))
            start += rows
            y += (rows - 1 + stack_gap_rows) * ANSWER_PITCH[1]
    return blocks


def _id_nodes(columns: int) -> Points:
    grid = np.zeros((ID_ROWS, columns, 2), dtype=np.float64)
    for row in range(ID_ROWS):
        for column in range(columns):
            grid[row, column] = (
                ID_ORIGIN[0] + column * ID_PITCH[0],
                ID_ORIGIN[1] + row * ID_PITCH[1],
            )
    return grid


def _block_nodes(block: _Block) -> Points:
    grid = np.zeros((block.rows, CHOICES, 2), dtype=np.float64)
    for row in range(block.rows):
        for column in range(CHOICES):
            grid[row, column] = (
                block.origin[0] + column * ANSWER_PITCH[0],
                block.origin[1] + row * ANSWER_PITCH[1],
            )
    return grid


def _draw_printing(
    page: Image,
    id_nodes: Points,
    blocks: Sequence[_Block],
    block_nodes: Sequence[Points],
    ring_gray: int,
    decorations: bool,
    digits: bool = True,
    header_rings: bool = False,
    header_label: str | None = None,
    header_in_table: bool = False,
) -> None:
    if decorations:
        cv2.putText(page, "ANSWER SHEET", (133, 190), _FONT, 1.4, (60,), 2, cv2.LINE_AA)
    if id_nodes.shape[1]:
        _table(page, id_nodes[0, 0] - _BORDER_MARGIN, id_nodes[-1, -1] + _BORDER_MARGIN)
        for row in range(ID_ROWS):
            for column in range(id_nodes.shape[1]):
                paint_ring(
                    page, _xy(id_nodes[row, column]), value=ring_gray, digit=row if digits else None
                )
        if decorations:
            _name_box(page, id_nodes)
    for block, nodes in zip(blocks, block_nodes, strict=True):
        top = nodes[0, 0] - (_TABLE_LEFT, _BORDER_MARGIN)
        if header_rings and header_in_table:
            top = top - (nodes[1, 0] - nodes[0, 0]) * (0, 1)
        _table(page, top, nodes[-1, -1] + (_TABLE_RIGHT, _BORDER_MARGIN))
        for row in range(block.rows):
            for column in range(CHOICES):
                paint_ring(
                    page,
                    _xy(nodes[row, column]),
                    value=ring_gray,
                    digit=column + 1 if digits else None,
                )
            if decorations:
                _question_number(page, str(block.start + row), nodes[row, 0])
        if header_rings:
            for column in range(CHOICES):
                above = nodes[0, column] - (nodes[1, column] - nodes[0, column])
                paint_ring(page, _xy(above), value=ring_gray, digit=column + 1 if digits else None)
            if header_label:
                _question_number(page, header_label, nodes[0, 0] - (nodes[1, 0] - nodes[0, 0]))


def _question_number(page: Image, label: str, first_choice: Points) -> None:
    """Right-aligned bold number in the label column left of the first choice."""
    (text_width, text_height), _ = cv2.getTextSize(label, _FONT, _LABEL_SCALE, _LABEL_THICKNESS)
    x = round(float(first_choice[0])) - _LABEL_RIGHT - text_width
    y = round(float(first_choice[1])) + text_height // 2
    cv2.putText(
        page, label, (x, y), _FONT, _LABEL_SCALE, (_LABEL_GRAY,), _LABEL_THICKNESS, cv2.LINE_AA
    )


def _table(page: Image, top_left: Points, bottom_right: Points) -> None:
    cv2.rectangle(
        page,
        (round(float(top_left[0])), round(float(top_left[1]))),
        (round(float(bottom_right[0])), round(float(bottom_right[1]))),
        _BORDER_GRAY,
        2,
        cv2.LINE_AA,
    )


def _name_box(page: Image, id_nodes: Points) -> None:
    """The empty framed area under the ID grid where a student writes by hand."""
    left = round(float(id_nodes[0, 0, 0])) - _BORDER_MARGIN
    right = round(float(id_nodes[-1, -1, 0])) + _BORDER_MARGIN
    top = round(float(id_nodes[-1, -1, 1])) + _BORDER_MARGIN + 80
    cv2.rectangle(page, (left, top), (right, top + 560), _BORDER_GRAY, 2, cv2.LINE_AA)


def _draw_marks(
    page: Image,
    id_nodes: Points,
    blocks: Sequence[_Block],
    block_nodes: Sequence[Points],
    answers: Mapping[int, Choice],
    student_id: str,
    radius: float,
    jitter: bool,
    rng: np.random.Generator,
) -> None:
    def stamp(center: Points) -> None:
        offset = rng.uniform(-2.0, 2.0, size=2) if jitter else np.zeros(2)
        size = radius + (rng.uniform(-0.5, 0.5) if jitter else 0.0)
        value = MARK_GRAY + (int(rng.integers(0, 20)) if jitter else 0)
        paint_mark(page, (float(center[0] + offset[0]), float(center[1] + offset[1])), size, value)

    for column, character in enumerate(student_id):
        if character in _DIGITS:
            stamp(id_nodes[int(character), column])
    for question, choice in answers.items():
        for block, nodes in zip(blocks, block_nodes, strict=True):
            if block.start <= question < block.start + block.rows:
                for selected in choices_of(choice):
                    stamp(nodes[question - block.start, selected - 1])
                break
        else:
            raise ValueError(f"question {question} is not printed on this sheet")


def _split_rotation(rotation: float) -> tuple[int, float]:
    quarter = round(rotation / 90.0)
    return quarter % 4, float(rotation - 90.0 * quarter)


def _fine_matrix(
    fine: float, scale: float, shift: tuple[float, float], width: int, height: int
) -> NDArray[np.float64]:
    """Affine map of the in-place move: clockwise turn and scale about the center, then shift."""
    center = ((width - 1) / 2.0, (height - 1) / 2.0)
    matrix = np.asarray(cv2.getRotationMatrix2D(center, -fine, scale), dtype=np.float64)
    matrix[:, 2] += shift
    return matrix


def _map_points(points: Points, matrix: NDArray[np.float64]) -> Points:
    flat = points.reshape(-1, 2)
    mapped = flat @ matrix[:, :2].T + matrix[:, 2]
    return np.asarray(mapped.reshape(points.shape), dtype=np.float64)


def _turn_points(points: Points, height: int) -> Points:
    """One clockwise quarter turn of an image that is ``height`` pixels tall."""
    flat = points.reshape(-1, 2)
    turned = np.c_[height - 1 - flat[:, 1], flat[:, 0]]
    return np.asarray(turned.reshape(points.shape), dtype=np.float64)


def _xy(point: Points) -> tuple[float, float]:
    return float(point[0]), float(point[1])


def _color(image: Image, value: int) -> tuple[int, ...]:
    return (value,) * (3 if image.ndim == 3 else 1)


def _circle(
    image: Image,
    center: tuple[float, float],
    radius: float,
    color: tuple[int, ...],
    thickness: int,
) -> None:
    cv2.circle(
        image,
        (round(center[0] * (1 << _SHIFT)), round(center[1] * (1 << _SHIFT))),
        round(radius * (1 << _SHIFT)),
        color,
        thickness,
        cv2.LINE_AA,
        _SHIFT,
    )


def _digit(image: Image, text: str, center: tuple[int, int], value: int) -> None:
    (text_width, text_height), _ = cv2.getTextSize(text, _FONT, _DIGIT_SCALE, 1)
    origin = (center[0] - text_width // 2, center[1] + text_height // 2)
    cv2.putText(image, text, origin, _FONT, _DIGIT_SCALE, _color(image, value), 1, cv2.LINE_AA)
