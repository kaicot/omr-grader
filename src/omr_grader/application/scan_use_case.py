"""Main-process scan orchestration; workers only return immutable recognition values."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from omr_grader.application.dto import (
    CancelOperationCommand,
    ScanCommand,
    ScanProgress,
    SessionCreateResult,
)
from omr_grader.domain.errors import Err, ErrorInfo, Result
from omr_grader.recognition.pipeline import PipelineResult
from omr_grader.ui.workers import ScanWorker, WorkerBatchResult, WorkerTask


class ScanTaskSource(Protocol):
    """Main-process ingestion adapter; it opens and closes all source resources itself."""

    def build_tasks(self, command: ScanCommand) -> Result[tuple[WorkerTask, ...]]: ...


class SessionCommitCoordinator(Protocol):
    """The only authority allowed to publish a scan generation and its artifacts."""

    def commit_scan(
        self, command: ScanCommand, results: tuple[PipelineResult, ...]
    ) -> Result[SessionCreateResult]: ...


ProgressCallback = Callable[[ScanProgress], None]


@dataclass(frozen=True, slots=True)
class ScanRun:
    command: ScanCommand
    result: WorkerBatchResult


class ScanUseCase:
    """Coordinates ingestion, recognition and one final main-process commit."""

    def __init__(
        self, task_source: ScanTaskSource, worker_factory: Callable[[], ScanWorker] = ScanWorker
    ) -> None:
        self._task_source = task_source
        self._worker_factory = worker_factory
        self._operations: dict[str, ScanWorker] = {}
        self._lock = Lock()

    def run_scan(
        self,
        command: ScanCommand,
        coordinator: SessionCommitCoordinator,
        *,
        progress: ProgressCallback | None = None,
    ) -> Result[SessionCreateResult]:
        """Run a scan and commit exactly once after all accepted worker outputs arrive."""
        tasks = self._task_source.build_tasks(command)
        if isinstance(tasks, Err):
            return tasks
        worker = self._worker_factory()
        with self._lock:
            if command.operation_id in self._operations:
                return _error("OPERATION_IN_PROGRESS", "operation_id")
            self._operations[command.operation_id] = worker
        try:
            batch = worker.run(
                tasks.value, multiprocessing=command.multiprocessing, progress=progress
            )
            run = ScanRun(command, batch)
            if run.result.cancelled:
                return _error("OPERATION_CANCELLED", "operation_id")
            ordered = tuple(item.result for item in run.result.results)
            if len(ordered) != len(tasks.value):
                return _error("WORKER_RESULT_INCOMPLETE", "source")
            return coordinator.commit_scan(command, ordered)
        except BaseException as error:
            return _error(f"WORKER_{type(error).__name__.upper()}", "source")
        finally:
            with self._lock:
                self._operations.pop(command.operation_id, None)

    def cancel_scan(self, command: CancelOperationCommand) -> Result[None]:
        with self._lock:
            worker = self._operations.get(command.operation_id)
        if worker is None:
            return _error("OPERATION_NOT_FOUND", "operation_id")
        worker.cancel()
        return _none()


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))


def _none() -> Result[None]:
    from omr_grader.domain.errors import Ok

    return Ok(None)
