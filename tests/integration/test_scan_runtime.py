from __future__ import annotations

from pathlib import Path

from omr_grader.application.dto import ScanCommand, ScanSource
from omr_grader.application.scan_use_case import ScanUseCase
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.scan_runtime import ScanRuntime
from omr_grader.ui.workers import WorkerBatchResult


def _command(source: Path, *, profile: str = "profile.omrtemplate") -> ScanCommand:
    return ScanCommand(
        "scan-session",
        "scan-operation",
        0,
        "Math",
        2026,
        ExamTerm.FIRST,
        profile,
        None,
        ScanSource((str(source),)),
        5,
        False,
    )


def test_scan_runtime_rejects_profile_source_with_wrong_extension_before_ingestion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(b"not inspected")

    class Profiles:
        def load_path(self, path: Path):
            return Ok(object())

    runtime = ScanRuntime(Profiles(), object())
    result = runtime.build_tasks(_command(source, profile="profile.json"))

    assert isinstance(result, Err)
    assert result.errors[0].code == "PROFILE_SOURCE_INVALID"


def test_scan_use_case_commits_only_complete_uncancelled_worker_results(tmp_path: Path) -> None:
    command = _command(tmp_path / "unused.png")
    tasks = (object(), object())

    class Source:
        def build_tasks(self, received):
            assert received is command
            return Ok(tasks)

    class Worker:
        def run(self, received, *, multiprocessing, progress):
            assert received == tasks
            return WorkerBatchResult((), False)

    class Coordinator:
        def commit_scan(self, command, results):
            raise AssertionError("incomplete worker output must not publish")

    result = ScanUseCase(Source(), Worker).run_scan(command, Coordinator())

    assert isinstance(result, Err)
    assert result.errors[0].code == "WORKER_RESULT_INCOMPLETE"
