from __future__ import annotations

from concurrent.futures.process import BrokenProcessPool

import pytest

from omr_grader.ui import workers
from omr_grader.ui.workers import ScanWorker, WorkerResult, WorkerTask


class _BrokenExecutor:
    def submit(self, *_: object) -> object:
        raise BrokenProcessPool("packaged worker failed to start")

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


def _task(ordinal: int) -> WorkerTask:
    task = object.__new__(WorkerTask)
    object.__setattr__(task, "ordinal", ordinal)
    object.__setattr__(task, "pipeline_input", None)
    return task


def test_broken_process_pool_retries_every_task_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed: list[int] = []

    def run(task: WorkerTask) -> WorkerResult:
        completed.append(task.ordinal)
        return WorkerResult(task.ordinal, object())  # type: ignore[arg-type]

    monkeypatch.setattr(workers, "run_pipeline_task", run)
    progress = []

    result = ScanWorker(lambda: _BrokenExecutor()).run(
        (_task(0), _task(1)),
        multiprocessing=True,
        progress=progress.append,
    )

    assert completed == [0, 1]
    assert tuple(item.ordinal for item in result.results) == (0, 1)
    assert not result.cancelled
    assert (progress[0].completed, progress[0].total) == (0, 2)
    assert (progress[-1].completed, progress[-1].total) == (2, 2)


def test_sequential_worker_emits_zero_of_total_before_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workers,
        "run_pipeline_task",
        lambda task: WorkerResult(task.ordinal, object()),  # type: ignore[arg-type]
    )
    progress = []

    ScanWorker().run((_task(0),), multiprocessing=False, progress=progress.append)

    assert (progress[0].completed, progress[0].total, progress[0].failed) == (0, 1, 0)
    assert (progress[-1].completed, progress[-1].total, progress[-1].failed) == (1, 1, 0)
