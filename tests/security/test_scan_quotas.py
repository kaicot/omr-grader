from __future__ import annotations

import struct
from hashlib import sha256
from pathlib import Path

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.ingestion import images
from omr_grader.ingestion.images import ScanInput, decode_image, enumerate_image_paths


def _input(path: Path) -> ScanInput:
    result = enumerate_image_paths((path,), "session-1")
    assert isinstance(result, Ok)
    return result.value.inputs[0]


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


@pytest.mark.parametrize(
    "size, expected",
    [(3, "IMAGE_MALFORMED"), (4, "IMAGE_MALFORMED"), (5, "INPUT_FILE_BYTES_QUOTA")],
)
def test_source_byte_quota_n_minus_one_n_n_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, expected: str
) -> None:
    monkeypatch.setattr(images, "MAX_SOURCE_BYTES", 4)
    path = tmp_path / "scan.png"
    path.write_bytes(b"x" * size)
    reference = _input(path) if size <= 4 else None

    if reference is None:
        enumerated = enumerate_image_paths((path,), "session-1")
        assert isinstance(enumerated, Ok)
        assert enumerated.value.failures[0].code == expected
    else:
        result = decode_image(reference)
        assert isinstance(result, Err)
        assert result.errors[0].code == expected


@pytest.mark.parametrize(
    "width, expected",
    [(9, "IMAGE_MALFORMED"), (10, "IMAGE_MALFORMED"), (11, "IMAGE_DIMENSION_QUOTA")],
)
def test_dimension_quota_n_minus_one_n_n_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int, expected: str
) -> None:
    monkeypatch.setattr(images, "MAX_IMAGE_DIMENSION", 10)
    path = tmp_path / "scan.png"
    path.write_bytes(_png_header(width, 1))
    scan_input = _input(path)

    result = decode_image(scan_input)

    assert isinstance(result, Err)
    assert result.errors[0].code == expected


@pytest.mark.parametrize(
    "width, expected", [(4, "IMAGE_MALFORMED"), (5, "IMAGE_MALFORMED"), (6, "IMAGE_PIXEL_QUOTA")]
)
def test_pixel_quota_n_minus_one_n_n_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int, expected: str
) -> None:
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 5)
    path = tmp_path / "scan.png"
    path.write_bytes(_png_header(width, 1))
    scan_input = _input(path)

    result = decode_image(scan_input)

    assert isinstance(result, Err)
    assert result.errors[0].code == expected


def test_decoded_byte_quota_is_checked_before_decoder_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(images, "MAX_DECODED_BYTES", 19)
    path = tmp_path / "bomb.png"
    payload = _png_header(5, 1)
    path.write_bytes(payload)
    input_page = _input(path)
    assert input_page.source_sha256 == sha256(payload).hexdigest()

    result = decode_image(input_page)

    assert isinstance(result, Err)
    assert result.errors[0].code == "IMAGE_DECODED_BYTES_QUOTA"
