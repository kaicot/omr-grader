from __future__ import annotations

from pathlib import Path

from omr_grader.application.dto import ScanCommand, ScanSource
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err
from omr_grader.infrastructure.scan_runtime import ScanRuntime


def _command(path: Path, *, roster: Path | None = None) -> ScanCommand:
    return ScanCommand(
        "scan-session",
        "scan-operation",
        0,
        "Math",
        2026,
        ExamTerm.FIRST,
        "profile.omrtemplate",
        None if roster is None else str(roster),
        ScanSource((str(path),)),
        5,
        False,
    )


def test_scan_runtime_rejects_invalid_roster_before_source_processing(tmp_path: Path) -> None:
    roster = tmp_path / "roster.xlsx"
    roster.write_bytes(b"not a workbook")

    class Profiles:
        def load(self, filename: str):
            from omr_grader.domain.errors import Ok

            return Ok(object())

    result = ScanRuntime(Profiles(), object()).build_tasks(
        _command(tmp_path / "scan.png", roster=roster)
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_INVALID_WORKBOOK"


def test_scan_runtime_loads_managed_profile_filename_from_profile_store(tmp_path: Path) -> None:
    loaded: list[str] = []

    class Profiles:
        def load(self, filename: str):
            from omr_grader.domain.errors import Ok

            loaded.append(filename)
            return Ok(object())

        def load_path(self, path: Path):
            raise AssertionError("managed scan profiles must not be reopened as external paths")

    result = ScanRuntime(Profiles(), object()).build_tasks(_command(tmp_path / "missing.pdf"))

    assert isinstance(result, Err)
    assert loaded == ["profile.omrtemplate"]
    assert result.errors[0].code != "PROFILE_SOURCE_NOT_FOUND"


def test_scan_commit_without_a_processable_result_never_calls_store(tmp_path: Path) -> None:
    class Store:
        def _create_initial_generation(self, **kwargs):
            raise AssertionError("unprocessable scan must not be published")

    runtime = ScanRuntime(object(), Store())
    command = _command(tmp_path / "scan.png")
    runtime._prepared[command.operation_id] = (object(), object(), object())

    result = runtime.commit_scan(command, ())

    assert isinstance(result, Err)
    assert result.errors[0].code == "SCAN_NO_PROCESSABLE_RESULT"
    assert result.errors[0].context["failed_pages"] == 0
    assert command.operation_id not in runtime._prepared
