from __future__ import annotations

from pathlib import Path

from omr_grader.application.dto import ScanCommand, ScanSource
from omr_grader.application.scan_use_case import ScanUseCase
from omr_grader.domain.enums import ExamTerm, RosterSnapshotKind
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.models import RosterSnapshot
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
        def load(self, filename: str):
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


def test_scan_artifacts_preserve_original_pdf_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source scan.pdf"
    payload = b"%PDF-1.7 exact source bytes"
    source.write_bytes(payload)
    runtime = ScanRuntime(object(), object())
    roster = RosterSnapshot(
        1,
        RosterSnapshotKind.NONE,
        None,
        None,
        None,
        "v1",
        (),
        (),
    )

    result = runtime._artifacts(
        _command(source),
        "a" * 64,
        roster,
        (),
        "2026-07-31T00:00:00.000000Z",
    )

    assert isinstance(result, Ok)
    artifacts = result.value[0]
    assert artifacts["sources/scans/001_source_scan.pdf"] == payload
