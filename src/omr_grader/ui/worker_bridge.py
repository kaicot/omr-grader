"""Qt boundary for cancellable, value-only background operations."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from threading import Event, Lock
from time import monotonic
from typing import Protocol

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

Operation = Callable[[Event, Callable[[object], None]], object]
CancelHook = Callable[[], Result[None] | None]


class WorkerLifecycle(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    TERMINAL = "terminal"
    CLOSING = "closing"


class _TerminalEmitter(Protocol):
    def emit(self, *args: object) -> object: ...


@dataclass(frozen=True, slots=True)
class WorkerError:
    """Immutable error payload permitted to cross a worker signal boundary."""

    code: str
    message_key: str
    field_path: str | None
    context: tuple[tuple[str, str | int | bool | None], ...]
    retryable: bool
    cause_type: str | None

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or type(self.message_key) is not str
            or (self.field_path is not None and type(self.field_path) is not str)
            or type(self.context) is not tuple
            or type(self.retryable) is not bool
            or (self.cause_type is not None and type(self.cause_type) is not str)
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) not in (str, int, bool, type(None))
                for item in self.context
            )
        ):
            raise ValueError("invalid worker error fields")


def _worker_error(error: object) -> WorkerError:
    if isinstance(error, WorkerError):
        return error
    if isinstance(error, ErrorInfo):
        return WorkerError(
            error.code,
            error.message_key,
            error.field_path,
            tuple(error.context.items()),
            error.retryable,
            error.cause_type,
        )
    return WorkerError(
        "UI_OPERATION_FAILED",
        "error.ui_operation_failed",
        None,
        (("reason", str(error) or type(error).__name__),),
        False,
        type(error).__name__,
    )


def _value_only(value: object) -> bool:
    """Accept only recursively immutable, non-Qt values across the thread boundary."""
    if isinstance(value, QObject):
        return False
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return True
    if isinstance(value, Enum):
        return _value_only(value.value)
    if isinstance(value, tuple | frozenset):
        return all(_value_only(item) for item in value)
    if is_dataclass(value):
        parameters = getattr(value, "__dataclass_params__", None)
        return bool(parameters and parameters.frozen) and all(
            _value_only(getattr(value, field.name)) for field in fields(value)
        )
    return False


class _OperationWorker(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, operation: Operation, cancelled: Event) -> None:
        super().__init__()
        self._operation = operation
        self._cancelled = cancelled
        self._terminal = False

    @Slot()
    def run(self) -> None:
        try:
            value = self._operation(self._cancelled, self._emit_progress)
            if self._cancelled.is_set():
                self._emit_terminal(self.cancelled)
            elif isinstance(value, Err):
                self._emit_terminal(self.failed, _worker_error(value.errors[0]))
            elif isinstance(value, Ok):
                self._emit_success(value.value)
            else:
                self._emit_success(value)
        except BaseException as error:
            self._emit_terminal(self.failed, _worker_error(error))
        finally:
            self.finished.emit()

    def _emit_progress(self, value: object) -> None:
        if self._terminal or self._cancelled.is_set():
            return
        if not _value_only(value):
            self._emit_terminal(
                self.failed,
                _worker_error(ErrorInfo("UI_NON_VALUE_PAYLOAD", "error.ui_non_value_payload")),
            )
            return
        self.progress.emit(value)

    def _emit_success(self, value: object) -> None:
        if self._cancelled.is_set():
            self._emit_terminal(self.cancelled)
        elif not _value_only(value):
            self._emit_terminal(
                self.failed,
                _worker_error(ErrorInfo("UI_NON_VALUE_PAYLOAD", "error.ui_non_value_payload")),
            )
        else:
            self._emit_terminal(self.completed, value)

    def _emit_terminal(self, signal: _TerminalEmitter, value: object | None = None) -> None:
        if self._terminal:
            return
        self._terminal = True
        if value is None:
            signal.emit()
        else:
            signal.emit(value)


class WorkerBridge(QObject):
    """Owns a QThread until it finishes and emits exactly one operation terminal state."""

    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    terminal = Signal()
    lifecycle_changed = Signal(object)
    finished = Signal()
    close_finished = Signal()

    def __init__(self, *, progress_interval_ms: int = 80, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if progress_interval_ms < 0:
            raise ValueError("progress_interval_ms must be nonnegative")
        self._progress_interval = progress_interval_ms / 1000
        self._last_progress = 0.0
        self._pending_progress: object | None = None
        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.timeout.connect(self._flush_pending_progress)
        self._cancelled = Event()
        self._cancel_hook: CancelHook | None = None
        self._thread: QThread | None = None
        self._worker: _OperationWorker | None = None
        self._retired_threads: deque[QThread] = deque(maxlen=64)
        self._retired_workers: deque[_OperationWorker] = deque(maxlen=64)
        self._lock = Lock()
        self._terminal_emitted = False
        self._cancel_hook_called = False
        self._close_requested = False
        self._lifecycle = WorkerLifecycle.IDLE

    @property
    def active(self) -> bool:
        return self._thread is not None or self._lifecycle is WorkerLifecycle.CLOSING

    @property
    def lifecycle(self) -> WorkerLifecycle:
        return self._lifecycle

    def start(self, operation: Operation, *, cancel_hook: CancelHook | None = None) -> None:
        if self._thread is not None or self._lifecycle is WorkerLifecycle.CLOSING:
            raise RuntimeError("the previous operation has not finished")
        self._cancelled.clear()
        self._cancel_hook = cancel_hook
        self._terminal_emitted = False
        self._cancel_hook_called = False
        self._close_requested = False
        self._last_progress = 0.0
        self._pending_progress = None
        thread = QThread(self)
        worker = _OperationWorker(operation, self._cancelled)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_thread)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        self._set_lifecycle(WorkerLifecycle.RUNNING)
        thread.start()

    def cancel(self) -> None:
        if self._lifecycle not in (
            WorkerLifecycle.RUNNING,
            WorkerLifecycle.CANCELLING,
            WorkerLifecycle.CLOSING,
        ):
            return
        self._cancelled.set()
        if self._lifecycle is WorkerLifecycle.RUNNING:
            self._set_lifecycle(WorkerLifecycle.CANCELLING)
        if self._cancel_hook_called:
            return
        self._cancel_hook_called = True
        hook = self._cancel_hook
        if hook is not None:
            try:
                result = hook()
            except BaseException as error:
                self._emit_terminal(self.failed, _worker_error(error))
                return
            if isinstance(result, Err):
                self._emit_terminal(self.failed, _worker_error(result.errors[0]))

    def close(self) -> None:
        """Request cancellation and report completion through ``close_finished``."""
        if self._lifecycle is WorkerLifecycle.IDLE:
            self.close_finished.emit()
            return
        if self._thread is None:
            self._set_lifecycle(WorkerLifecycle.CLOSING)
            self._set_lifecycle(WorkerLifecycle.TERMINAL)
            self.close_finished.emit()
            return
        self._close_requested = True
        self._set_lifecycle(WorkerLifecycle.CLOSING)
        self.cancel()

    @Slot(object)
    def _on_progress(self, value: object) -> None:
        if self._terminal_emitted or self._cancelled.is_set():
            return
        now = monotonic()
        if now - self._last_progress < self._progress_interval:
            self._pending_progress = value
            remaining_ms = max(
                1,
                round(
                    (self._progress_interval - (now - self._last_progress))
                    * 1000
                ),
            )
            if not self._progress_timer.isActive():
                self._progress_timer.start(remaining_ms)
            return
        self._progress_timer.stop()
        self._pending_progress = None
        self._last_progress = now
        self.progress.emit(value)

    @Slot()
    def _flush_pending_progress(self) -> None:
        value = self._pending_progress
        self._pending_progress = None
        if value is None or self._terminal_emitted or self._cancelled.is_set():
            return
        self._last_progress = monotonic()
        self.progress.emit(value)

    @Slot(object)
    def _on_success(self, value: object) -> None:
        if self._cancelled.is_set():
            return
        self._emit_terminal(self.succeeded, value)

    @Slot(object)
    def _on_failure(self, error: object) -> None:
        if self._cancelled.is_set():
            return
        self._emit_terminal(self.failed, _worker_error(error))

    @Slot()
    def _on_cancelled(self) -> None:
        # A cancellation request remains active until the worker exits.
        return

    @Slot()
    def _clear_thread(self) -> None:
        try:
            sender = self.sender()
        except RuntimeError:
            # Qt may delete the source thread while processing its teardown signal.
            return
        if sender is not self._thread:
            return
        self._pending_progress = None
        if self._thread is not None:
            self._retired_threads.append(self._thread)
        if self._worker is not None:
            self._retired_workers.append(self._worker)
        self._thread = None
        self._worker = None
        self._cancel_hook = None
        if self._cancelled.is_set():
            self._emit_terminal(self.cancelled)
        try:
            self._set_lifecycle(WorkerLifecycle.TERMINAL)
            self.finished.emit()
            if self._close_requested:
                self.close_finished.emit()
        except RuntimeError:
            # The bridge can be deleted with its parent while Qt delivers teardown signals.
            return

    def _emit_terminal(self, signal: _TerminalEmitter, value: object | None = None) -> None:
        try:
            self._progress_timer.stop()
        except RuntimeError:
            # The bridge may outlive its native QObject briefly during Qt teardown.
            pass
        self._flush_pending_progress()
        with self._lock:
            if self._terminal_emitted:
                return
            self._terminal_emitted = True
        self._pending_progress = None
        if value is None:
            signal.emit()
        else:
            signal.emit(value)
        self.terminal.emit()
        if self._lifecycle is not WorkerLifecycle.CLOSING:
            self._set_lifecycle(WorkerLifecycle.TERMINAL)

    def _set_lifecycle(self, lifecycle: WorkerLifecycle) -> None:
        if self._lifecycle is lifecycle:
            return
        self._lifecycle = lifecycle
        self.lifecycle_changed.emit(lifecycle)


__all__ = ["CancelHook", "Operation", "WorkerBridge", "WorkerError", "WorkerLifecycle"]
