from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.ingestion.roster import ROSTER_HEADERS, import_roster
from omr_grader.workbooks import roster_sample


def test_roster_sample_is_deterministic_formula_free_and_strictly_valid(tmp_path):
    data = roster_sample.roster_sample_bytes()

    assert data == roster_sample.roster_sample_bytes()
    with ZipFile(BytesIO(data)) as archive:
        assert all(
            b"<f" not in archive.read(member).lower()
            for member in archive.infolist()
            if member.filename.lower().startswith("xl/worksheets/")
        )

    path = tmp_path / "roster.xlsx"
    path.write_bytes(data)
    imported = import_roster(str(path), roster_sample.SAMPLE_SHEET_NAME)

    assert isinstance(imported, Ok)
    assert [entry.student_id for entry in imported.value.rows] == [
        "20260001",
        "02026002",
        "20260003",
    ]
    assert [entry.name for entry in imported.value.rows] == ["김하늘", "이봄", "박여름"]
    assert all(not entry.issues for entry in imported.value.rows)


def test_roster_sample_uses_frozen_headers():
    assert ROSTER_HEADERS == ("연번", "학번", "이름")


def test_write_roster_sample_writes_the_deterministic_package(tmp_path):
    target = tmp_path / "sample.xlsx"

    result = roster_sample.write_roster_sample(str(target))

    assert isinstance(result, Ok)
    assert target.read_bytes() == roster_sample.roster_sample_bytes()
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("failure", (TypeError, ValueError, RuntimeError))
def test_write_roster_sample_cleans_temp_after_generation_failure(tmp_path, monkeypatch, failure):
    target = tmp_path / "sample.xlsx"

    def fail_generation(_sheet_name):
        raise failure("generation failed")

    monkeypatch.setattr(roster_sample, "roster_sample_bytes", fail_generation)

    result = roster_sample.write_roster_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_write_roster_sample_cleans_temp_after_write_failure(tmp_path, monkeypatch):
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
        roster_sample, "NamedTemporaryFile", lambda *_args, **_kwargs: FailingTemporary()
    )

    result = roster_sample.write_roster_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_write_roster_sample_cleans_temp_after_fsync_failure(tmp_path, monkeypatch):
    target = tmp_path / "sample.xlsx"

    def fail_fsync(_descriptor):
        raise OSError("fsync failed")

    monkeypatch.setattr(roster_sample.os, "fsync", fail_fsync)

    result = roster_sample.write_roster_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_write_roster_sample_cleans_temp_after_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "sample.xlsx"
    target.write_bytes(b"existing workbook")

    monkeypatch.setattr(
        roster_sample.os,
        "replace",
        lambda _source, _destination: (_ for _ in ()).throw(OSError()),
    )

    result = roster_sample.write_roster_sample(str(target))

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_WRITE_FAILED"
    assert target.read_bytes() == b"existing workbook"
    assert list(tmp_path.iterdir()) == [target]
