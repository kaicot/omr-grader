"""Detect the OMR form of selected scans and match it to a saved profile.

The user no longer picks a template: the printed bubbles of the first scanned pages
define the form. A saved profile with the same structure that fits the pages is reused;
otherwise a new exact profile is generated for one confirmation by the user.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.profile import Profile
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.ingestion.images import enumerate_image_folder, enumerate_image_paths
from omr_grader.ingestion.pdf import enumerate_pdf, render_pdf_page
from omr_grader.recognition.form_alignment import PageAlignment, align_page
from omr_grader.recognition.form_layout import (
    FormLayout,
    detect_layout,
    drop_unmarked_header_rows,
)
from omr_grader.recognition.form_profile import build_profile
from omr_grader.recognition.orientation import rotate_right_angle

MAX_SAMPLE_PAGES: Final = 12
_MATCH_PAGES: Final = 3
_MATCH_INLIER_RATIO: Final = 0.75
_MATCH_PRECISION: Final = 1.3
_MATCH_SLACK: Final = 0.03
"""A saved profile is reused when its median fit error on each checked page, in bubble
radii, is at most 1.3 times that of a profile built from these very pages plus 0.03.

A generated profile of the same form qualifies even on pages whose feed wobble no
profile can follow. The hand-drawn v3 template of the baseline form fits at 0.15-0.20
where a generated one fits at 0.05-0.07, sends whole pages to review, and is not reused.
"""
_FALLBACK_ERROR: Final = 0.1
"""Fit error limit on a page the fresh profile could not be fitted to at all."""
_DETECTION_SESSION: Final = "scan-" + "0" * 32
_PREVIEW_LONG_SIDE: Final = 1600


@dataclass(frozen=True, slots=True)
class FormDetection:
    """What was found on the scans and which profile to grade them with."""

    profile_filename: str | None
    generated_profile: bytes | None
    suggested_filename: str
    id_digits: int
    question_count: int
    answer_blocks: tuple[tuple[int, int], ...]
    pages_checked: int
    pages_matching: int
    preview_png: bytes
    dropped_header_rows: int = 0
    """Answer blocks whose first row no sample page marked, so it was left out as a header."""

    @property
    def is_new(self) -> bool:
        return self.profile_filename is None

    @property
    def summary(self) -> str:
        ranges = ", ".join(f"{start}~{start + rows - 1}" for start, rows in self.answer_blocks)
        return f"학번 {self.id_digits}자리 · 객관식 {self.question_count}문항 ({ranges})"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A profile built from the sample pages, with the saved profile that can replace it."""

    profile: Profile
    payload: bytes
    existing: str | None
    matching: int
    preview: bytes

    def detection(self, pages_checked: int, dropped_header_rows: int) -> FormDetection:
        answers = tuple(
            (int(region.question_start or 0), region.grid.rows)
            for region in self.profile.answer_regions
        )
        question_count = sum(rows for _, rows in answers)
        digest = hashlib.sha256(self.payload).hexdigest()[:6]
        return FormDetection(
            self.existing,
            None if self.existing else self.payload,
            f"자동양식_객관식{question_count}문항_{digest}.omrtemplate",
            self.profile.id_region.grid.cols,
            question_count,
            answers,
            pages_checked,
            self.matching,
            self.preview,
            dropped_header_rows,
        )


@dataclass(frozen=True, slots=True)
class FormDetector:
    """Read-only detection; saving a generated profile is a separate, confirmed step."""

    profiles: ProfileStore

    def detect(self, paths: tuple[str, ...]) -> Result[FormDetection]:
        pages = list(_sample_pages(paths, MAX_SAMPLE_PAGES))
        if not pages:
            return _error("SCAN_SOURCE_EMPTY", "선택한 답안지에서 읽을 수 있는 쪽이 없습니다.")
        found: list[tuple[NDArray[np.uint8], FormLayout]] = []
        for gray in pages:
            layout = detect_layout(gray)
            if layout is not None:
                found.append((gray, layout))
        if not found:
            return _error("FORM_NOT_FOUND", "답안지에서 OMR 양식을 찾지 못했습니다.")
        # A saved profile of the structure as printed comes first: a batch whose sampled
        # students all left one block's first question blank must not replace it.
        printed = self._candidate(found)
        if not isinstance(printed, Err) and printed.value.existing is not None:
            return Ok(printed.value.detection(len(pages), 0))
        # Label rings above a block that no student marks are not question 1 of the block.
        layouts, dropped = drop_unmarked_header_rows([(layout, gray) for gray, layout in found])
        if dropped:
            trimmed = self._candidate(
                [(gray, layout) for (gray, _), layout in zip(found, layouts, strict=True)]
            )
            if isinstance(trimmed, Err):
                return trimmed
            chosen = trimmed.value
            return Ok(chosen.detection(len(pages), 0 if chosen.existing else dropped))
        if isinstance(printed, Err):
            return printed
        return Ok(printed.value.detection(len(pages), 0))

    def _candidate(self, found: list[tuple[NDArray[np.uint8], FormLayout]]) -> Result[_Candidate]:
        """The profile these layouts make, checked on its own pages and matched to saved ones."""
        built = build_profile(
            [(layout, (gray.shape[1], gray.shape[0])) for gray, layout in found], "자동 인식 양식"
        )
        if isinstance(built, Err):
            return built
        profile, payload = built.value
        majority = _signature(profile)
        # Pages of the detected form speak for it; a cover sheet or a stray page of another
        # form neither becomes the preview nor blocks reuse (it goes to review when read).
        members = [item for item in found if _layout_signature(item[1]) == majority] or found
        checked = [gray for gray, _ in members[:_MATCH_PAGES]]
        fresh = [align_page(gray, profile) for gray in checked]
        # A profile that its own pages do not trust (an end row taken one step off, a page
        # read turned over) is never offered: reading with it would only send pages to review.
        if sum(1 for fit in fresh if fit is not None and fit.trusted) < -(-2 * len(fresh) // 3):
            return _error(
                "FORM_GEOMETRY_INVALID",
                "자동 인식한 양식이 답안지에 정확히 맞지 않습니다. OMR 프로필을 직접 선택하세요.",
            )
        first_gray, first_layout = members[0]
        return Ok(
            _Candidate(
                profile,
                payload,
                self._matching_profile(profile, checked, fresh),
                sum(1 for _, layout in found if _layout_signature(layout) == majority),
                _preview(rotate_right_angle(first_gray, first_layout.rotation), first_layout),
            )
        )

    def _matching_profile(
        self,
        detected: Profile,
        pages: list[NDArray[np.uint8]],
        fresh: list[PageAlignment | None],
    ) -> str | None:
        """A saved profile of the same structure that every checked page trusts and that
        fits each of them about as precisely as ``detected`` (the fresh profile) does."""
        names = self.profiles.discover()
        if isinstance(names, Err):
            return None
        best: tuple[float, str] | None = None
        for name in names.value:
            loaded = self.profiles.load(name)
            if isinstance(loaded, Err) or _signature(loaded.value) != _signature(detected):
                continue
            fits = [align_page(gray, loaded.value) for gray in pages]
            if not fits or not all(
                fit is not None and fit.trusted and _as_precise(fit, own)
                for fit, own in zip(fits, fresh, strict=True)
            ):
                continue
            score = min(fit.inlier_ratio for fit in fits if fit is not None)
            if score >= _MATCH_INLIER_RATIO and (best is None or score > best[0]):
                best = (score, name)
        return None if best is None else best[1]


def _as_precise(saved: PageAlignment, fresh: PageAlignment | None) -> bool:
    """Whether a saved profile fits a page nearly as well as one built from the pages."""
    error = saved.residual / saved.bubble_radius
    if fresh is None:
        return error <= _FALLBACK_ERROR
    return error <= _MATCH_PRECISION * fresh.residual / fresh.bubble_radius + _MATCH_SLACK


def _signature(profile: Profile) -> tuple[object, ...]:
    return (
        (profile.id_region.grid.cols,),
        tuple(
            (region.grid.cols, region.grid.rows)
            for region in sorted(profile.answer_regions, key=lambda item: item.question_start or 0)
        ),
    )


def _layout_signature(layout: FormLayout) -> tuple[object, ...]:
    return layout.signature


def _sample_pages(paths: tuple[str, ...], limit: int) -> Iterator[NDArray[np.uint8]]:
    """Grayscale pages in selection order, at most ``limit`` of them."""
    produced = 0
    for raw_path in paths:
        path = Path(raw_path)
        if produced >= limit:
            return
        if path.suffix.casefold() == ".pdf":
            batch = enumerate_pdf(path, _DETECTION_SESSION, input_ordinal=0, duplicate_ordinal=0)
            if isinstance(batch, Err):
                continue
            for item in batch.value.inputs:
                if produced >= limit:
                    return
                rendered = render_pdf_page(item)
                if isinstance(rendered, Err):
                    continue
                produced += 1
                yield _gray(rendered.value.pixels)
            continue
        images = (
            enumerate_image_folder(path, _DETECTION_SESSION)
            if path.is_dir()
            else enumerate_image_paths((path,), _DETECTION_SESSION)
        )
        if isinstance(images, Err):
            continue
        for image_input in images.value.inputs:
            if produced >= limit:
                return
            try:
                payload = image_input.source_path.read_bytes()
            except OSError:
                continue
            decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if decoded is None or decoded.size == 0:
                continue
            produced += 1
            yield np.asarray(decoded, dtype=np.uint8)


def _gray(pixels: NDArray[np.uint8]) -> NDArray[np.uint8]:
    if pixels.ndim == 2:
        return pixels
    return np.asarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY), dtype=np.uint8)


def _preview(gray: NDArray[np.uint8], layout: FormLayout) -> bytes:
    """First page with the student-ID grid in green and answer bubbles in blue."""
    canvas = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    radius = max(3, round(layout.radius * 1.15))
    thickness = max(2, radius // 6)
    for block in layout.blocks:
        if block.kind not in {"id", "answer"}:
            continue
        color = (40, 160, 40) if block.kind == "id" else (200, 90, 20)
        for row in block.nodes:
            for x, y in row:
                cv2.circle(canvas, (round(x), round(y)), radius, color, thickness, cv2.LINE_AA)
        if block.kind == "answer" and block.question_start is not None:
            for row_index in (0, block.rows - 1):
                x, y = block.nodes[row_index, 0]
                cv2.putText(
                    canvas,
                    str(block.question_start + row_index),
                    (round(x - 5.2 * radius), round(y + radius / 2)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    radius / 14,
                    (0, 0, 220),
                    max(2, thickness),
                    cv2.LINE_AA,
                )
    scale = min(1.0, _PREVIEW_LONG_SIDE / max(canvas.shape[:2]))
    if scale < 1.0:
        canvas = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    encoded, png = cv2.imencode(".png", canvas)
    return bytes(png) if encoded else b""


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", None, {"reason": reason}),))


__all__ = ["MAX_SAMPLE_PAGES", "FormDetection", "FormDetector"]
