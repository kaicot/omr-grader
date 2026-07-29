from __future__ import annotations

import gc
import weakref
from pathlib import Path

import cv2
import fitz
import numpy as np
import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.ingestion import pdf
from omr_grader.ingestion.pdf import PDF_RENDER_DPI, PdfInput, enumerate_pdf, render_pdf_page


def _pdf(path: Path, pages: int = 1) -> None:
    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=72, height=144)
    document.save(path)
    document.close()


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


def test_encrypted_and_malformed_pdfs_are_rejected_without_rendering(tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted.pdf"
    document = fitz.open()
    document.new_page()
    document.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    document.close()
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


def test_pdf_byte_page_empty_and_invalid_page_limits_use_bounded_fakes(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    monkeypatch.setattr(pdf, "MAX_PDF_SOURCE_BYTES", 1)
    rejected = enumerate_pdf(source, "session-1")
    assert isinstance(rejected, Err)
    assert rejected.errors[0].code == "INPUT_FILE_BYTES_QUOTA"

    class FakeDocument:
        def __init__(self, pages: int) -> None:
            self.needs_pass = False
            self.page_count = pages
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(pdf._fitz, "open", lambda **_: FakeDocument(0))
    empty = pdf._open_pdf(b"%PDF")
    assert isinstance(empty, Err)
    assert empty.errors[0].code == "PDF_PAGE_COUNT_QUOTA"

    monkeypatch.setattr(pdf._fitz, "open", lambda **_: FakeDocument(2))
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

    class Rect:
        width = 2
        height = 2

    class Pixmap:
        width = 2
        height = 2
        n = 4
        stride = 8
        samples = b"\0" * 16

    class Page:
        rect = Rect()

        def get_pixmap(self, **_):
            return Pixmap()

    class Document:
        needs_pass = False
        page_count = 1

        def close(self) -> None:
            pass

        def load_page(self, _: int) -> Page:
            return Page()

    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(Document()))
    monkeypatch.setattr(pdf, "MAX_PDF_RENDER_DIMENSION", 1)
    quota = render_pdf_page(enumerated.value.inputs[0])
    assert isinstance(quota, Err)
    assert quota.errors[0].code == "PDF_PAGE_RENDER_QUOTA"
    monkeypatch.setattr(pdf, "MAX_PDF_RENDER_DIMENSION", 10)
    monkeypatch.setattr(pdf, "MAX_PDF_RENDERED_BYTES", 11)
    byte_quota = render_pdf_page(enumerated.value.inputs[0])
    assert isinstance(byte_quota, Err)
    assert byte_quota.errors[0].code == "PDF_PAGE_RENDER_QUOTA"

    monkeypatch.setattr(pdf, "MAX_PDF_RENDERED_BYTES", 100)

    monkeypatch.setattr(pdf, "MAX_PDF_RENDER_DIMENSION", 10)
    layout = render_pdf_page(enumerated.value.inputs[0])
    assert isinstance(layout, Err)
    assert layout.errors[0].code == "PDF_PAGE_RENDER_QUOTA"


def test_pdf_renderer_failures_are_translated_and_programmer_faults_surface(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "answers.pdf"
    _pdf(source)
    enumerated = enumerate_pdf(source, "session-1")
    assert isinstance(enumerated, Ok)
    pdf_input = enumerated.value.inputs[0]

    def raise_runtime_error(**_: object) -> object:
        raise RuntimeError("renderer fault")

    monkeypatch.setattr(pdf._fitz, "open", raise_runtime_error)
    malformed = pdf._open_pdf(b"%PDF")
    assert isinstance(malformed, Err)
    assert malformed.errors[0].code == "PDF_MALFORMED"

    def raise_attribute_error(**_: object) -> object:
        raise AttributeError("programmer fault")

    monkeypatch.setattr(pdf._fitz, "open", raise_attribute_error)
    with pytest.raises(AttributeError, match="programmer fault"):
        pdf._open_pdf(b"%PDF")

    class Rect:
        width = 2
        height = 2

    class Pixmap:
        width = 2
        height = 2
        n = 3
        stride = 6
        samples = b"\0" * 12

    class Page:
        rect = Rect()

        def __init__(self, render_fault: Exception | None = None, samples: bytes | None = None):
            self.render_fault = render_fault
            self.samples = samples

        def get_pixmap(self, **_: object) -> Pixmap:
            if self.render_fault is not None:
                raise self.render_fault
            pixmap = Pixmap()
            if self.samples is not None:
                pixmap.samples = self.samples
            return pixmap

    class Document:
        needs_pass = False
        page_count = 1

        def __init__(
            self,
            load_fault: Exception | None = None,
            close_fault: Exception | None = None,
            page: Page | None = None,
        ):
            self.load_fault = load_fault
            self.close_fault = close_fault
            self.page = page

        def close(self) -> None:
            if self.close_fault is not None:
                raise self.close_fault

        def load_page(self, _: int) -> Page:
            if self.load_fault is not None:
                raise self.load_fault
            return self.page if self.page is not None else Page()

    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(Document(RuntimeError("load fault"))))
    load_fault = render_pdf_page(pdf_input)
    assert isinstance(load_fault, Err)
    assert load_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"

    monkeypatch.setattr(
        pdf, "_open_pdf", lambda _: Ok(Document(page=Page(RuntimeError("render fault"))))
    )
    render_fault = render_pdf_page(pdf_input)
    assert isinstance(render_fault, Err)
    assert render_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"

    monkeypatch.setattr(pdf, "_open_pdf", lambda _: Ok(Document(page=Page(samples=b""))))
    materialize_fault = render_pdf_page(pdf_input)
    assert isinstance(materialize_fault, Err)
    assert materialize_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"

    def raise_cv_error(*_: object) -> object:
        raise cv2.error("conversion fault")

    with monkeypatch.context() as conversion_patch:
        conversion_patch.setattr(pdf, "_open_pdf", lambda _: Ok(Document()))
        conversion_patch.setattr(pdf.cv2, "cvtColor", raise_cv_error)
        convert_fault = render_pdf_page(pdf_input)
    assert isinstance(convert_fault, Err)
    assert convert_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"

    monkeypatch.setattr(
        pdf, "_open_pdf", lambda _: Ok(Document(AttributeError("programmer fault")))
    )
    with pytest.raises(AttributeError, match="programmer fault"):
        render_pdf_page(pdf_input)

    monkeypatch.setattr(
        pdf, "_open_pdf", lambda _: Ok(Document(close_fault=OSError("close fault")))
    )
    close_fault = render_pdf_page(pdf_input)
    assert isinstance(close_fault, Err)
    assert close_fault.errors[0].code == "PDF_PAGE_RENDER_FAILED"


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
