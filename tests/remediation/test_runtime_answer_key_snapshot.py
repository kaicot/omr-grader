from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from omr_grader.application.answer_key_use_case import AnswerKeyWorkbookUseCase
from omr_grader.application.dto import AnswerKeyRequest, AnswerKeyValidation
from omr_grader.domain.errors import Err, Ok
from omr_grader.workbooks.answer_key import import_answer_key, import_answer_key_bytes


def _workbook_bytes(answer: int) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "정답표"
    sheet.append(("문항번호", "정답", "배점"))
    sheet.append((1, answer, 1))
    from io import BytesIO

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_answer_key_validation_uses_one_pinned_byte_read(tmp_path: Path) -> None:
    path = tmp_path / "key.xlsx"
    original = _workbook_bytes(1)
    replacement = _workbook_bytes(2)
    path.write_bytes(original)
    reads = 0

    def reader(source: Path) -> bytes:
        nonlocal reads
        reads += 1
        payload = source.read_bytes()
        source.write_bytes(replacement)
        return payload

    validated = AnswerKeyWorkbookUseCase(reader=reader).validate_answer_key(
        AnswerKeyRequest(str(path), "정답표")
    )

    assert isinstance(validated, Ok)
    assert reads == 1
    assert validated.value.source_bytes == original
    assert validated.value.snapshot.entries[0].answer.choices == (1,)
    assert path.read_bytes() == replacement


def test_answer_key_bytes_parser_and_validation_reject_malformed_or_mismatched_source() -> None:
    malformed = import_answer_key_bytes(b"not-an-xlsx", "key.xlsx", "정답표")
    assert isinstance(malformed, Err)

    payload = _workbook_bytes(1)
    parsed = import_answer_key_bytes(payload, "key.xlsx", "정답표")
    assert isinstance(parsed, Ok)
    with pytest.raises(ValueError, match="does not match"):
        AnswerKeyValidation(parsed.value, "key.xlsx", _workbook_bytes(2))


def test_path_api_remains_a_compatibility_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "key.xlsx"
    path.write_bytes(_workbook_bytes(1))
    parsed = import_answer_key(str(path), "정답표")
    assert isinstance(parsed, Ok)
    assert parsed.value.source_name == "key.xlsx"
