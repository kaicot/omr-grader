"""Exact, English-domain schemas for response workbook boundaries."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Final
from xml.etree import ElementTree

from omr_grader.domain.enums import AnswerStatus
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import AnswerValue

RESPONSE_SHEET_NAME: Final = "응답원본"
MAX_QUESTIONS: Final = 100
RESPONSE_FIXED_HEADERS: Final = ("일련번호", "원본파일명", "학번", "이름")


def question_headers(question_count: int) -> tuple[str, ...]:
    """``Q1`` .. ``Q<question_count>``."""
    return tuple(f"Q{number}" for number in range(1, question_count + 1))


def response_headers(question_count: int = MAX_QUESTIONS) -> tuple[str, ...]:
    """The 응답원본 header row for a form with ``question_count`` questions."""
    if not 1 <= question_count <= MAX_QUESTIONS:
        raise ValueError("question_count must be 1..100")
    return (*RESPONSE_FIXED_HEADERS, *question_headers(question_count), "비고")


def question_count(answer_rows: Iterable[Sequence[AnswerValue]]) -> int:
    """How many questions the books show: up to the last one any answer sheet prints.

    Questions past the form read unasked on every sheet, so a 40-question form gives 40.
    With no rows at all the books keep the full 100 columns.
    """
    last = 0
    for answers in answer_rows:
        for number, answer in enumerate(answers, 1):
            if number > last and answer.status is not AnswerStatus.UNASKED:
                last = number
    return last or MAX_QUESTIONS


RESPONSE_HEADERS: Final = response_headers()
RESPONSE_COLUMN_COUNT: Final = len(RESPONSE_HEADERS)
RESPONSE_POLICY_VERSION: Final = "response-xlsx-policy-v1"
MAX_COMPRESSED_BYTES: Final = 64 * 1024 * 1024
MAX_ZIP_ENTRIES: Final = 2_048
MAX_ENTRY_UNCOMPRESSED_BYTES: Final = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES: Final = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 100
MAX_SHEETS: Final = 32
MAX_RELATIONSHIPS: Final = 4_096
MAX_DEFINED_NAMES: Final = 1_024
MAX_SELECTED_ROWS: Final = 10_001
MAX_OTHER_ROWS: Final = 10_000
MAX_OTHER_COLUMNS: Final = 256
MAX_SELECTED_CELLS: Final = 1_050_105
MAX_WORKBOOK_CELLS: Final = 2_000_000
MAX_SHARED_STRINGS: Final = 250_000
MAX_SHARED_STRING_BYTES: Final = 8 * 1024
MAX_SHARED_STRING_TOTAL_BYTES: Final = 32 * 1024 * 1024
MAX_FORMULA_CELLS: Final = 10_000
MAX_FORMULA_BYTES: Final = 8 * 1024 * 1024
_FORMULA_PREFIXES: Final = ("=", "+", "-", "@")
_OPC_RELATIONSHIPS_NAMESPACE: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_OPC_RELATIONSHIPS_TAG: Final = f"{{{_OPC_RELATIONSHIPS_NAMESPACE}}}Relationships"
_OPC_RELATIONSHIP_TAG: Final = f"{{{_OPC_RELATIONSHIPS_NAMESPACE}}}Relationship"
_OPC_RELATIONSHIP_ATTRIBUTES: Final = frozenset({"Id", "Type", "Target", "TargetMode"})
_OPC_QUALIFIED_RELATIONSHIP_ATTRIBUTES: Final = frozenset(
    f"{{{_OPC_RELATIONSHIPS_NAMESPACE}}}{name}" for name in _OPC_RELATIONSHIP_ATTRIBUTES
)


def _package_error() -> ErrorInfo:
    return ErrorInfo("XLSX_INVALID_PACKAGE", "error.xlsx_invalid_package", "path")


def _is_whitespace(value: str | None) -> bool:
    return value is None or not value.strip()


def validate_opc_relationships(payload: bytes) -> Result[bool]:
    """Strictly parse OPC relationships and report whether one is external."""
    try:
        parser = ElementTree.XMLParser(
            target=ElementTree.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        root = ElementTree.fromstring(payload, parser=parser)
    except ElementTree.ParseError:
        return Err((_package_error(),))
    if (
        root.tag != _OPC_RELATIONSHIPS_TAG
        or root.attrib
        or not _is_whitespace(root.text)
        or not _is_whitespace(root.tail)
    ):
        return Err((_package_error(),))
    relationship_ids: set[str] = set()
    has_external = False
    for element in root:
        if (
            element.tag != _OPC_RELATIONSHIP_TAG
            or len(element) != 0
            or not _is_whitespace(element.text)
            or not _is_whitespace(element.tail)
        ):
            return Err((_package_error(),))
        attributes: dict[str, str] = {}
        for name, value in element.attrib.items():
            if name in _OPC_RELATIONSHIP_ATTRIBUTES:
                local_name = name
            elif name in _OPC_QUALIFIED_RELATIONSHIP_ATTRIBUTES:
                local_name = name.rsplit("}", 1)[-1]
            else:
                return Err((_package_error(),))
            if local_name in attributes:
                return Err((_package_error(),))
            attributes[local_name] = value
        if not all(attributes.get(name, "").strip() for name in ("Id", "Type", "Target")):
            return Err((_package_error(),))
        relationship_id = attributes["Id"].strip()
        if relationship_id in relationship_ids:
            return Err((_package_error(),))
        relationship_ids.add(relationship_id)
        target_mode = attributes.get("TargetMode")
        if target_mode is None or target_mode.strip().casefold() == "internal":
            continue
        if target_mode.strip().casefold() == "external":
            has_external = True
            continue
        return Err((_package_error(),))
    return Ok(has_external)


def normalize_header(value: object) -> str | None:
    """NFC-normalize and outer-trim a header without accepting non-text cells."""
    if not isinstance(value, str):
        return None
    return unicodedata.normalize("NFC", value).strip()


def normalize_text(value: str) -> str:
    """Canonical import text: CRLF, NFKC, then Unicode outer whitespace trim."""
    return unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def has_prohibited_control(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Cc" and character not in {"\n", "\t"}
        for character in value
    )


def escape_formula_text(value: str) -> str:
    """Return safe projection display text without altering canonical truth text."""
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def sheet_extent(sheet: Any) -> tuple[int, int]:
    """Rows and columns a worksheet uses, counted when the file does not record them.

    Excel always records the used range. Files written by some other tools leave it out, and
    openpyxl's read-only sheets then report no size at all; the used range is then the last
    row and column that hold a cell.
    """
    rows, columns = sheet.max_row, sheet.max_column
    if isinstance(rows, int) and isinstance(columns, int):
        return rows, columns
    rows = columns = 0
    for row in sheet.iter_rows():
        for cell in row:
            row_number = getattr(cell, "row", None)
            column_number = getattr(cell, "column", None)
            if isinstance(row_number, int) and isinstance(column_number, int):
                rows = max(rows, row_number)
                columns = max(columns, column_number)
    return rows, columns


__all__ = [
    name
    for name in globals()
    if name.startswith(
        (
            "MAX_",
            "RESPONSE_",
            "escape_",
            "has_",
            "normalize_",
            "question_",
            "response_",
            "sheet_",
            "validate_",
        )
    )
]
