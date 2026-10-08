"""The data-format marker ``Data/FORMAT.json``.

Version 4.2.0 introduced it. A folder without the marker was written by 4.0–4.1, which use the
same layout as format 1, so the marker is simply added. A folder marked with a newer format than
this program understands is opened read-only so an older program never rewrites newer data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.atomic_io import atomic_write_json

DATA_FORMAT = 1
FORMAT_FILENAME = "FORMAT.json"


@dataclass(frozen=True, slots=True)
class DataFormat:
    data_format: int
    written_by: str | None


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", None, context={"reason": reason}),))


def read_data_format(data_dir: Path) -> Result[DataFormat | None]:
    """``None`` when the marker is missing (a 4.0–4.1 folder or a new one)."""
    target = data_dir / FORMAT_FILENAME
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return Ok(None)
    except OSError:
        return _error("DATA_FORMAT_UNREADABLE", "자료 형식 표시(FORMAT.json)를 읽을 수 없습니다.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    number = value.get("data_format") if isinstance(value, dict) else None
    if type(number) is not int or number < 1:
        return _error("DATA_FORMAT_UNREADABLE", "자료 형식 표시(FORMAT.json)가 손상되었습니다.")
    written_by = value.get("written_by") if isinstance(value, dict) else None
    return Ok(DataFormat(number, written_by if isinstance(written_by, str) else None))


def newer_format_reason(found: DataFormat) -> str:
    version = f" {found.written_by}" if found.written_by else ""
    return (
        f"이 자료 폴더는 더 새 버전 OMR Grader{version}에서 만든 자료입니다. 자료를 보호하려고"
        " 읽기 전용으로 엽니다. 새 버전으로 여세요."
    )


def ensure_data_format(data_dir: Path, version: str) -> Result[DataFormat]:
    """Check the marker, writing it for folders that have none.

    ``Err`` means the folder must not be written: it is newer than this program understands, or
    its marker is damaged.
    """
    found = read_data_format(data_dir)
    if isinstance(found, Err):
        return found
    if found.value is not None:
        if found.value.data_format > DATA_FORMAT:
            return _error("DATA_FORMAT_NEWER", newer_format_reason(found.value))
        return Ok(found.value)
    marker = DataFormat(DATA_FORMAT, version)
    written = atomic_write_json(
        data_dir / FORMAT_FILENAME, {"data_format": DATA_FORMAT, "written_by": version}
    )
    if isinstance(written, Err):
        # Not fatal: the folder is still format 1; the marker is written on a later start.
        return Ok(
            marker,
            (
                ErrorInfo(
                    "DATA_FORMAT_UNWRITTEN",
                    "warning.data_format_unwritten",
                    None,
                    context={"reason": "자료 형식 표시(FORMAT.json)를 쓰지 못했습니다."},
                ),
            ),
        )
    return Ok(marker)


__all__ = [
    "DATA_FORMAT",
    "FORMAT_FILENAME",
    "DataFormat",
    "ensure_data_format",
    "newer_format_reason",
    "read_data_format",
]
