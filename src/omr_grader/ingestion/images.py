"""Bounded, deterministic image scan ingestion for scan-input-policy-v1."""

from __future__ import annotations

import mmap
import os
import struct
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import SourceKind
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import PageRef
from omr_grader.domain.session import build_page_ref

POLICY_VERSION: Final = "scan-input-policy-v1"
IMAGE_EXTENSIONS: Final = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"})
MAX_SOURCE_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_IMAGE_DIMENSION: Final = 32_768
MAX_IMAGE_PIXELS: Final = 100_000_000
MAX_DECODED_BYTES: Final = 400 * 1024 * 1024
MAX_BATCH_FILES: Final = 5_000
MAX_BATCH_ENCODED_BYTES: Final = 100 * 1024 * 1024 * 1024
MAX_PATH_COMPONENT_UTF16_CODE_UNITS: Final = 255
MAX_TIFF_FRAMES: Final = 256


@dataclass(frozen=True, slots=True)
class ScanInput:
    """One immutable source unit; TIFF always denotes its first frame."""

    page_ref: PageRef
    source_path: Path
    source_sha256: str
    source_kind: SourceKind
    # st_dev/st_ino/size/mtime_ns captured before the source digest.  It detects a
    # same-content replacement as well as a changed byte stream.
    source_snapshot: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class InputFailure:
    source_path: Path
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImageInputBatch:
    inputs: tuple[ScanInput, ...]
    failures: tuple[InputFailure, ...]


@dataclass(frozen=True, slots=True)
class DecodedImage:
    scan_input: ScanInput
    pixels: NDArray[np.uint8]
    width: int
    height: int


def _failure_reason(error: ErrorInfo) -> str:
    reason = error.context.get("reason")
    return reason if type(reason) is str else "unspecified failure"


@dataclass(frozen=True, slots=True)
class DecodeFailure:
    page_ref: PageRef
    code: str
    reason: str


def _issue(code: str, reason: str, **context: str | int) -> ErrorInfo:
    return ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason, **context})


def _error(code: str, reason: str, **context: str | int) -> Err:
    return Err((_issue(code, reason, **context),))


def _display_label(path: Path) -> str:
    # Labels deliberately omit absolute machine paths.  Folder enumeration supplies
    # direct children, so a basename is also its relative display label.
    return unicodedata.normalize("NFC", path.name)


def _sort_key(path: Path) -> tuple[str, bytes]:
    label = _display_label(path)
    return (label.casefold(), os.fsencode(label))


def _snapshot(path: Path) -> Result[tuple[int, int, int, int]]:
    try:
        if not path.is_file():
            return _error("SCAN_SOURCE_INVALID", "source is not a regular file")
        status = path.stat()
    except OSError as exc:
        return _error("SCAN_SOURCE_UNREADABLE", str(exc))
    if status.st_size < 0:
        return _error("SCAN_SOURCE_UNREADABLE", "source has a negative size")
    return Ok((status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns))


def _path_component_error(path: Path) -> Err | None:
    for component in path.parts:
        try:
            units = len(component.encode("utf-16-le")) // 2
        except UnicodeEncodeError:
            return _error("INPUT_PATH_COMPONENT_QUOTA", "path component is not UTF-16 encodable")
        if units > MAX_PATH_COMPONENT_UTF16_CODE_UNITS:
            return _error(
                "INPUT_PATH_COMPONENT_QUOTA",
                "path component UTF-16 quota exceeded",
                limit_utf16=MAX_PATH_COMPONENT_UTF16_CODE_UNITS,
                actual_utf16=units,
            )
    return None


def _hash_authenticated(path: Path, expected: tuple[int, int, int, int]) -> Result[str]:
    digest = sha256()
    remaining = expected[2]
    try:
        with path.open("rb") as stream:
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    return _error("SCAN_SOURCE_CHANGED", "source shrank while being authenticated")
                if len(block) > remaining:
                    return _error("SCAN_SOURCE_CHANGED", "source grew while being authenticated")
                digest.update(block)
                remaining -= len(block)
            if stream.read(1):
                return _error("SCAN_SOURCE_CHANGED", "source grew while being authenticated")
    except OSError as exc:
        return _error("SCAN_SOURCE_UNREADABLE", str(exc))
    current = _snapshot(path)
    if isinstance(current, Err):
        return current
    if current.value != expected:
        return _error("SCAN_SOURCE_CHANGED", "source changed while being authenticated")
    return Ok(digest.hexdigest())


def _dimension_error(width: int, height: int, channels: int = 4) -> tuple[str, str] | None:
    if width < 1 or height < 1:
        return ("IMAGE_DIMENSION_QUOTA", "invalid image dimensions")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        return ("IMAGE_DIMENSION_QUOTA", "image dimension quota exceeded")
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS:
        return ("IMAGE_PIXEL_QUOTA", "image pixel quota exceeded")
    if channels < 1 or channels > 4 or pixels * channels > MAX_DECODED_BYTES:
        return ("IMAGE_DECODED_BYTES_QUOTA", "decoded image byte quota exceeded")
    return None


def _jpeg_dimensions(payload: bytes | mmap.mmap) -> tuple[int, int] | None:
    if len(payload) < 4 or payload[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 <= len(payload):
        if payload[offset] != 0xFF:
            return None
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return None
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            return None
        length = int.from_bytes(payload[offset : offset + 2], "big")
        if length < 2 or offset + length > len(payload):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            return (
                int.from_bytes(payload[offset + 5 : offset + 7], "big"),
                int.from_bytes(payload[offset + 3 : offset + 5], "big"),
            )
        offset += length
    return None


@dataclass(frozen=True, slots=True)
class TiffPreflight:
    frame_count: int
    dimensions: tuple[int, int]


def preflight_tiff(payload: bytes | mmap.mmap) -> Result[TiffPreflight]:
    if len(payload) < 8 or payload[:2] not in {b"II", b"MM"}:
        return _error("IMAGE_MALFORMED", "TIFF header is malformed")
    byte_order: Literal["little", "big"] = "little" if payload[:2] == b"II" else "big"
    endian = "<" if byte_order == "little" else ">"
    magic = struct.unpack_from(f"{endian}H", payload, 2)[0]
    if magic == 42:
        offset = struct.unpack_from(f"{endian}I", payload, 4)[0]
        offset_size, count_size, entry_size = 4, 2, 12
    elif (
        magic == 43
        and len(payload) >= 16
        and struct.unpack_from(f"{endian}H", payload, 4)[0] == 8
        and struct.unpack_from(f"{endian}H", payload, 6)[0] == 0
    ):
        offset = struct.unpack_from(f"{endian}Q", payload, 8)[0]
        offset_size, count_size, entry_size = 8, 8, 20
    else:
        return _error("IMAGE_MALFORMED", "TIFF header is malformed")

    offsets: set[int] = set()
    dimensions: tuple[int, int] | None = None
    while offset:
        if offset in offsets:
            return _error("IMAGE_MALFORMED", "TIFF IFD chain contains a cycle")
        if offset > len(payload) - count_size:
            return _error("IMAGE_MALFORMED", "TIFF IFD offset is malformed")
        entries = int.from_bytes(payload[offset : offset + count_size], byte_order)
        entries_start = offset + count_size
        next_offset_at = entries_start + entries * entry_size
        if entries > 4096 or next_offset_at > len(payload) - offset_size:
            return _error("IMAGE_MALFORMED", "TIFF IFD entries are malformed")
        offsets.add(offset)
        if len(offsets) > MAX_TIFF_FRAMES:
            return _error("TIFF_FRAME_QUOTA", "TIFF frame quota exceeded")
        if len(offsets) == 1:
            values: dict[int, int] = {}
            for index in range(entries):
                entry = entries_start + index * entry_size
                tag, field_type = struct.unpack_from(f"{endian}HH", payload, entry)
                item_count = int.from_bytes(
                    payload[entry + 4 : entry + 4 + offset_size], byte_order
                )
                if tag in {256, 257} and item_count == 1 and field_type in {3, 4, 16}:
                    size = {3: 2, 4: 4, 16: 8}[field_type]
                    value_at = entry + 4 + offset_size
                    raw = payload[value_at : value_at + size]
                    values[tag] = int.from_bytes(raw, byte_order)
            if 256 in values and 257 in values:
                dimensions = (values[256], values[257])
        offset = int.from_bytes(payload[next_offset_at : next_offset_at + offset_size], byte_order)
    if not offsets:
        return _error("TIFF_NO_FRAME", "TIFF contains no frames")
    if dimensions is None:
        return _error("IMAGE_MALFORMED", "TIFF first-frame dimensions are unavailable")
    return Ok(TiffPreflight(len(offsets), dimensions))


def _preflight(payload: bytes | mmap.mmap, kind: SourceKind) -> Result[TiffPreflight | None]:
    tiff_info: TiffPreflight | None = None
    dimensions: tuple[int, int] | None
    if kind is SourceKind.TIFF:
        tiff = preflight_tiff(payload)
        if isinstance(tiff, Err):
            return tiff
        tiff_info = tiff.value
        dimensions = tiff_info.dimensions
    elif payload[:8] == b"\x89PNG\r\n\x1a\n" and len(payload) >= 24:
        dimensions = struct.unpack(">II", payload[16:24])
    elif payload[:2] == b"BM" and len(payload) >= 26:
        width, height = struct.unpack("<ii", payload[18:26])
        dimensions = (abs(width), abs(height))
    else:
        dimensions = _jpeg_dimensions(payload)
    if dimensions is None:
        return _error("IMAGE_MALFORMED", "image header is unsupported or malformed")
    failure = _dimension_error(*dimensions)
    if failure:
        return _error(*failure)
    return Ok(tiff_info)


def _kind(path: Path) -> SourceKind:
    return SourceKind.TIFF if path.suffix.casefold() in {".tif", ".tiff"} else SourceKind.IMAGE


def enumerate_image_paths(paths: tuple[Path, ...], session_id: str) -> Result[ImageInputBatch]:
    if not isinstance(session_id, str) or not session_id:
        return _error("INVALID_SESSION_ID", "session_id must be nonempty")
    ordered = tuple(
        sorted(
            (path for path in paths if path.suffix.casefold() in IMAGE_EXTENSIONS), key=_sort_key
        )
    )
    if len(ordered) > MAX_BATCH_FILES:
        return _error("INPUT_BATCH_FILES_QUOTA", "input file-count quota exceeded")

    snapshots: list[tuple[int, Path, tuple[int, int, int, int]]] = []
    failures: list[InputFailure] = []
    total_bytes = 0
    for ordinal, path in enumerate(ordered):
        path_failure = _path_component_error(path)
        if path_failure is not None:
            failures.append(
                InputFailure(
                    path, path_failure.errors[0].code, _failure_reason(path_failure.errors[0])
                )
            )
            continue
        snapshot = _snapshot(path)
        if isinstance(snapshot, Err):
            failures.append(
                InputFailure(path, snapshot.errors[0].code, _failure_reason(snapshot.errors[0]))
            )
            continue
        size = snapshot.value[2]
        if size > MAX_SOURCE_BYTES:
            failures.append(
                InputFailure(path, "INPUT_FILE_BYTES_QUOTA", "source byte quota exceeded")
            )
            continue
        if total_bytes > MAX_BATCH_ENCODED_BYTES - size:
            return _error("INPUT_BATCH_BYTES_QUOTA", "input batch byte quota exceeded")
        total_bytes += size
        snapshots.append((ordinal, path, snapshot.value))

    inputs: list[ScanInput] = []
    duplicate_counts: dict[str, int] = {}
    for ordinal, path, source_snapshot in snapshots:
        digest = _hash_authenticated(path, source_snapshot)
        if isinstance(digest, Err):
            failures.append(
                InputFailure(path, digest.errors[0].code, _failure_reason(digest.errors[0]))
            )
            continue
        display_name = _display_label(path)
        duplicate_ordinal = duplicate_counts.get(display_name, 0)
        duplicate_counts[display_name] = duplicate_ordinal + 1
        kind = _kind(path)
        reference = build_page_ref(
            session_id=session_id,
            source_kind=kind,
            source_sha256=digest.value,
            source_display_name=display_name,
            page_number=None,
            frame_number=1 if kind is SourceKind.TIFF else None,
            input_ordinal=ordinal,
            duplicate_ordinal=duplicate_ordinal,
        )
        if isinstance(reference, Err):
            failures.append(
                InputFailure(path, reference.errors[0].code, _failure_reason(reference.errors[0]))
            )
            continue
        inputs.append(ScanInput(reference.value, path, digest.value, kind, source_snapshot))
    return Ok(ImageInputBatch(tuple(inputs), tuple(failures)))


def enumerate_image_folder(folder: Path, session_id: str) -> Result[ImageInputBatch]:
    try:
        if not folder.is_dir():
            return _error("SCAN_FOLDER_INVALID", "image source folder does not exist")
        children = tuple(child for child in folder.iterdir() if child.is_file())
    except OSError as exc:
        return _error("SCAN_FOLDER_UNREADABLE", str(exc))
    return enumerate_image_paths(children, session_id)


@dataclass(frozen=True, slots=True)
class ImageDecodeBatch:
    decoded: Iterator[DecodedImage]
    _failures: list[DecodeFailure]

    @property
    def failures(self) -> tuple[DecodeFailure, ...]:
        return tuple(self._failures)


def decode_images(inputs: tuple[ScanInput, ...]) -> ImageDecodeBatch:
    failures: list[DecodeFailure] = []

    def decoded_images() -> Iterator[DecodedImage]:
        for scan_input in inputs:
            result = decode_image(scan_input)
            if isinstance(result, Err):
                failure = result.errors[0]
                failures.append(
                    DecodeFailure(scan_input.page_ref, failure.code, _failure_reason(failure))
                )
            else:
                yield result.value

    return ImageDecodeBatch(decoded_images(), failures)


def decode_image(scan_input: ScanInput) -> Result[DecodedImage]:
    if not isinstance(scan_input, ScanInput):
        return _error("INVALID_SCAN_INPUT", "scan_input must be ScanInput")
    snapshot = _snapshot(scan_input.source_path)
    if isinstance(snapshot, Err):
        return snapshot
    if snapshot.value[2] > MAX_SOURCE_BYTES:
        return _error("INPUT_FILE_BYTES_QUOTA", "source byte quota exceeded")
    if scan_input.source_snapshot and snapshot.value != scan_input.source_snapshot:
        return _error("SCAN_SOURCE_CHANGED", "source was replaced after enumeration")
    if snapshot.value[2] == 0:
        return _error("IMAGE_MALFORMED", "image source is empty")
    additional_tiff_frames = False
    try:
        with scan_input.source_path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            opened_snapshot = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            if opened_snapshot != snapshot.value:
                return _error("SCAN_SOURCE_CHANGED", "source was replaced before decode")
            payload = stream.read(snapshot.value[2])
            if len(payload) != snapshot.value[2] or stream.read(1):
                return _error("SCAN_SOURCE_CHANGED", "source changed while being read for decode")
    except (OSError, MemoryError) as exc:
        return _error("SCAN_SOURCE_UNREADABLE", str(exc))
    digest = sha256(payload).hexdigest()
    if digest != scan_input.source_sha256:
        return _error("SCAN_SOURCE_CHANGED", "source content changed after enumeration")
    valid = _preflight(payload, scan_input.source_kind)
    if isinstance(valid, Err):
        return valid
    if scan_input.source_kind is SourceKind.TIFF:
        additional_tiff_frames = valid.value is not None and valid.value.frame_count > 1
    try:
        encoded = np.frombuffer(payload, dtype=np.uint8)
        try:
            decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        finally:
            del encoded
    except (cv2.error, MemoryError, ValueError) as exc:
        return _error("IMAGE_MALFORMED", str(exc))
    del payload
    if (
        decoded is None
        or decoded.dtype != np.uint8
        or decoded.ndim != 3
        or decoded.shape[2] not in {1, 2, 3, 4}
    ):
        return _error("IMAGE_MALFORMED", "image decoder did not produce uint8 interleaved pixels")
    pixels = cast(NDArray[np.uint8], decoded)
    height, width = pixels.shape[:2]
    failure = _dimension_error(width, height, pixels.shape[2])
    if failure or pixels.nbytes > MAX_DECODED_BYTES:
        return _error(
            *(failure or ("IMAGE_DECODED_BYTES_QUOTA", "decoded image byte quota exceeded"))
        )
    current = _snapshot(scan_input.source_path)
    if isinstance(current, Err) or current.value != snapshot.value:
        return _error("SCAN_SOURCE_CHANGED", "source changed during decode")
    warnings: tuple[ErrorInfo, ...] = ()
    if additional_tiff_frames:
        warnings = (
            ErrorInfo(
                "TIFF_ADDITIONAL_FRAMES_IGNORED",
                "warning.tiff_additional_frames_ignored",
                context={"reason": "only TIFF frame 1 is processed"},
            ),
        )
    return Ok(DecodedImage(scan_input, pixels, width, height), warnings)


__all__ = [
    "DecodeFailure",
    "DecodedImage",
    "IMAGE_EXTENSIONS",
    "ImageDecodeBatch",
    "ImageInputBatch",
    "InputFailure",
    "MAX_BATCH_ENCODED_BYTES",
    "MAX_BATCH_FILES",
    "MAX_DECODED_BYTES",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "MAX_PATH_COMPONENT_UTF16_CODE_UNITS",
    "MAX_SOURCE_BYTES",
    "MAX_TIFF_FRAMES",
    "POLICY_VERSION",
    "ScanInput",
    "TiffPreflight",
    "decode_image",
    "decode_images",
    "enumerate_image_folder",
    "enumerate_image_paths",
    "preflight_tiff",
]
