from __future__ import annotations

import gc
import os
import weakref
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np

from omr_grader.domain.errors import Err, Ok
from omr_grader.ingestion import images
from omr_grader.ingestion.images import (
    DecodedImage,
    decode_image,
    decode_images,
    enumerate_image_folder,
    enumerate_image_paths,
)


def _classic_tiff_chain(frame_count: int, *, cycle: bool = False) -> bytes:
    if frame_count < 1:
        return b"II*\0\0\0\0\0"
    ifd_size = 30
    offsets = [8 + index * ifd_size for index in range(frame_count)]
    payload = bytearray(b"II*\0" + offsets[0].to_bytes(4, "little"))
    for index, offset in enumerate(offsets):
        assert len(payload) == offset
        next_offset = offsets[index + 1] if index + 1 < frame_count else 0
        if cycle and index + 1 == frame_count:
            next_offset = offsets[0]
        payload.extend((2).to_bytes(2, "little"))
        for tag, value in ((256, 5), (257, 3)):
            payload.extend(tag.to_bytes(2, "little"))
            payload.extend((4).to_bytes(2, "little"))
            payload.extend((1).to_bytes(4, "little"))
            payload.extend(value.to_bytes(4, "little"))
        payload.extend(next_offset.to_bytes(4, "little"))
    return bytes(payload)


def _big_tiff(width: int, height: int) -> bytes:
    payload = bytearray(b"II+\0\x08\0\0\0" + (16).to_bytes(8, "little"))
    payload.extend((2).to_bytes(8, "little"))
    for tag, value in ((256, width), (257, height)):
        payload.extend(tag.to_bytes(2, "little"))
        payload.extend((4).to_bytes(2, "little"))
        payload.extend((1).to_bytes(8, "little"))
        payload.extend(value.to_bytes(4, "little") + b"\0" * 4)
    payload.extend(b"\0" * 8)
    return bytes(payload)


def _big_endian_short_tiff(width: int, height: int, *, big_tiff: bool) -> bytes:
    if big_tiff:
        payload = bytearray(b"MM\0+\0\x08\0\0" + (16).to_bytes(8, "big"))
        payload.extend((2).to_bytes(8, "big"))
        count_size, value_size, next_size = 8, 8, 8
    else:
        payload = bytearray(b"MM\0*" + (8).to_bytes(4, "big"))
        payload.extend((2).to_bytes(2, "big"))
        count_size, value_size, next_size = 4, 4, 4
    for tag, value in ((256, width), (257, height)):
        payload.extend(tag.to_bytes(2, "big"))
        payload.extend((3).to_bytes(2, "big"))
        payload.extend((1).to_bytes(count_size, "big"))
        payload.extend(value.to_bytes(2, "big") + b"\0" * (value_size - 2))
    payload.extend(b"\0" * next_size)
    return bytes(payload)


class _RecordingStream(BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def test_authenticated_hash_is_limited_to_snapshot_and_detects_stream_changes(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"abc")
    expected = (1, 2, 3, 4)

    grown = _RecordingStream(b"abcx")
    monkeypatch.setattr(Path, "open", lambda _path, _mode: grown)
    monkeypatch.setattr(images, "_snapshot", lambda _path: Ok(expected))
    growth = images._hash_authenticated(source, expected)
    assert isinstance(growth, Err)
    assert growth.errors[0].code == "SCAN_SOURCE_CHANGED"
    assert grown.read_sizes == [3, 1]

    short = _RecordingStream(b"ab")
    monkeypatch.setattr(Path, "open", lambda _path, _mode: short)
    early_eof = images._hash_authenticated(source, expected)
    assert isinstance(early_eof, Err)
    assert early_eof.errors[0].code == "SCAN_SOURCE_CHANGED"
    assert short.read_sizes == [3, 1]

    stable = _RecordingStream(b"abc")
    snapshots: list[Path] = []
    monkeypatch.setattr(Path, "open", lambda _path, _mode: stable)
    monkeypatch.setattr(images, "_snapshot", lambda path: snapshots.append(path) or Ok(expected))
    authenticated = images._hash_authenticated(source, expected)
    assert isinstance(authenticated, Ok)
    assert stable.read_sizes == [3, 1]
    assert snapshots == [source]


def test_tiff_preflight_detects_cycles_and_distinguishes_frame_quota() -> None:
    cyclic = images.preflight_tiff(_classic_tiff_chain(2, cycle=True))
    assert isinstance(cyclic, Err)
    assert cyclic.errors[0].code == "IMAGE_MALFORMED"

    bounded = images.preflight_tiff(_classic_tiff_chain(256))
    assert isinstance(bounded, Ok)
    assert bounded.value.frame_count == 256
    assert bounded.value.dimensions == (5, 3)

    quota = images.preflight_tiff(_classic_tiff_chain(257))
    assert isinstance(quota, Err)
    assert quota.errors[0].code == "TIFF_FRAME_QUOTA"


def test_tiff_preflight_reads_bigtiff_metadata() -> None:
    result = images.preflight_tiff(_big_tiff(17, 19))
    assert isinstance(result, Ok)
    assert result.value.frame_count == 1
    assert result.value.dimensions == (17, 19)


def test_tiff_preflight_reads_big_endian_inline_short_dimensions() -> None:
    for big_tiff in (False, True):
        result = images.preflight_tiff(_big_endian_short_tiff(17, 19, big_tiff=big_tiff))
        assert isinstance(result, Ok)
        assert result.value.dimensions == (17, 19)


def _write(path: Path, color: tuple[int, int, int] = (0, 0, 0)) -> None:
    image = np.full((3, 5, 3), color, dtype=np.uint8)
    assert cv2.imwrite(str(path), image)


def test_folder_is_nonrecursive_sorted_and_ignores_unsupported(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    _write(tmp_path / "z.PNG")
    _write(tmp_path / "A.jpg")
    _write(nested / "not-included.png")
    (tmp_path / "notes.txt").write_text("no")

    result = enumerate_image_folder(tmp_path, "session-1")

    assert isinstance(result, Ok)
    assert [item.page_ref.source_display_name for item in result.value.inputs] == ["A.jpg", "z.PNG"]
    assert [item.page_ref.input_ordinal for item in result.value.inputs] == [0, 1]


def test_duplicate_explicit_inputs_keep_unique_deterministic_identities(tmp_path: Path) -> None:
    source = tmp_path / "same.png"
    _write(source)

    result = enumerate_image_paths((source, source), "session-1")

    assert isinstance(result, Ok)
    first, second = result.value.inputs
    assert first.source_sha256 == second.source_sha256
    assert first.page_ref.duplicate_ordinal == 0
    assert second.page_ref.duplicate_ordinal == 1
    assert first.page_ref.work_item_id != second.page_ref.work_item_id


def test_decode_batch_is_lazy_and_releases_prior_rasters(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _write(first)
    _write(second)
    enumerated = enumerate_image_paths((first, second), "session-1")
    assert isinstance(enumerated, Ok)

    calls: list[object] = []
    raster_refs: list[weakref.ReferenceType[np.ndarray]] = []

    def fake_decode(scan_input: images.ScanInput) -> Ok[DecodedImage]:
        calls.append(scan_input)
        pixels = np.zeros((1, 1, 3), dtype=np.uint8)
        raster_refs.append(weakref.ref(pixels))
        return Ok(DecodedImage(scan_input, pixels, 1, 1))

    monkeypatch.setattr(images, "decode_image", fake_decode)
    batch = decode_images(enumerated.value.inputs)
    assert calls == []

    decoded = next(batch.decoded)
    assert calls == [enumerated.value.inputs[0]]
    del decoded
    next(batch.decoded)
    gc.collect()

    assert raster_refs[0]() is None
    assert batch.failures == ()


def test_decode_preserves_source_pixels_and_does_not_apply_rotation(tmp_path: Path) -> None:
    source = tmp_path / "rotated.png"
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    pixels[0, 2] = (10, 20, 30)
    assert cv2.imwrite(str(source), pixels)
    enumerated = enumerate_image_paths((source,), "session-1")
    assert isinstance(enumerated, Ok)

    decoded = decode_image(enumerated.value.inputs[0])

    assert isinstance(decoded, Ok)
    assert decoded.value.pixels.shape == (2, 3, 3)
    assert tuple(decoded.value.pixels[0, 2]) == (10, 20, 30)


def test_tiff_uses_first_frame_and_warns(tmp_path: Path) -> None:
    source = tmp_path / "scan.tiff"
    assert cv2.imwritemulti(
        str(source), [np.zeros((2, 2), dtype=np.uint8), np.ones((2, 2), dtype=np.uint8)]
    )
    enumerated = enumerate_image_paths((source,), "session-1")
    assert isinstance(enumerated, Ok)

    decoded = decode_image(enumerated.value.inputs[0])

    assert isinstance(decoded, Ok)
    assert decoded.value.scan_input.page_ref.frame_number == 1
    assert [warning.code for warning in decoded.warnings] == ["TIFF_ADDITIONAL_FRAMES_IGNORED"]


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\0\0\0\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def test_image_quotas_are_rejected_before_decode_with_bounded_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "quota.png"
    source.write_bytes(_png_header(4, 4))

    monkeypatch.setattr(images, "MAX_IMAGE_DIMENSION", 3)
    enumerated = enumerate_image_paths((source,), "session-1")
    assert isinstance(enumerated, Ok)
    assert isinstance(decode_image(enumerated.value.inputs[0]), Err)
    assert decode_image(enumerated.value.inputs[0]).errors[0].code == "IMAGE_DIMENSION_QUOTA"

    monkeypatch.setattr(images, "MAX_IMAGE_DIMENSION", 10)
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 15)
    assert (
        decode_image(enumerated.value.inputs[0]).errors[0].context["reason"]
        == "image pixel quota exceeded"
    )

    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(images, "MAX_DECODED_BYTES", 63)
    assert (
        decode_image(enumerated.value.inputs[0]).errors[0].context["reason"]
        == "decoded image byte quota exceeded"
    )


def test_image_source_bytes_malformed_payload_and_replacement_are_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    oversized = tmp_path / "oversized.png"
    oversized.write_bytes(b"x" * 33)
    monkeypatch.setattr(images, "MAX_SOURCE_BYTES", 32)
    enumerated = enumerate_image_paths((oversized,), "session-1")
    assert isinstance(enumerated, Ok)
    assert enumerated.value.failures[0].code == "INPUT_FILE_BYTES_QUOTA"

    monkeypatch.undo()
    malformed = tmp_path / "malformed.png"
    malformed.write_bytes(b"not an image")
    accepted = enumerate_image_paths((malformed,), "session-1")
    assert isinstance(accepted, Ok)
    decoded = decode_image(accepted.value.inputs[0])
    assert isinstance(decoded, Err)
    assert decoded.errors[0].code == "IMAGE_MALFORMED"

    source = tmp_path / "replaced.png"
    _write(source)
    original = enumerate_image_paths((source,), "session-1")
    assert isinstance(original, Ok)
    _write(source, (1, 2, 3))
    replaced = decode_image(original.value.inputs[0])
    assert isinstance(replaced, Err)
    assert replaced.errors[0].code == "SCAN_SOURCE_CHANGED"


def test_decode_uses_authenticated_bytes_when_path_is_swapped_with_same_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.bmp"
    _write(source, (1, 2, 3))
    replacement = tmp_path / "replacement.bmp"
    _write(replacement, (4, 5, 6))
    original_payload = source.read_bytes()
    replacement_payload = replacement.read_bytes()
    assert len(original_payload) == len(replacement_payload)
    original_stat = source.stat()
    enumerated = enumerate_image_paths((source,), "session-1")
    assert isinstance(enumerated, Ok)
    original_decode = cv2.imdecode

    def swap_after_binding(encoded: np.ndarray, flags: int) -> np.ndarray | None:
        source.write_bytes(replacement_payload)
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return original_decode(encoded, flags)

    monkeypatch.setattr(images.cv2, "imdecode", swap_after_binding)
    decoded = decode_image(enumerated.value.inputs[0])

    assert isinstance(decoded, Ok)
    assert tuple(decoded.value.pixels[0, 0]) == (1, 2, 3)
