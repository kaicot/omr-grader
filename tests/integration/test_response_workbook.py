from __future__ import annotations

from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from tests.unit.test_response_book import _MARKED, _answers, _effective, _imported, _uncertain

from omr_grader.application.dto import ResponseBookRequest
from omr_grader.application.response_import_use_case import ResponseImportUseCase
from omr_grader.domain.enums import AnswerStatus, ExamTerm
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import AnswerValue
from omr_grader.workbooks import response_import
from omr_grader.workbooks.response_book import (
    recognition_note,
    write_effective_response_projection,
    write_response_projection,
)
from omr_grader.workbooks.response_import import parse_response_book
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME


def _workbook(path, rows: list[list[object]]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(list(RESPONSE_HEADERS))
    for row in rows:
        sheet.append(row)
    book.save(path)


def _row(serial: int, student_id: str = "00123456", answer: object = "13") -> list[object]:
    return [serial, "scan-01.jpg", student_id, "홍길동", *([answer] + [""] * 99), ""]


def test_response_workbook_roundtrips_canonical_rows_and_text_id(tmp_path) -> None:
    path = tmp_path / "responses.xlsx"
    _workbook(path, [_row(7), _row(8, "00123456", "2")])
    with path.open("rb") as source:
        parsed = parse_response_book(
            source,
            sheet_name=RESPONSE_SHEET_NAME,
            session_id="import-session",
            source_sha256=sha256(path.read_bytes()).hexdigest(),
        )
    assert isinstance(parsed, Ok)
    assert [row.serial for row in parsed.value] == [7, 8]
    assert parsed.value[0].raw_student_id == "00123456"
    assert parsed.value[0].answers[0].choices == (1, 3)
    assert parsed.value[0].answers[0].status is AnswerStatus.MULTIPLE
    assert parsed.value[0].work_item_id != parsed.value[1].work_item_id


def test_response_workbook_rejects_numeric_id_without_normalizing(tmp_path) -> None:
    path = tmp_path / "numeric-id.xlsx"
    _workbook(path, [_row(1, 12345678)])
    with path.open("rb") as source:
        parsed = parse_response_book(
            source,
            sheet_name=RESPONSE_SHEET_NAME,
            session_id="import-session",
            source_sha256=sha256(path.read_bytes()).hexdigest(),
        )
    assert isinstance(parsed, Err)
    assert parsed.errors[0].code == "XLSX_ID_TEXT_REQUIRED"


def test_response_workbook_rejects_duplicate_serial_and_formula(tmp_path) -> None:
    path = tmp_path / "hostile.xlsx"
    _workbook(path, [_row(1), _row(1)])
    book = load_workbook(path)
    book[RESPONSE_SHEET_NAME]["E2"] = "=1+1"
    book.save(path)
    with path.open("rb") as source:
        parsed = parse_response_book(
            source,
            sheet_name=RESPONSE_SHEET_NAME,
            session_id="import-session",
            source_sha256=sha256(path.read_bytes()).hexdigest(),
        )
    assert isinstance(parsed, Err)
    assert parsed.errors[0].code == "XLSX_FORMULA_FORBIDDEN"


def _package(entries: dict[str, str | bytes]) -> BytesIO:
    source = BytesIO()
    with ZipFile(source, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    source.seek(0)
    return source


@pytest.mark.parametrize(
    ("entry_name", "payload", "code"),
    (
        ("xl/VBAPROJECT.BIN", b"x", "XLSX_DANGEROUS_FEATURE"),
        ("ENCRYPTIONINFO", b"x", "XLSX_DANGEROUS_FEATURE"),
        ("xl/EXTERNALLINKS/link1.xml", "<root />", "XLSX_DANGEROUS_FEATURE"),
        ("xl/CONNECTIONS.xml", "<root />", "XLSX_DANGEROUS_FEATURE"),
        ("xl/OLEOBJECTS/object1.bin", b"x", "XLSX_DANGEROUS_FEATURE"),
        ("xl/ACTIVEX/activeX1.bin", b"x", "XLSX_DANGEROUS_FEATURE"),
        (
            "XL/_RELS/WORKBOOK.XML.RELS",
            '<Relationships><Relationship Id="rId1" Type="x" Target="x" '
            'targetmode="external" /></Relationships>',
            "XLSX_DANGEROUS_FEATURE",
        ),
        ("xl/worksheets/sheet1.xml", "<root><F>1+1</F></root>", "XLSX_FORMULA_FORBIDDEN"),
        ("xl/worksheets/sheet1.xml", "<root><DDELINK /></root>", "XLSX_DANGEROUS_FEATURE"),
        ("xl/_rels/workbook.xml.rels", "<not-relationships />", "XLSX_ARCHIVE_QUOTA"),
        (
            "xl/_rels/workbook.xml.rels",
            "<Relationships><unexpected /></Relationships>",
            "XLSX_ARCHIVE_QUOTA",
        ),
        (
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" Type="x" /></Relationships>',
            "XLSX_ARCHIVE_QUOTA",
        ),
    ),
)
def test_response_package_rejects_casefolded_hostile_and_malformed_parts(
    entry_name: str, payload: str | bytes, code: str
) -> None:
    scanned = response_import._scan_package(_package({entry_name: payload}))

    assert isinstance(scanned, Err)
    assert scanned.errors[0].code == code


@pytest.mark.parametrize(
    ("kind", "value", "expected_code"),
    (
        ("serial", True, "XLSX_ROW_INVALID"),
        ("serial", date(2026, 1, 1), "XLSX_ROW_INVALID"),
        ("id", True, "XLSX_ID_TEXT_REQUIRED"),
        ("id", 12345678.0, "XLSX_ID_TEXT_REQUIRED"),
        ("id", date(2026, 1, 1), "XLSX_ID_TEXT_REQUIRED"),
        ("id_error", "#VALUE!", "XLSX_ID_TEXT_REQUIRED"),
        ("answer_error", "13", "XLSX_ROW_INVALID"),
        ("answer", True, "XLSX_ROW_INVALID"),
        ("answer", date(2026, 1, 1), "XLSX_ROW_INVALID"),
    ),
)
def test_response_workbook_rejects_physical_nontext_and_error_cells(
    tmp_path, kind: str, value: object, expected_code: str
) -> None:
    path = tmp_path / f"{kind}.xlsx"
    row = _row(1)
    if kind == "serial":
        row[0] = value
    elif kind.startswith("id"):
        row[2] = value
    else:
        row[4] = value
    _workbook(path, [row])
    if kind.endswith("_error"):
        book = load_workbook(path)
        cell = book[RESPONSE_SHEET_NAME]["C2" if kind == "id_error" else "E2"]
        cell.data_type = "e"
        book.save(path)

    with path.open("rb") as source:
        parsed = parse_response_book(
            source,
            sheet_name=RESPONSE_SHEET_NAME,
            session_id="import-session",
            source_sha256=sha256(path.read_bytes()).hexdigest(),
        )

    assert isinstance(parsed, Err)
    assert parsed.errors[0].code == expected_code


def test_response_workbook_normalizes_text_rejects_unicode_controls_and_preserves_provenance(
    tmp_path,
) -> None:
    path = tmp_path / "canonical.xlsx"
    normalized = _row(1)
    normalized[1] = "  scan\u00a0name.jpg  "
    _workbook(path, [normalized, _row(2)])

    def parse() -> Ok | Err:
        with path.open("rb") as source:
            return parse_response_book(
                source,
                sheet_name=RESPONSE_SHEET_NAME,
                session_id="import-session",
                source_sha256=sha256(path.read_bytes()).hexdigest(),
            )

    first = parse()
    second = parse()
    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value == second.value
    assert first.value[0].source_filename == "scan name.jpg"
    assert [row.input_ordinal for row in first.value] == [0, 1]
    assert [row.row_number for row in first.value] == [2, 3]
    assert all(row.source_sha256 == sha256(path.read_bytes()).hexdigest() for row in first.value)

    _workbook(path, [_row(1, "00123456"), _row(2)])
    book = load_workbook(path)
    book[RESPONSE_SHEET_NAME]["B2"] = "scan\u0080name.jpg"
    book.save(path)
    with path.open("rb") as source:
        rejected = parse_response_book(
            source,
            sheet_name=RESPONSE_SHEET_NAME,
            session_id="import-session",
            source_sha256=sha256(path.read_bytes()).hexdigest(),
        )
    assert isinstance(rejected, Err)
    assert rejected.errors[0].code == "XLSX_ROW_INVALID"


@pytest.mark.parametrize(
    ("entry_count", "expected_code"),
    ((0, None), (1, None), (2, None), (3, None), (4, "XLSX_ARCHIVE_QUOTA")),
)
def test_response_package_zip_entry_quota_boundaries_are_compact_and_fail_closed(
    monkeypatch, entry_count: int, expected_code: str | None
) -> None:
    monkeypatch.setattr(response_import, "MAX_ZIP_ENTRIES", 3)
    scanned = response_import._scan_package(
        _package({f"part-{index}": b"x" for index in range(entry_count)})
    )

    if expected_code is None:
        assert isinstance(scanned, Ok)
    else:
        assert isinstance(scanned, Err)
        assert scanned.errors[0].code == expected_code


@pytest.mark.parametrize(
    ("relationship_count", "expected_code"),
    ((0, None), (1, None), (2, None), (3, None), (4, "XLSX_ARCHIVE_QUOTA")),
)
def test_response_package_relationship_quota_boundaries_are_compact_and_fail_closed(
    monkeypatch, relationship_count: int, expected_code: str | None
) -> None:
    monkeypatch.setattr(response_import, "MAX_RELATIONSHIPS", 3)
    relationships = "".join(
        f'<Relationship Id="r{index}" Type="x" Target="part-{index}" />'
        for index in range(relationship_count)
    )
    scanned = response_import._scan_package(
        _package(
            {"xl/_rels/workbook.xml.rels": (f"<Relationships>{relationships}</Relationships>")}
        )
    )

    if expected_code is None:
        assert isinstance(scanned, Ok)
    else:
        assert isinstance(scanned, Err)
        assert scanned.errors[0].code == expected_code


def _parse_file(path: Path) -> Ok | Err:
    with path.open("rb") as source:
        return parse_response_book(
            source,
            sheet_name=RESPONSE_SHEET_NAME,
            session_id="import-session",
            source_sha256=sha256(path.read_bytes()).hexdigest(),
        )


def _marks(answer: AnswerValue) -> AnswerStatus:
    """The importer sees marks, not recognition status: none, one or several."""
    if not answer.choices:
        return AnswerStatus.BLANK
    return AnswerStatus.NORMAL if len(answer.choices) == 1 else AnswerStatus.MULTIPLE


def _assert_formatted(path: Path) -> None:
    """The book under test really carries the formatting the importer must tolerate."""
    sheet = load_workbook(path)[RESPONSE_SHEET_NAME]
    assert sheet.freeze_panes == "E2"
    assert sheet["A1"].font.b
    assert sheet["G2"].fill.fill_type == "solid"


def _clear_review_notes(path: Path) -> None:
    """What a teacher does after checking the yellow cells: drop '확인 필요: …번'."""
    book = load_workbook(path)
    sheet = book[RESPONSE_SHEET_NAME]
    for row in range(2, sheet.max_row + 1):
        cell = sheet.cell(row, 105)
        parts = str(cell.value or "").split(" / ")
        kept = [part for part in parts if part and not part.startswith("확인 필요:")]
        cell.value = " / ".join(kept) or None
    book.save(path)


def test_recognition_response_book_waits_until_unconfirmed_answers_are_checked(
    tmp_path: Path,
) -> None:
    marked = _answers(_MARKED)
    unread = _answers({})
    withheld = tuple(_uncertain(1) for _ in range(100))
    rows = (
        _imported(1, marked, recognition_note(marked, student_id_valid=True)),
        _imported(2, unread, recognition_note(unread, student_id_valid=False), student_id=""),
        _imported(3, withheld, recognition_note(withheld, student_id_valid=False), student_id=""),
        _imported(4, _answers({1: AnswerValue((2,), AnswerStatus.NORMAL)})),
    )
    path = tmp_path / "recognition.xlsx"
    write_response_projection(
        path, rows, session_id="session", revision=1, manifest_sha256="0" * 64
    )

    _assert_formatted(path)
    refused = _parse_file(path)

    assert isinstance(refused, Err)
    assert refused.errors[0].code == "XLSX_REVIEW_PENDING"
    assert refused.errors[0].field_path == f"{RESPONSE_SHEET_NAME}!2:DA"
    assert "2행 비고에 '확인 필요: 3, 5, 17번'" in str(refused.errors[0].context["reason"])

    _clear_review_notes(path)
    parsed = _parse_file(path)

    _assert_formatted(path)
    assert isinstance(parsed, Ok)
    assert [row.note for row in parsed.value] == ["", "학번 확인 필요", "학번 확인 필요", ""]
    for written, read in zip(rows, parsed.value, strict=True):
        assert (
            read.serial,
            read.source_filename,
            read.raw_student_id,
            read.name,
        ) == (
            written.serial,
            written.source_filename,
            written.raw_student_id,
            written.name,
        )
        assert [answer.choices for answer in read.answers] == [
            answer.choices for answer in written.answers
        ]
        assert [answer.status for answer in read.answers] == [
            _marks(answer) for answer in written.answers
        ]


def test_effective_response_book_with_fills_is_accepted_and_roundtrips(tmp_path: Path) -> None:
    rows = (
        _effective(1, _answers(_MARKED), ("answer_cell:3",)),
        _effective(2, _answers({1: AnswerValue((2,), AnswerStatus.NORMAL)}), student_id="20240002"),
    )
    path = tmp_path / "effective.xlsx"
    write_effective_response_projection(
        path,
        rows,
        session_id="session",
        revision=2,
        manifest_sha256="0" * 64,
        names_by_student_id={"20240001": "가명"},
    )

    parsed = _parse_file(path)

    _assert_formatted(path)
    assert isinstance(parsed, Ok)
    assert [
        (row.serial, row.source_filename, row.raw_student_id, row.name, row.note)
        for row in parsed.value
    ] == [
        (1, "scan-1.png", "20240001", "가명", "수동 수정 반영"),
        (2, "scan-2.png", "20240002", "", ""),
    ]
    for written, read in zip(rows, parsed.value, strict=True):
        assert [answer.choices for answer in read.answers] == [
            answer.choices for answer in written.answers
        ]


def test_start_from_responses_validation_accepts_a_checked_book_with_fills(
    tmp_path: Path,
) -> None:
    marked = _answers(_MARKED)
    path = tmp_path / "start.xlsx"
    write_response_projection(
        path,
        (_imported(1, marked, recognition_note(marked, student_id_valid=True)),),
        session_id="session",
        revision=1,
        manifest_sha256="0" * 64,
    )
    _clear_review_notes(path)

    validation = ResponseImportUseCase(object()).validate_response_book(  # type: ignore[arg-type]
        ResponseBookRequest(str(path), RESPONSE_SHEET_NAME, "시험", 2026, ExamTerm.FIRST)
    )

    _assert_formatted(path)
    assert isinstance(validation, Ok)
    try:
        assert validation.value.row_count == 1
        assert validation.value.normalized_rows[0].note == ""
    finally:
        validation.value.validation_token.close()
