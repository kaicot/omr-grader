"""Generate an exact ``.omrtemplate`` profile from detected bubble layouts.

The profile's normalized frame is the deskewed page scaled so a printed bubble has a
fixed radius. Every region is the lattice of one detected block, so the evenly divided
cells of the profile sit on the printed bubble centers.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.profile import Profile, parse_profile_bytes
from omr_grader.recognition.form_layout import ANSWER_CHOICES, ID_DIGITS, FormLayout, LatticeBlock

FRAME_BUBBLE_RADIUS: Final = 16.0
MAX_QUESTIONS: Final = 100
ID_COLUMNS: Final = 8


def supported(layout: FormLayout) -> ErrorInfo | None:
    """Explain why a detected layout cannot be graded, or return ``None``."""
    ids = layout.id_blocks
    if len(ids) != 1 or ids[0].cols != ID_COLUMNS or ids[0].rows != ID_DIGITS:
        return _issue("FORM_ID_UNSUPPORTED", "student ID grid must be 8 columns of digits 0-9")
    answers = layout.answer_blocks
    if not answers or any(block.cols != ANSWER_CHOICES for block in answers):
        return _issue("FORM_ANSWERS_UNSUPPORTED", "answer blocks must have five choices")
    if not 1 <= layout.question_count <= MAX_QUESTIONS:
        return _issue("FORM_QUESTIONS_UNSUPPORTED", "forms may hold 1 to 100 questions")
    return None


def build_profile(
    samples: Sequence[tuple[FormLayout, tuple[int, int]]], profile_name: str
) -> Result[tuple[Profile, bytes]]:
    """Average same-signature layouts into one profile.

    ``samples`` pairs each page layout with that page's ``(width, height)`` in pixels.
    Pages whose signature differs from the most common one are ignored.
    """
    if not samples:
        return Err((_issue("FORM_NOT_FOUND", "no page produced a bubble layout"),))
    signature, _ = Counter(layout.signature for layout, _ in samples).most_common(1)[0]
    chosen = [(layout, size) for layout, size in samples if layout.signature == signature]
    reference, size = chosen[0]
    # Layouts are measured on the page turned upright; a quarter turn swaps its sides.
    width, height = size if reference.rotation in (0, 180) else (size[1], size[0])
    problem = supported(reference)
    if problem is not None:
        return Err((problem,))
    blocks = (*reference.id_blocks, *reference.answer_blocks)
    stacked = [_nodes(layout) for layout, _ in chosen]
    target = stacked[0]
    aligned = [_similarity_apply(_similarity(nodes, target), nodes) for nodes in stacked]
    mean = np.mean(aligned, axis=0)

    # Deskew around the page center and scale to the fixed frame bubble radius.
    scale = FRAME_BUBBLE_RADIUS / float(np.median([layout.radius for layout, _ in chosen]))
    # The averaged lattice lives in the first sample's frame, so it is deskewed by its own
    # rows and columns; the other samples' skews no longer apply to it.
    angle = -_lattice_skew(mean, blocks)
    center = np.array([width / 2, height / 2])
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    frame_width, frame_height = round(width * scale), round(height * scale)
    frame = ((mean - center) @ rotation.T) * scale + np.array([frame_width, frame_height]) / 2

    regions: list[dict[str, object]] = []
    cursor = 0
    for block in blocks:
        count = block.rows * block.cols
        nodes = frame[cursor : cursor + count].reshape(block.rows, block.cols, 2)
        cursor += count
        col_step = float(np.mean(np.diff(nodes[:, :, 0], axis=1))) if block.cols > 1 else 0.0
        row_step = float(np.mean(np.diff(nodes[:, :, 1], axis=0))) if block.rows > 1 else 0.0
        if col_step <= 0 or row_step <= 0:
            return Err((_issue("FORM_GEOMETRY_INVALID", "lattice steps must be positive"),))
        left = float(np.mean(nodes[:, 0, 0])) - col_step / 2
        top = float(np.mean(nodes[0, :, 1])) - row_step / 2
        region: dict[str, object] = {
            "name": "id" if block.kind == "id" else _answer_name(block),
            "type": block.kind,
            "bbox_ratio": {
                "x": _ratio(left / frame_width),
                "y": _ratio(top / frame_height),
                "w": _ratio(block.cols * col_step / frame_width),
                "h": _ratio(block.rows * row_step / frame_height),
            },
            "grid": {"cols": block.cols, "rows": block.rows},
        }
        if block.kind == "answer":
            region["question_start"] = block.question_start
        regions.append(region)
    aspect = frame_width / frame_height
    wire = {
        "schema_version": 1,
        "profile_name": profile_name,
        "page": {
            "orientation": "landscape" if aspect > 1 else ("portrait" if aspect < 1 else "square"),
            "aspect_ratio": _ratio(aspect),
            "source_width": frame_width,
            "source_height": frame_height,
        },
        "regions": regions,
    }
    payload = json.dumps(wire, ensure_ascii=False, indent=1, default=_json_decimal).encode("utf-8")
    parsed = parse_profile_bytes(payload)
    if isinstance(parsed, Err):
        return parsed
    return Ok((parsed.value, payload))


def _lattice_skew(nodes: NDArray[np.float64], blocks: Sequence[LatticeBlock]) -> float:
    """Angle of the stacked lattice: where its rows and columns point, all blocks pooled."""
    across = np.zeros(2)
    down = np.zeros(2)
    cursor = 0
    for block in blocks:
        count = block.rows * block.cols
        grid = nodes[cursor : cursor + count].reshape(block.rows, block.cols, 2)
        cursor += count
        across += np.diff(grid, axis=1).reshape(-1, 2).sum(axis=0)
        down += np.diff(grid, axis=0).reshape(-1, 2).sum(axis=0)
    # A step down a column, turned back a quarter, points along the rows.
    pooled = across + np.array([down[1], -down[0]])
    return float(np.arctan2(pooled[1], pooled[0]))


def _nodes(layout: FormLayout) -> NDArray[np.float64]:
    blocks = (*layout.id_blocks, *layout.answer_blocks)
    return np.concatenate([block.nodes.reshape(-1, 2) for block in blocks])


def _similarity(source: NDArray[np.float64], target: NDArray[np.float64]) -> NDArray[np.float64]:
    """Least-squares rotation, uniform scale and translation (Umeyama) from source to target."""
    source_mean, target_mean = source.mean(axis=0), target.mean(axis=0)
    centered_source, centered_target = source - source_mean, target - target_mean
    covariance = centered_target.T @ centered_source / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    sign = np.eye(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[1, 1] = -1
    rotation = u @ sign @ vt
    variance = float((centered_source**2).sum() / len(source))
    scale = float(np.trace(np.diag(singular) @ sign) / variance)
    transform = np.eye(3)
    transform[:2, :2] = scale * rotation
    transform[:2, 2] = target_mean - scale * rotation @ source_mean
    return transform


def _similarity_apply(
    transform: NDArray[np.float64], points: NDArray[np.float64]
) -> NDArray[np.float64]:
    return points @ transform[:2, :2].T + transform[:2, 2]


def _answer_name(block: object) -> str:
    start = int(getattr(block, "question_start", 0) or 0)
    rows = int(getattr(block, "rows", 0))
    return f"q{start:03d}_{start + rows - 1:03d}"


def _ratio(value: float) -> Decimal:
    return Decimal(f"{value:.8f}")


def _json_decimal(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _issue(code: str, detail: str) -> ErrorInfo:
    return ErrorInfo(code, f"error.{code.lower()}", None, {"reason": detail})


__all__ = ["FRAME_BUBBLE_RADIUS", "build_profile", "supported"]
