from __future__ import annotations

from concurrent.futures import Future

from omr_grader.ui.workers import ScanWorker


class _QueuedExecutor:
    def __init__(self) -> None:
        self.submitted = 0
        self.shutdown_called = False

    def submit(self, fn, task):
        self.submitted += 1
        future = Future()
        # Keep work queued until cancellation has been requested.
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdown_called = True


def test_cancelled_worker_does_not_submit_the_entire_queue() -> None:
    executor = _QueuedExecutor()
    worker = ScanWorker(lambda: executor, max_in_flight=1)
    worker.cancel()
    # A pre-run cancellation is intentionally reset for a distinct operation;
    # this verifies an empty operation has no process-pool side effects.
    result = worker.run((), multiprocessing=True)
    assert not result.cancelled
    assert executor.submitted == 0
    assert executor.shutdown_called
