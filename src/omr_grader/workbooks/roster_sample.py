"""Deterministic, strict XLSX sample generator for student roster workbooks."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.ingestion.roster import ROSTER_HEADERS

SAMPLE_SHEET_NAME = "학생명단"
_SAMPLE_ROWS = (
    ("1", "20260001", "김하늘"),
    ("2", "02026002", "이봄"),
    ("3", "20260003", "박여름"),
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_WORKBOOK_TIMESTAMP = datetime(1980, 1, 1, tzinfo=UTC)
_DCTERMS = "http://purl.org/dc/terms/"
_CORE_TIMESTAMP = "1980-01-01T00:00:00Z"


def _error(code: str, field: str | None = None) -> ErrorInfo:
    return ErrorInfo(code, f"error.{code.lower()}", field)


def _normalized_member(name: str, payload: bytes) -> bytes:
    if name != "docProps/core.xml":
        return payload
    properties = ElementTree.fromstring(payload)
    for property_name in ("created", "modified"):
        element = properties.find(f"{{{_DCTERMS}}}{property_name}")
        if element is not None:
            element.text = _CORE_TIMESTAMP
    return bytes(ElementTree.tostring(properties, encoding="utf-8", xml_declaration=True))


def _deterministic_package(data: bytes) -> bytes:
    """Normalize ZIP metadata that otherwise varies between workbook saves."""
    source = BytesIO(data)
    destination = BytesIO()
    with (
        ZipFile(source) as archive,
        ZipFile(destination, "w", compression=ZIP_DEFLATED) as normalized,
    ):
        for name in sorted(archive.namelist()):
            member = ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            member.compress_type = ZIP_DEFLATED
            normalized.writestr(member, _normalized_member(name, archive.read(name)))
    return destination.getvalue()


def roster_sample_bytes(sheet_name: str = SAMPLE_SHEET_NAME) -> bytes:
    """Return a deterministic, formula-free roster sample accepted by the strict importer."""
    workbook = Workbook(write_only=False)
    workbook.properties.created = _WORKBOOK_TIMESTAMP
    workbook.properties.modified = _WORKBOOK_TIMESTAMP
    sheet = workbook.active
    if not isinstance(sheet, Worksheet):
        raise RuntimeError("new workbook must have an active worksheet")
    sheet.title = sheet_name
    sheet.append(ROSTER_HEADERS)
    for row in _SAMPLE_ROWS:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return _deterministic_package(stream.getvalue())


def write_roster_sample(path: str, sheet_name: str = SAMPLE_SHEET_NAME) -> Result[None]:
    """Atomically write the sample; callers never observe a partial workbook."""
    target = Path(path)
    temporary_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("wb", dir=target.parent, delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(roster_sample_bytes(sheet_name))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    except (OSError, TypeError, ValueError, RuntimeError):
        return Err((_error("XLSX_WRITE_FAILED", "path"),))
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return Ok(None)
