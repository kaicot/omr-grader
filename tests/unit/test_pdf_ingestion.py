from __future__ import annotations

import gc
import weakref
from pathlib import Path

import numpy as np
import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.ingestion import pdf
from omr_grader.ingestion.pdf import PDF_RENDER_DPI, PdfInput, enumerate_pdf, render_pdf_page
from tests.helpers.pdf_writer import pdf_bytes, write_pdf


def _pdf(path: Path, pages: int = 1) -> None:
    write_pdf(path, [None] * pages, page_size=(72, 144))


def test_pdf_pages_have_stable_distinct_page_refs_and_render_at_300_dpi(tmp_path: Path) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source, 2)

    enumerated = enumerate_pdf(source, "session-1", input_ordinal=3)

    assert isinstance(enumerated, Ok)
    assert [item.page_ref.page_number for item in enumerated.value.inputs] == [1, 2]
    assert (
        enumerated.value.inputs[0].page_ref.work_item_id
        != enumerated.value.inputs[1].page_ref.work_item_id
    )
    rendered = render_pdf_page(enumerated.value.inputs[0])
    assert isinstance(rendered, Ok)
    assert (rendered.value.width, rendered.value.height) == (PDF_RENDER_DPI, PDF_RENDER_DPI * 2)


def test_pages_render_in_bgr_on_white_and_follow_the_page_rotation(tmp_path: Path) -> None:
    image = np.full((60, 120, 3), 255, dtype=np.uint8)
    image[20:40, 40:80] = (255, 0, 0)  # blue in BGR
    source = write_pdf(tmp_path / "scan.pdf", [image, None], page_size=(72, 36))
    turned = write_pdf(tmp_path / "turned.pdf", [image], page_size=(72, 36), rotation=90)

    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    page = render_pdf_page(enumerated.value.inputs[0])
    blank = render_pdf_page(enumerated.value.inputs[1])
    sideways = enumerate_pdf(turned, "session-1")
    assert isinstance(sideways, Ok)
    rotated = render_pdf_page(sideways.value.inputs[0])

    assert isinstance(page, Ok) and isinstance(blank, Ok) and isinstance(rotated, Ok)
    pixels = page.value.pixels
    assert pixels.shape == (150, 300, 3) and pixels.flags.c_contiguous
    assert tuple(pixels[75, 150]) == (255, 0, 0)
    assert tuple(pixels[5, 5]) == (255, 255, 255)
    assert int(blank.value.pixels.min()) == 255
    assert (rotated.value.width, rotated.value.height) == (150, 300)


def test_encrypted_and_malformed_pdfs_are_rejected_without_rendering(tmp_path: Path) -> None:
    encrypted = write_pdf(tmp_path / "encrypted.pdf", [None], encrypted=True)
    malformed = tmp_path / "bad.pdf"
    malformed.write_bytes(b"not a pdf")

    locked = enumerate_pdf(encrypted, "session-1")
    broken = enumerate_pdf(malformed, "session-1")

    assert isinstance(locked, Err)
    assert locked.errors[0].code == "PDF_ENCRYPTED"
    assert isinstance(broken, Err)
    assert broken.errors[0].code == "PDF_MALFORMED"


def test_render_rejects_replaced_source(tmp_path: Path) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    _pdf(source, 2)

    rendered = render_pdf_page(enumerated.value.inputs[0])

    assert isinstance(rendered, Err)
    assert rendered.errors[0].code == "SCAN_SOURCE_CHANGED"


class _FakeDocument:
    """The slice of ``pypdfium2.PdfDocument`` the ingestion uses."""

    def __init__(
        self,
        pages: int = 1,
        *,
        load_fault: Exception | None = None,
        close_fault: Exception | None = None,
        page: _FakePage | None = None,
    ) -> None:
        self.pages = pages
        self.load_fault = load_fault
        self.close_fault = close_fault
        self.page = page
        self.closed = False

    def __len__(self) -> int:
        return self.pages

    def __getitem__(self, _: int) -> _FakePage:
        if self.load_fault is not None:
            raise self.load_fault
        return self.page if self.page is not None else _FakePage()

    def close(self) -> None:
        self.closed = True
        if self.close_fault is not None:
            raise self.close_fault


class _FakeBitmap:
    def __init__(self, width: int, height: int, mode: str, array: np.ndarray | None) -> None:
        self.width = width
        self.height = height
        self.mode = mode
        self.array = array
        self.closed = False

    def to_numpy(self) -> np.ndarray:
        if self.array is None:
            raise ValueError("buffer is gone")
        return self.array

    def close(self) -> None:
        self.closed = True


class _FakePage:
    def __init__(
        self,
        *,
        size: tuple[float, float] = (2 * 72 / PDF_RENDER_DPI, 2 * 72 / PDF_RENDER_DPI),
        render_fault: Exception | None = None,
        mode: str = "BGR",
        array: np.ndarray | None = None,
    ) -> None:
        self.size = size
        self.render_fault = render_fault
        self.mode = mode
        self.array = np.zeros((2, 2, 3), dtype=np.uint8) if array is None else array
        self.bitmaps: list[_FakeBitmap] = []
        self.closed = False

    def get_size(self) -> tuple[float, float]:
        return self.size

    def render(self, **_: object) -> _FakeBitmap:
        if self.render_fault is not None:
            raise self.render_fault
        height, width = self.array.shape[:2]
        bitmap = _FakeBitmap(width, height, self.mode, self.array)
        self.bitmaps.append(bitmap)
        return bitmap

    def close(self) -> None:
        self.closed = True


def test_pdf_byte_page_empty_and_invalid_page_limits_use_bounded_fakes(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    monkeypatch.setattr(pdf, "MAX_PDF_SOURCE_BYTES", 1)
    rejected = enumerate_pdf(source, "session-1")
    assert isinstance(rejected, Err)
    assert rejected.errors[0].code == "INPUT_FILE_BYTES_QUOTA"

    empty_document = _FakeDocument(0)
    monkeypatch.setattr(pdf._pdfium, "PdfDocument", lambda _: empty_document)
    empty = pdf._open_pdf(b"%PDF")
    assert isinstance(empty, Err)
    assert empty.errors[0].code == "PDF_PAGE_COUNT_QUOTA"
    assert empty_document.closed

    monkeypatch.setattr(pdf._pdfium, "PdfDocument", lambda _: _FakeDocument(2))
    monkeypatch.setattr(pdf, "MAX_PDF_PAGES", 1)
    too_many = pdf._open_pdf(b"%PDF")
    assert isinstance(too_many, Err)
    assert too_many.errors[0].code == "PDF_PAGE_COUNT_QUOTA"

    monkeypatch.undo()
    _pdf(source)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    invalid = PdfInput(
        enumerated.value.inputs[0].page_ref,
        source,
        enumerated.value.inputs[0].source_sha256,
        2,
    )
    rendered = render_pdf_page(invalid)
    assert isinstance(rendered, Err)
    assert rendered.errors[0].code == "PDF_PAGE_INVALID"


def test_pdf_render_quota_and_renderer_layout_are_rejected_before_array_allocation(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    pdf_input = enumerated.value.inputs[0]

    page = _FakePage()
    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(_FakeDocument(page=page)))
    monkeypatch.setattr(pdf, "MAX_PDF_RENDER_DIMENSION", 1)
    quota = render_pdf_page(pdf_input)
    assert isinstance(quota, Err)
    assert quota.errors[0].code == "PDF_PAGE_RENDER_QUOTA"
    # The quota is checked from the page size, before PDFium allocates a bitmap.
    assert page.bitmaps == [] and page.closed

    monkeypatch.setattr(pdf, "MAX_PDF_RENDER_DIMENSION", 10)
    monkeypatch.setattr(pdf, "MAX_PDF_RENDERED_BYTES", 11)
    byte_quota = render_pdf_page(pdf_input)
    assert isinstance(byte_quota, Err)
    assert byte_quota.errors[0].code == "PDF_PAGE_RENDER_QUOTA"
    monkeypatch.setattr(pdf, "MAX_PDF_RENDERED_BYTES", 100)

    # A bitmap larger than the page size promised is still refused.
    oversized = _FakePage(array=np.zeros((20, 2, 3), dtype=np.uint8))
    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(_FakeDocument(page=oversized)))
    layout = render_pdf_page(pdf_input)
    assert isinstance(layout, Err)
    assert layout.errors[0].code == "PDF_PAGE_RENDER_QUOTA"
    assert all(bitmap.closed for bitmap in oversized.bitmaps)

    for wrong in (
        _FakePage(mode="BGRA", array=np.zeros((2, 2, 4), dtype=np.uint8)),
        _FakePage(array=np.zeros((2, 3, 3), dtype=np.uint8)[:, :2]),
        _FakePage(array=np.zeros((2, 2), dtype=np.uint8)),
    ):
        monkeypatch.setattr(pdf, "_open_pdf", lambda _, page=wrong: Ok(_FakeDocument(page=page)))
        rejected = render_pdf_page(pdf_input)
        if wrong.array.ndim == 3 and wrong.mode == "BGR":
            # A strided view is copied into a compact array of its own.
            assert isinstance(rejected, Ok) and rejected.value.pixels.flags.c_contiguous
        else:
            assert isinstance(rejected, Err)
            assert rejected.errors[0].code == "PDF_PAGE_RENDER_FAILED"
        assert all(bitmap.closed for bitmap in wrong.bitmaps) and wrong.closed


def test_rendered_pixels_outlive_the_pdfium_bitmap(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    buffer = np.full((2, 2, 3), 7, dtype=np.uint8)
    monkeypatch.setattr(
        pdf, "_open_pdf", lambda _: Ok(_FakeDocument(page=_FakePage(array=buffer)))
    )

    rendered = render_pdf_page(enumerated.value.inputs[0])
    buffer[:] = 0  # PDFium reuses or frees the buffer once the bitmap is closed

    assert isinstance(rendered, Ok)
    assert int(rendered.value.pixels.min()) == 7


def test_pdf_renderer_failures_are_translated_and_programmer_faults_surface(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    pdf_input = enumerated.value.inputs[0]

    def raise_runtime_error(_: object) -> object:
        raise RuntimeError("renderer fault")

    monkeypatch.setattr(pdf._pdfium, "PdfDocument", raise_runtime_error)
    malformed = pdf._open_pdf(b"%PDF")
    assert isinstance(malformed, Err)
    assert malformed.errors[0].code == "PDF_MALFORMED"

    def raise_pdfium_error(_: object) -> object:
        raise pdf._pdfium.PdfiumError("Failed to load document", err_code=3)

    monkeypatch.setattr(pdf._pdfium, "PdfDocument", raise_pdfium_error)
    data_error = pdf._open_pdf(b"%PDF")
    assert isinstance(data_error, Err)
    assert data_error.errors[0].code == "PDF_MALFORMED"

    def raise_attribute_error(_: object) -> object:
        raise AttributeError("programmer fault")

    monkeypatch.setattr(pdf._pdfium, "PdfDocument", raise_attribute_error)
    with pytest.raises(AttributeError, match="programmer fault"):
        pdf._open_pdf(b"%PDF")
    monkeypatch.undo()

    monkeypatch.setattr(
        pdf, "_open_pdf", lambda _: Ok(_FakeDocument(load_fault=RuntimeError("load fault")))
    )
    load_fault = render_pdf_page(pdf_input)
    assert isinstance(load_fault, Err)
    assert load_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"

    faulty = _FakePage(render_fault=RuntimeError("render fault"))
    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(_FakeDocument(page=faulty)))
    render_fault = render_pdf_page(pdf_input)
    assert isinstance(render_fault, Err)
    assert render_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"
    assert faulty.closed

    gone = _FakePage()
    gone.array = None  # type: ignore[assignment]
    gone.render = lambda **_: _FakeBitmap(2, 2, "BGR", None)  # type: ignore[method-assign]
    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(_FakeDocument(page=gone)))
    materialize_fault = render_pdf_page(pdf_input)
    assert isinstance(materialize_fault, Err)
    assert materialize_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"

    monkeypatch.setattr(
        pdf,
        "_open_pdf",
        lambda _: Ok(_FakeDocument(load_fault=AttributeError("programmer fault"))),
    )
    with pytest.raises(AttributeError, match="programmer fault"):
        render_pdf_page(pdf_input)

    monkeypatch.setattr(
        pdf, "_open_pdf", lambda _: Ok(_FakeDocument(close_fault=OSError("close fault")))
    )
    close_fault = render_pdf_page(pdf_input)
    assert isinstance(close_fault, Err)
    assert close_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"


def test_pdf_bytes_helper_is_read_by_pdfium_like_a_scanner_pdf() -> None:
    document = pdf._pdfium.PdfDocument(pdf_bytes([None, None], page_size=(100, 50)))
    try:
        assert len(document) == 2
        assert document[0].get_size() == (100, 50)
    finally:
        document.close()


def test_pdf_source_read_uses_one_immutable_allocation(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)

    def duplicate_allocation(*_: object) -> bytearray:
        raise MemoryError("duplicate source allocation")

    monkeypatch.setattr(pdf, "bytearray", duplicate_allocation, raising=False)

    loaded = pdf._read_source(source)

    assert isinstance(loaded, Ok)
    assert isinstance(loaded.value, bytes)


def test_pdf_source_read_requests_exact_measured_size_then_growth_probe(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    payload = source.read_bytes()
    measured_size = source.stat().st_size

    class ReaderSpy:
        def __init__(self) -> None:
            self.offset = 0
            self.requests: list[int] = []

        def __enter__(self) -> ReaderSpy:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            self.requests.append(size)
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    reader = ReaderSpy()
    monkeypatch.setattr(Path, "open", lambda *_: reader)

    loaded = pdf._read_source(source)

    assert isinstance(loaded, Ok)
    assert loaded.value == payload
    assert reader.requests == [measured_size, 1]
    assert max(reader.requests) == measured_size
    assert pdf.MAX_PDF_SOURCE_BYTES + 1 not in reader.requests


def test_pdf_source_read_rejects_shrinking_source_after_measured_read(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    payload = source.read_bytes()
    measured_size = source.stat().st_size

    class ReaderSpy:
        def __init__(self) -> None:
            self.requests: list[int] = []

        def __enter__(self) -> ReaderSpy:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            self.requests.append(size)
            if size == measured_size:
                return payload[:-1]
            return b""

    reader = ReaderSpy()
    monkeypatch.setattr(Path, "open", lambda *_: reader)

    loaded = pdf._read_source(source)

    assert isinstance(loaded, Err)
    assert loaded.errors[0].code == "PDF_SOURCE_UNREADABLE"
    assert reader.requests == [measured_size, 1]


def test_pdf_source_read_rejects_growing_source_after_measured_read(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    payload = source.read_bytes()
    measured_size = source.stat().st_size

    class ReaderSpy:
        def __init__(self) -> None:
            self.requests: list[int] = []

        def __enter__(self) -> ReaderSpy:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            self.requests.append(size)
            if size == measured_size:
                return payload
            return b"!"

    reader = ReaderSpy()
    monkeypatch.setattr(Path, "open", lambda *_: reader)

    loaded = pdf._read_source(source)

    assert isinstance(loaded, Err)
    assert loaded.errors[0].code == "PDF_SOURCE_UNREADABLE"
    assert reader.requests == [measured_size, 1]


def test_pdf_source_memory_error_is_a_typed_result(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)

    class MemoryFailingStream:
        def __enter__(self) -> MemoryFailingStream:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            raise MemoryError("source allocation failed")

    monkeypatch.setattr(Path, "open", lambda *_: MemoryFailingStream())

    loaded = pdf._read_source(source)

    assert isinstance(loaded, Err)
    assert loaded.errors[0].code == "PDF_SOURCE_UNREADABLE"


def test_pdf_batch_yields_pages_without_retaining_prior_rasters(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source, 2)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)

    source_reads = 0
    original_read_source = pdf._read_source

    def counted_read_source(path: Path):
        nonlocal source_reads
        source_reads += 1
        return original_read_source(path)

    def render_with_fresh_raster(pdf_input: PdfInput, _: bytes):
        pixels = np.zeros((1, 1, 3), dtype=np.uint8)
        return Ok(pdf.RenderedPdfPage(pdf_input, pixels, 1, 1))

    monkeypatch.setattr(pdf, "_read_source", counted_read_source)
    monkeypatch.setattr(pdf, "_render_pdf_page_from_source", render_with_fresh_raster)

    rendered = pdf.render_pdf_pages(enumerated.value.inputs)
    assert source_reads == 0
    first = next(rendered)
    assert isinstance(first, Ok)
    first_pixels = weakref.ref(first.value.pixels)
    assert source_reads == 1

    del first
    gc.collect()

    second = next(rendered)
    assert isinstance(second, Ok)
    assert first_pixels() is None
    assert source_reads == 1
