"""Bounded PDF scan ingestion and one-page-at-a-time 300 DPI rendering."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from math import ceil, isfinite
from pathlib import Path
from typing import Final, Protocol, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import SourceKind
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import PageRef
from omr_grader.domain.session import build_page_ref


class _PdfiumBitmap(Protocol):
    width: int
    height: int
    mode: str

    def to_numpy(self) -> NDArray[np.uint8]: ...

    def close(self) -> None: ...


class _PdfiumPage(Protocol):
    def get_size(self) -> tuple[float, float]: ...

    def render(self, *, scale: float, fill_color: tuple[int, int, int, int]) -> _PdfiumBitmap: ...

    def close(self) -> None: ...


class _PdfiumDocument(Protocol):
    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> _PdfiumPage: ...

    def close(self) -> None: ...


class _PdfiumApi(Protocol):
    PdfiumError: type[Exception]

    def PdfDocument(self, payload: bytes) -> _PdfiumDocument: ...


# PDFium (BSD-3/Apache-2.0) renders the pages; it is the engine Chrome uses.
_pdfium = cast(_PdfiumApi, import_module("pypdfium2"))
# PDFium's FPDF_ERR_PASSWORD: the document needs a password to open.
_PDFIUM_PASSWORD_ERROR: Final = 4
_PDF_RENDERER_FAILURES = (
    _pdfium.PdfiumError,
    RuntimeError,
    OSError,
    MemoryError,
    cv2.error,
)


PDF_RENDER_DPI: Final = 300
MAX_PDF_SOURCE_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_PDF_PAGES: Final = 5_000
MAX_PDF_RENDER_DIMENSION: Final = 32_768
MAX_PDF_RENDER_PIXELS: Final = 100_000_000
MAX_PDF_RENDERED_BYTES: Final = 400 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PdfInput:
    page_ref: PageRef
    source_path: Path
    source_sha256: str
    page_number: int


@dataclass(frozen=True, slots=True)
class PdfInputBatch:
    inputs: tuple[PdfInput, ...]


@dataclass(frozen=True, slots=True)
class RenderedPdfPage:
    pdf_input: PdfInput
    pixels: NDArray[np.uint8]
    width: int
    height: int


def _issue(code: str, reason: str) -> ErrorInfo:
    return ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason})


def _error(code: str, reason: str) -> Err:
    return Err((_issue(code, reason),))


def _read_source(path: Path) -> Result[bytes]:
    try:
        if not path.is_file():
            return _error("PDF_SOURCE_INVALID", "PDF source is not a regular file")
        size = path.stat().st_size
        if size < 0 or size > MAX_PDF_SOURCE_BYTES:
            return _error("INPUT_FILE_BYTES_QUOTA", "PDF source byte quota exceeded")
        with path.open("rb") as stream:
            payload = stream.read(size)
            grew = stream.read(1)
        if len(payload) != size or grew:
            return _error("PDF_SOURCE_UNREADABLE", "PDF source changed while reading")
    except (OSError, MemoryError) as exc:
        return _error("PDF_SOURCE_UNREADABLE", str(exc))
    return Ok(payload)


class _PdfSourceCache:
    """One authenticated payload per streamed source group."""

    def __init__(self) -> None:
        self._sources: dict[Path, Result[bytes]] = {}

    def authenticated(self, pdf_input: PdfInput) -> Result[bytes]:
        source = self._sources.get(pdf_input.source_path)
        if source is None:
            source = _read_source(pdf_input.source_path)
            self._sources[pdf_input.source_path] = source
        if isinstance(source, Err):
            return source
        if sha256(source.value).hexdigest() != pdf_input.source_sha256:
            return _error("SCAN_SOURCE_CHANGED", "source content changed after enumeration")
        return source

    def clear(self) -> None:
        self._sources.clear()


def _close_document(document: _PdfiumDocument) -> Exception | None:
    try:
        document.close()
    except _PDF_RENDERER_FAILURES as exc:
        return exc
    return None


def _open_pdf(payload: bytes) -> Result[_PdfiumDocument]:
    document: _PdfiumDocument | None = None
    try:
        document = _pdfium.PdfDocument(payload)
        page_count = len(document)
    except _PDF_RENDERER_FAILURES as exc:
        if document is not None:
            _close_document(document)
        if getattr(exc, "err_code", None) == _PDFIUM_PASSWORD_ERROR:
            return _error("PDF_ENCRYPTED", "encrypted PDFs are not accepted")
        return _error("PDF_MALFORMED", str(exc))
    if not 1 <= page_count <= MAX_PDF_PAGES:
        close_error = _close_document(document)
        if close_error is not None:
            return _error("PDF_MALFORMED", str(close_error))
        return _error("PDF_PAGE_COUNT_QUOTA", "PDF page count must be between 1 and 5000")
    return Ok(document)


def enumerate_pdf(
    path: Path, session_id: str, *, input_ordinal: int = 0, duplicate_ordinal: int = 0
) -> Result[PdfInputBatch]:
    """Build deterministic refs for every PDF page without rendering any page."""
    if not isinstance(session_id, str) or not session_id:
        return _error("INVALID_SESSION_ID", "session_id must be nonempty")
    if type(input_ordinal) is not int or input_ordinal < 0:
        return _error("INVALID_INPUT_ORDINAL", "input_ordinal must be nonnegative")
    if type(duplicate_ordinal) is not int or duplicate_ordinal < 0:
        return _error("INVALID_DUPLICATE_ORDINAL", "duplicate_ordinal must be nonnegative")
    source = _read_source(path)
    if isinstance(source, Err):
        return source
    document_result = _open_pdf(source.value)
    if isinstance(document_result, Err):
        return document_result
    document = document_result.value
    digest = sha256(source.value).hexdigest()
    try:
        inputs: list[PdfInput] = []
        for zero_based_page in range(len(document)):
            reference = build_page_ref(
                session_id=session_id,
                source_kind=SourceKind.PDF,
                source_sha256=digest,
                source_display_name=path.name,
                page_number=zero_based_page + 1,
                frame_number=None,
                input_ordinal=input_ordinal,
                duplicate_ordinal=duplicate_ordinal,
            )
            if isinstance(reference, Err):
                return reference
            inputs.append(PdfInput(reference.value, path, digest, zero_based_page + 1))
        return Ok(PdfInputBatch(tuple(inputs)))
    finally:
        _close_document(document)


def _dimension_error(width: int, height: int) -> str | None:
    if width < 1 or height < 1:
        return "invalid rendered dimensions"
    if width > MAX_PDF_RENDER_DIMENSION or height > MAX_PDF_RENDER_DIMENSION:
        return "rendered image dimension quota exceeded"
    if width * height > MAX_PDF_RENDER_PIXELS:
        return "rendered image pixel quota exceeded"
    if width * height * 3 > MAX_PDF_RENDERED_BYTES:
        return "rendered image byte quota exceeded"
    return None


def _render_page(pdf_input: PdfInput, page: _PdfiumPage) -> Result[RenderedPdfPage]:
    """Render one open page at 300 DPI on white into a BGR array the caller owns."""
    try:
        scale = PDF_RENDER_DPI / 72
        page_width, page_height = page.get_size()
        if not isfinite(page_width) or not isfinite(page_height):
            return _error("PDF_PAGE_RENDER_FAILED", "PDF page has non-finite dimensions")
        reason = _dimension_error(ceil(page_width * scale), ceil(page_height * scale))
        if reason is not None:
            return _error("PDF_PAGE_RENDER_QUOTA", reason)
        bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
        try:
            reason = _dimension_error(bitmap.width, bitmap.height)
            if reason is not None:
                return _error("PDF_PAGE_RENDER_QUOTA", reason)
            if bitmap.mode != "BGR":
                return _error("PDF_PAGE_RENDER_FAILED", "unexpected PDF renderer pixel layout")
            view = bitmap.to_numpy()
            if view.shape != (bitmap.height, bitmap.width, 3) or view.nbytes > (
                MAX_PDF_RENDERED_BYTES
            ):
                return _error("PDF_PAGE_RENDER_FAILED", "unexpected PDF renderer pixel layout")
            # The view points into PDFium's buffer, which closing the bitmap frees.
            pixels: NDArray[np.uint8] = np.ascontiguousarray(view).copy()
        finally:
            bitmap.close()
        return Ok(RenderedPdfPage(pdf_input, pixels, pixels.shape[1], pixels.shape[0]))
    except (ValueError, MemoryError) as exc:
        return _error("PDF_PAGE_RENDER_FAILED", str(exc))
    finally:
        page.close()


def _render_pdf_page_from_source(pdf_input: PdfInput, payload: bytes) -> Result[RenderedPdfPage]:
    document_result = _open_pdf(payload)
    if isinstance(document_result, Err):
        return document_result
    document = document_result.value
    outcome: Result[RenderedPdfPage] | None = None
    close_error: Exception | None = None
    try:
        if not 1 <= pdf_input.page_number <= len(document):
            outcome = _error("PDF_PAGE_INVALID", "page number is outside the source PDF")
        else:
            try:
                outcome = _render_page(pdf_input, document[pdf_input.page_number - 1])
            except _PDF_RENDERER_FAILURES as exc:
                outcome = _error("PDF_PAGE_RENDER_FAILED", str(exc))
    finally:
        close_error = _close_document(document)

    if close_error is not None and (outcome is None or isinstance(outcome, Ok)):
        outcome = _error("PDF_PAGE_RENDER_FAILED", str(close_error))
    if outcome is None:
        return _error("PDF_PAGE_RENDER_FAILED", "PDF renderer returned no result")
    return outcome


def render_pdf_page(pdf_input: PdfInput) -> Result[RenderedPdfPage]:
    """Render exactly one authenticated source page at 300 DPI."""
    if not isinstance(pdf_input, PdfInput):
        return _error("INVALID_PDF_INPUT", "pdf_input must be PdfInput")
    source = _read_source(pdf_input.source_path)
    if isinstance(source, Err):
        return source
    if sha256(source.value).hexdigest() != pdf_input.source_sha256:
        return _error("SCAN_SOURCE_CHANGED", "source content changed after enumeration")
    return _render_pdf_page_from_source(pdf_input, source.value)


def render_pdf_pages(inputs: tuple[PdfInput, ...]) -> Iterator[Result[RenderedPdfPage]]:
    """Yield pages lazily, retaining no rendered page after the next yield."""

    grouped_inputs: dict[Path, list[PdfInput]] = {}
    for pdf_input in inputs:
        grouped_inputs.setdefault(pdf_input.source_path, []).append(pdf_input)

    source_cache = _PdfSourceCache()
    try:
        for grouped in grouped_inputs.values():
            for pdf_input in grouped:
                source = source_cache.authenticated(pdf_input)
                if isinstance(source, Err):
                    yield source
                else:
                    yield _render_pdf_page_from_source(pdf_input, source.value)
            source_cache.clear()
    finally:
        source_cache.clear()


__all__ = [
    "MAX_PDF_PAGES",
    "MAX_PDF_RENDERED_BYTES",
    "MAX_PDF_RENDER_DIMENSION",
    "MAX_PDF_RENDER_PIXELS",
    "MAX_PDF_SOURCE_BYTES",
    "PDF_RENDER_DPI",
    "PdfInput",
    "PdfInputBatch",
    "RenderedPdfPage",
    "enumerate_pdf",
    "render_pdf_page",
    "render_pdf_pages",
]
