"""Data/FORMAT.json: written for unmarked folders, newer formats open read-only."""

from __future__ import annotations

import json

from omr_grader import __version__
from omr_grader.bootstrap import bootstrap
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.data_format import (
    DATA_FORMAT,
    DataFormat,
    ensure_data_format,
    read_data_format,
)
from omr_grader.infrastructure.paths import ManagedPaths


def test_an_unmarked_folder_gets_the_current_format(tmp_path):
    assert read_data_format(tmp_path) == Ok(None)

    marked = ensure_data_format(tmp_path, "4.2.0")

    assert marked == Ok(DataFormat(DATA_FORMAT, "4.2.0"))
    assert json.loads((tmp_path / "FORMAT.json").read_text(encoding="utf-8")) == {
        "data_format": DATA_FORMAT,
        "written_by": "4.2.0",
    }
    # Later starts leave the marker as it is.
    assert ensure_data_format(tmp_path, "4.3.0") == Ok(DataFormat(DATA_FORMAT, "4.2.0"))


def test_newer_and_damaged_markers_refuse_writing(tmp_path):
    (tmp_path / "FORMAT.json").write_text('{"data_format": 2, "written_by": "5.0.0"}')
    newer = ensure_data_format(tmp_path, "4.2.0")
    assert isinstance(newer, Err) and newer.errors[0].code == "DATA_FORMAT_NEWER"
    assert "5.0.0" in str(newer.errors[0].context["reason"])

    (tmp_path / "FORMAT.json").write_text("{broken")
    damaged = ensure_data_format(tmp_path, "4.2.0")
    assert isinstance(damaged, Err) and damaged.errors[0].code == "DATA_FORMAT_UNREADABLE"


def test_bootstrap_marks_new_folders_and_opens_newer_data_read_only(tmp_path):
    (tmp_path / "fresh").mkdir()
    fresh = bootstrap(ManagedPaths.from_root(tmp_path / "fresh"))
    assert isinstance(fresh, Ok) and fresh.value.write_enabled and fresh.value.first_run
    marker = json.loads((tmp_path / "fresh" / "Data" / "FORMAT.json").read_text("utf-8"))
    assert marker == {"data_format": DATA_FORMAT, "written_by": __version__}
    again = bootstrap(ManagedPaths.from_root(tmp_path / "fresh"))
    assert isinstance(again, Ok) and not again.value.first_run

    (tmp_path / "fresh" / "Data" / "FORMAT.json").write_text(
        '{"data_format": 99, "written_by": "9.9.9"}'
    )
    newer = bootstrap(ManagedPaths.from_root(tmp_path / "fresh"))

    assert isinstance(newer, Ok)
    assert not newer.value.write_enabled
    assert "더 새 버전 OMR Grader 9.9.9" in str(newer.value.diagnostic)
