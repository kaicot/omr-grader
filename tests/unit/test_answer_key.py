from __future__ import annotations

from datetime import date
from zipfile import ZIP_BZIP2, ZipFile

import pytest
from openpyxl import Workbook

from omr_grader.domain.enums import AnswerStatus, KeyQuestionStatus
from omr_grader.domain.errors import Err, Ok
from omr_grader.workbooks import answer_key


def _book(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "정답표"
    sheet.append(answer_key.ANSWER_KEY_HEADERS)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


def _append_member(path, name, content=b"hostile"):
    with ZipFile(path, "a") as package:
        package.writestr(name, content)


def _mark_member_encrypted(path, name):
    with ZipFile(path) as package:
        member = package.getinfo(name)
    payload = bytearray(path.read_bytes())
    payload[member.header_offset + 6] |= 0x1
    offset = 0
    while True:
        offset = payload.find(b"PK\x01\x02", offset)
        assert offset >= 0
        filename_length = int.from_bytes(payload[offset + 28 : offset + 30], "little")
        filename = bytes(payload[offset + 46 : offset + 46 + filename_length]).decode()
        if filename == name:
            payload[offset + 8] |= 0x1
            path.write_bytes(payload)
            return
        offset += 46 + filename_length


def test_answer_key_normalizes_and_fills_all_questions(tmp_path):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "31", 1.5), (2, "전체", 2), (3, "", 0)])

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Ok)
    entries = result.value.entries
    assert len(entries) == 100
    assert entries[0].answer.choices == (1, 3)
    assert entries[0].answer.status is AnswerStatus.MULTIPLE
    assert entries[0].points == "1.5"
    assert entries[1].status is KeyQuestionStatus.ALL
    assert entries[99].status is KeyQuestionStatus.UNASKED


def test_answer_key_accepts_physical_blank_answer_as_unasked(tmp_path):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, None, 0)])

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Ok)
    entry = result.value.entries[0]
    assert entry.answer.status is AnswerStatus.UNASKED
    assert entry.status is KeyQuestionStatus.UNASKED
    assert entry.points == "0"


def test_answer_key_rejects_duplicate_question_and_n_plus_one_rows(tmp_path, monkeypatch):
    duplicate = tmp_path / "duplicate.xlsx"
    _book(duplicate, [(1, "1", 1), (1, "2", 1)])
    duplicate_result = answer_key.import_answer_key(str(duplicate), "정답표")
    assert isinstance(duplicate_result, Err)
    assert duplicate_result.errors[0].code == "XLSX_QUESTION_INVALID"

    over_limit = tmp_path / "over-limit.xlsx"
    _book(over_limit, [(1, "1", 1), (2, "2", 1)])
    monkeypatch.setattr(answer_key, "MAX_ROWS", 2)
    quota_result = answer_key.import_answer_key(str(over_limit), "정답표")
    assert isinstance(quota_result, Err)
    assert quota_result.errors[0].code == "XLSX_DIMENSION_QUOTA"


@pytest.mark.parametrize(
    ("value", "choices", "status"),
    [
        ("1", (1,), AnswerStatus.NORMAL),
        ("531", (1, 3, 5), AnswerStatus.MULTIPLE),
        ("0", (), AnswerStatus.ALL),
        ("전체", (), AnswerStatus.ALL),
        ("", (), AnswerStatus.UNASKED),
    ],
)
def test_answer_key_answer_semantics_are_canonical(value, choices, status):
    result = answer_key._answer(value, "B2")

    assert isinstance(result, Ok)
    answer, _ = result.value
    assert answer.choices == choices
    assert answer.status is status


@pytest.mark.parametrize("value", ("11", "123456", "6", "A", "가", "1!", " 1", "01"))
def test_answer_key_rejects_duplicate_or_invalid_answer_symbols(value):
    result = answer_key._answer(value, "B2")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_ANSWER_INVALID"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [(0, "0"), (1, "1"), (1.25, "1.25"), (0.000000000001, "0.000000000001")],
)
def test_answer_key_canonicalizes_nonnegative_points(value, canonical):
    result = answer_key._canonical_points(value, "C2")

    assert isinstance(result, Ok)
    assert result.value == canonical


@pytest.mark.parametrize("value", (-1, float("nan"), float("inf"), 0.0000000000001))
def test_answer_key_rejects_negative_nonfinite_or_overprecise_points(value):
    result = answer_key._canonical_points(value, "C2")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_POINTS_INVALID"


@pytest.mark.parametrize(
    "row",
    [
        ("1", "1", 1),
        (True, "1", 1),
        (1, 1, 1),
        (1, True, 1),
        (1, "1", "1"),
        (1, "1", True),
        (1, "1", date(2026, 1, 1)),
        (1, "1", "#DIV/0!"),
    ],
)
def test_answer_key_rejects_nonconforming_physical_cell_types(tmp_path, row):
    path = tmp_path / "key.xlsx"
    _book(path, [row])

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_CELL_TYPE"


def test_answer_key_rejects_unasked_question_with_points(tmp_path):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "", 1)])

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_UNASKED_POINTS"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("xl/vbaProject.bin", b"macro"),
        ("EncryptionInfo", b"encrypted"),
        ("EncryptedPackage", b"encrypted"),
        ("xl/externalLinks/externalLink1.xml", b"link"),
        ("xl/connections2.xml", b"connection"),
        ("xl/embeddings/oleObject1.bin", b"ole"),
        ("xl/ddeLink.xml", b"dde"),
        ("xl/activeX/activeX1.bin", b"activeX"),
        (
            "xl/_rels/workbook-hostile.rels",
            (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                b'relationships"><Relationship Id="rId1" Type="hostile" '
                b'TargetMode="ExTeRnAl" Target="https://hostile.invalid"/></Relationships>'
            ),
        ),
    ],
)
def test_answer_key_rejects_hostile_package_features(tmp_path, name, content):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "1", 1)])
    _append_member(path, name, content)

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_FORBIDDEN_FEATURE"


@pytest.mark.parametrize(
    ("selected_rows", "accepted_rows"),
    [(0, 0), (1, 1), (2, 2), (3, None), (4, None)],
)
def test_answer_key_n_three_row_quota_counts_header_separately(
    tmp_path, monkeypatch, selected_rows, accepted_rows
):
    path = tmp_path / "key.xlsx"
    _book(path, [(index, "1", 1) for index in range(1, selected_rows + 1)])
    monkeypatch.setattr(answer_key, "MAX_ROWS", 3)

    result = answer_key.import_answer_key(str(path), "정답표")

    if accepted_rows is None:
        assert isinstance(result, Err)
        assert result.errors[0].code == "XLSX_DIMENSION_QUOTA"
    else:
        assert isinstance(result, Ok)
        assert (
            sum(entry.status is KeyQuestionStatus.ANSWER for entry in result.value.entries)
            == accepted_rows
        )


def test_answer_key_rejects_malformed_relationship_xml(tmp_path):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "1", 1)])
    _append_member(path, "xl/_rels/hostile.rels", b"<Relationships>")

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_INVALID_PACKAGE"


@pytest.mark.parametrize("name", ("xl/worksheets/encrypted.xml", "irrelevant.bin"))
def test_answer_key_rejects_encrypted_members_before_zip_reads(tmp_path, name):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "1", 1)])
    _append_member(path, name)
    _mark_member_encrypted(path, name)

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_INVALID_PACKAGE"


def test_answer_key_rejects_unsupported_member_compression_before_zip_reads(tmp_path):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "1", 1)])
    with ZipFile(path, "a", compression=ZIP_BZIP2) as package:
        package.writestr("irrelevant.bin", b"hostile")

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_INVALID_PACKAGE"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"<Relationships/>", "XLSX_INVALID_PACKAGE"),
        (
            b'<Relationships xmlns="urn:wrong"><Relationship Id="rId1" Type="type" '
            b'Target="target"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"/>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship xmlns="urn:wrong" Id="rId1" Type="type" '
            b'Target="target"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><relationship Id="rId1" Type="type" Target="target"/>'
            b"</Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target">'
            b"<nested/></Relationship></Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships" Extra="value"/>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships">text</Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target">'
            b"text</Relationship></Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target"/>'
            b"tail</Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target" '
            b'Unknown="value"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship iD="rId1" Type="type" Target="target"/>'
            b"</Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships" xmlns:bad="urn:bad"><Relationship Id="rId1" Type="type" '
            b'Target="target" bad:TargetMode="Internal"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships" xmlns:r="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" r:Id="rId2" Type="type" '
            b'Target="target"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id=" " Type="type" Target="target"/>'
            b"</Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type=" " Target="target"/>'
            b"</Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target=" "/>'
            b"</Relationships>",
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target"/>'
            b'<Relationship Id="rId1" Type="type" Target="target"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target" '
            b'TargetMode="remote"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target" '
            b'TargetMode=" \tExTeRnAl\n"/></Relationships>',
            "XLSX_FORBIDDEN_FEATURE",
        ),
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target" '
            b'TargetMode="External"/><Relationship Id="rId1" Type="type" '
            b'Target="later"/></Relationships>',
            "XLSX_INVALID_PACKAGE",
        ),
    ],
)
def test_answer_key_rejects_nonconforming_opc_relationships(tmp_path, content, code):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "1", 1)])
    _append_member(path, "xl/_rels/hostile.rels", content)

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Err)
    assert result.errors[0].code == code


def test_answer_key_accepts_exact_opc_internal_relationship(tmp_path):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "1", 1)])
    _append_member(
        path,
        "xl/_rels/internal.rels",
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="type" Target="target" '
            b'TargetMode=" InTeRnAl "/></Relationships>'
        ),
    )

    result = answer_key.import_answer_key(str(path), "정답표")

    assert isinstance(result, Ok)


def test_answer_key_formula_and_package_sheet_boundaries(tmp_path, monkeypatch):
    path = tmp_path / "key.xlsx"
    _book(path, [(1, "=1", 1)])
    formula = answer_key.import_answer_key(str(path), "정답표")
    assert isinstance(formula, Err)
    assert formula.errors[0].code == "XLSX_FORMULA_FORBIDDEN"

    _book(path, [(1, "1", 1)])
    data = path.read_bytes()
    for size, code in ((0, "XLSX_PACKAGE_QUOTA"), (1, "XLSX_INVALID_PACKAGE")):
        malformed = tmp_path / f"{size}-byte.xlsx"
        malformed.write_bytes(b"x" * size)
        result = answer_key.import_answer_key(str(malformed), "정답표")
        assert isinstance(result, Err)
        assert result.errors[0].code == code
    monkeypatch.setattr(answer_key, "MAX_PACKAGE_BYTES", len(data))
    assert isinstance(answer_key.import_answer_key(str(path), "정답표"), Ok)
    monkeypatch.setattr(answer_key, "MAX_PACKAGE_BYTES", len(data) - 1)
    too_large = answer_key.import_answer_key(str(path), "정답표")
    assert isinstance(too_large, Err)
    assert too_large.errors[0].code == "XLSX_PACKAGE_QUOTA"

    monkeypatch.setattr(answer_key, "MAX_PACKAGE_BYTES", len(data))
    missing_sheet = answer_key.import_answer_key(str(path), "없음")
    assert isinstance(missing_sheet, Err)
    assert missing_sheet.errors[0].code == "XLSX_SHEET_NOT_FOUND"

    monkeypatch.setattr(answer_key, "MAX_ROWS", 1)
    over_rows = answer_key.import_answer_key(str(path), "정답표")
    assert isinstance(over_rows, Err)
    assert over_rows.errors[0].code == "XLSX_DIMENSION_QUOTA"


@pytest.mark.parametrize("failure", (TypeError, ValueError))
def test_write_answer_key_sample_cleans_temp_after_generation_failure(
    tmp_path, monkeypatch, failure
):
    target = tmp_path / "sample.xlsx"

    def fail_generation(_sheet_name):
        raise failure("invalid sheet name")

    monkeypatch.setattr(answer_key, "answer_key_sample_bytes", fail_generation)

    result = answer_key.write_answer_key_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_write_answer_key_sample_cleans_temp_after_write_failure(tmp_path, monkeypatch):
    target = tmp_path / "sample.xlsx"
    temporary_path = tmp_path / "temporary.xlsx"

    class FailingTemporary:
        def __enter__(self):
            self.stream = temporary_path.open("wb")
            self.name = str(temporary_path)
            return self

        def __exit__(self, *_):
            self.stream.close()

        def write(self, _data):
            raise OSError("write failed")

    monkeypatch.setattr(
        answer_key, "NamedTemporaryFile", lambda *_args, **_kwargs: FailingTemporary()
    )

    result = answer_key.write_answer_key_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_write_answer_key_sample_cleans_temp_after_fsync_failure(tmp_path, monkeypatch):
    target = tmp_path / "sample.xlsx"

    def fail_fsync(_descriptor):
        raise OSError("fsync failed")

    monkeypatch.setattr(answer_key.os, "fsync", fail_fsync)

    result = answer_key.write_answer_key_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_write_answer_key_sample_cleans_temp_after_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "sample.xlsx"

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(answer_key.os, "replace", fail_replace)

    result = answer_key.write_answer_key_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())
