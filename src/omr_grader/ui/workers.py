"""Non-Qt scan worker coordination with value-only process-pool messages."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from typing import Protocol

from omr_grader.application.dto import ScanProgress
from omr_grader.domain.errors import ErrorInfo
from omr_grader.recognition.pipeline import (
    PipelineFailure,
    PipelineInput,
    PipelineResult,
    recognize_page,
)


@dataclass(frozen=True, slots=True)
class WorkerTask:
    ordinal: int
    pipeline_input: PipelineInput

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("ordinal must be nonnegative")
        if not isinstance(self.pipeline_input, PipelineInput):
            raise TypeError("worker tasks accept only PipelineInput values")


@dataclass(frozen=True, slots=True)
class WorkerResult:
    ordinal: int
    result: PipelineResult


@dataclass(frozen=True, slots=True)
class WorkerBatchResult:
    results: tuple[WorkerResult, ...]
    cancelled: bool


class ExecutorLike(Protocol):
    def submit(
        self, fn: Callable[[WorkerTask], WorkerResult], task: WorkerTask
    ) -> Future[WorkerResult]: ...

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None: ...


ExecutorFactory = Callable[[], ExecutorLike]
ProgressCallback = Callable[[ScanProgress], None]


def run_pipeline_task(task: WorkerTask) -> WorkerResult:
    """Top-level pickle-safe process-pool task. It owns no session or UI resource."""
    return WorkerResult(task.ordinal, recognize_page(task.pipeline_input))


class ScanWorker:
    """Main-process coordinator for sequential or process-pool recognition."""

    def __init__(
        self, executor_factory: ExecutorFactory | None = None, *, max_in_flight: int = 4
    ) -> None:
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise ValueError("max_in_flight must be a positive integer")
        self._executor_factory = executor_factory or ProcessPoolExecutor
        self._max_in_flight = max_in_flight
        self._cancelled = Event()
        self._lock = Lock()

    def cancel(self) -> None:
        """Request cancellation; the polling loop observes it within 50 ms."""
        self._cancelled.set()

    def run(
        self,
        tasks: tuple[WorkerTask, ...],
        *,
        multiprocessing: bool,
        progress: ProgressCallback | None = None,
    ) -> WorkerBatchResult:
        with self._lock:
            self._cancelled.clear()
        started = monotonic()
        if not multiprocessing:
            return self._sequential(tasks, started, progress)
        return self._parallel(tasks, started, progress)

    def _sequential(
        self, tasks: tuple[WorkerTask, ...], started: float, progress: ProgressCallback | None
    ) -> WorkerBatchResult:
        results: list[WorkerResult] = []
        for task in tasks:
            if self._cancelled.is_set():
                return WorkerBatchResult(tuple(results), True)
            result = run_pipeline_task(task)
            if self._cancelled.is_set():
                return WorkerBatchResult(tuple(results), True)
            results.append(result)
            self._progress(progress, len(results), len(tasks), results, started)
        return WorkerBatchResult(tuple(results), False)

    def _parallel(
        self, tasks: tuple[WorkerTask, ...], started: float, progress: ProgressCallback | None
    ) -> WorkerBatchResult:
        executor = self._executor_factory()
        pending: dict[Future[WorkerResult], WorkerTask] = {}
        results: list[WorkerResult] = []
        submitted = 0
        cancelled = False
        try:
            while (
                submitted < len(tasks)
                and len(pending) < self._max_in_flight
                and not self._cancelled.is_set()
            ):
                task = tasks[submitted]
                pending[executor.submit(run_pipeline_task, task)] = task
                submitted += 1
            while pending:
                done, _ = wait(tuple(pending), timeout=0.05, return_when=FIRST_COMPLETED)
                if self._cancelled.is_set():
                    cancelled = True
                    for future in pending:
                        future.cancel()
                    return WorkerBatchResult(tuple(results), True)
                for future in done:
                    task = pending.pop(future)
                    try:
                        result = future.result()
                    except BrokenProcessPool:
                        result = _broken_pool(task)
                    except BaseException as error:
                        result = _task_failure(task, "WORKER_TASK_FAILED", type(error).__name__)
                    if self._cancelled.is_set():
                        cancelled = True
                        for pending_future in pending:
                            pending_future.cancel()
                        return WorkerBatchResult(tuple(results), True)
                    results.append(result)
                    self._progress(progress, len(results), len(tasks), results, started)
                while (
                    submitted < len(tasks)
                    and len(pending) < self._max_in_flight
                    and not self._cancelled.is_set()
                ):
                    task = tasks[submitted]
                    pending[executor.submit(run_pipeline_task, task)] = task
                    submitted += 1
            return WorkerBatchResult(
                tuple(sorted(results, key=lambda item: item.ordinal)), cancelled
            )
        except BrokenProcessPool:
            results.extend(_broken_pool(task) for task in pending.values())
            results.extend(_broken_pool(task) for task in tasks[submitted:])
            return WorkerBatchResult(tuple(sorted(results, key=lambda item: item.ordinal)), False)
        finally:
            try:
                executor.shutdown(wait=not cancelled, cancel_futures=cancelled)
            except TypeError:
                executor.shutdown(wait=not cancelled)

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        completed: int,
        total: int,
        results: list[WorkerResult],
        started: float,
    ) -> None:
        if callback is None:
            return
        failures = sum(isinstance(item.result, PipelineFailure) for item in results)
        elapsed_ms = int((monotonic() - started) * 1000)
        remaining = total - completed
        eta_ms = int(elapsed_ms * remaining / completed) if completed else None
        callback(ScanProgress(completed - failures, total, failures, elapsed_ms, eta_ms))


def _broken_pool(task: WorkerTask) -> WorkerResult:
    return _task_failure(task, "WORKER_POOL_BROKEN", "BrokenProcessPool")


def _task_failure(task: WorkerTask, code: str, cause: str) -> WorkerResult:
    error = ErrorInfo(
        code, f"error.{code.lower()}", context={"manual_review": True}, cause_type=cause
    )
    from omr_grader.recognition.pipeline import _failure

    return WorkerResult(task.ordinal, _failure(task.pipeline_input.page_ref, error))
