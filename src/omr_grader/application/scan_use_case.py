"""Main-process scan orchestration; workers only return immutable recognition values."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from threading import Event, Lock
from time import monotonic
from typing import Protocol

from omr_grader.application.dto import (
    CancelOperationCommand,
    ScanCommand,
    ScanProgress,
    SessionCreateResult,
)
from omr_grader.domain.errors import Err, ErrorInfo, Result
from omr_grader.recognition.pipeline import PipelineFailure, PipelineResult
from omr_grader.ui.workers import ScanWorker, WorkerBatchResult, WorkerTask


class ScanTaskSource(Protocol):
    """Main-process ingestion adapter; it opens and closes all source resources itself.

    ``progress(done, total)`` follows each prepared page and may raise to stop early.
    """

    def build_tasks(
        self, command: ScanCommand, progress: Callable[[int, int], None] | None = None
    ) -> Result[tuple[WorkerTask, ...]]: ...


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


@dataclass(frozen=True, slots=True)
class _Operation:
    """A running scan; ``cancelled`` also covers preparation, before the worker runs."""

    worker: ScanWorker
    cancelled: Event = field(default_factory=Event)


class _PreparationCancelled(Exception):
    """Raised from the preparation callback to stop reading the sources."""


class ScanUseCase:
    """Coordinates ingestion, recognition and one final main-process commit."""

    def __init__(
        self, task_source: ScanTaskSource, worker_factory: Callable[[], ScanWorker] = ScanWorker
    ) -> None:
        self._task_source = task_source
        self._worker_factory = worker_factory
        self._operations: dict[str, _Operation] = {}
        self._lock = Lock()

    def run_scan(
        self,
        command: ScanCommand,
        coordinator: SessionCommitCoordinator,
        *,
        progress: ProgressCallback | None = None,
    ) -> Result[SessionCreateResult]:
        """Run a scan and commit exactly once after all accepted worker outputs arrive.

        ``progress`` hears the pages being prepared, then read, then one report before
        the results are saved.
        """
        operation = _Operation(self._worker_factory())
        with self._lock:
            if command.operation_id in self._operations:
                return _error("OPERATION_IN_PROGRESS", "operation_id")
            self._operations[command.operation_id] = operation
        started = monotonic()
        try:
            tasks = self._task_source.build_tasks(
                command, partial(_prepared, operation.cancelled, progress, started)
            )
            if isinstance(tasks, Err):
                return tasks
            if operation.cancelled.is_set():
                return _error("OPERATION_CANCELLED", "operation_id")
            batch = operation.worker.run(
                tasks.value, multiprocessing=command.multiprocessing, progress=progress
            )
            run = ScanRun(command, batch)
            if run.result.cancelled:
                return _error("OPERATION_CANCELLED", "operation_id")
            ordered = tuple(item.result for item in run.result.results)
            if len(ordered) != len(tasks.value):
                return _error("WORKER_RESULT_INCOMPLETE", "source")
            if operation.cancelled.is_set():
                return _error("OPERATION_CANCELLED", "operation_id")
            if progress is not None:
                failed = sum(isinstance(item, PipelineFailure) for item in ordered)
                progress(
                    ScanProgress(
                        len(ordered) - failed,
                        len(ordered),
                        failed,
                        _elapsed_ms(started),
                        None,
                        "save",
                    )
                )
            return coordinator.commit_scan(command, ordered)
        except _PreparationCancelled:
            return _error("OPERATION_CANCELLED", "operation_id")
        except BaseException as error:  # noqa: BLE001  worker failure is reported to the caller
            return _error(f"WORKER_{type(error).__name__.upper()}", "source")
        finally:
            with self._lock:
                self._operations.pop(command.operation_id, None)

    def cancel_scan(self, command: CancelOperationCommand) -> Result[None]:
        with self._lock:
            operation = self._operations.get(command.operation_id)
        if operation is None:
            return _error("OPERATION_NOT_FOUND", "operation_id")
        operation.cancelled.set()
        operation.worker.cancel()
        return _none()


def _prepared(
    cancelled: Event,
    progress: ProgressCallback | None,
    started: float,
    done: int,
    total: int,
) -> None:
    """Report one prepared page, and stop preparing once the scan is cancelled."""
    if cancelled.is_set():
        raise _PreparationCancelled
    if progress is not None:
        progress(ScanProgress(done, total, 0, _elapsed_ms(started), None, "prepare"))


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))


def _none() -> Result[None]:
    from omr_grader.domain.errors import Ok

    return Ok(None)
