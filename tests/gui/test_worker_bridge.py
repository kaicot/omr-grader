from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from time import sleep
from types import MappingProxyType

from PySide6.QtCore import QObject

from omr_grader.application.dto import SessionCreateResult
from omr_grader.domain.enums import IndexState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.worker_bridge import WorkerBridge, WorkerError, WorkerLifecycle


def test_success_emits_one_terminal(qtbot):
    bridge = WorkerBridge(progress_interval_ms=0)
    seen: list[tuple[str, object | None]] = []
    bridge.progress.connect(lambda value: seen.append(("progress", value)))
    bridge.succeeded.connect(lambda value: seen.append(("success", value)))
    bridge.terminal.connect(lambda: seen.append(("terminal", None)))
    finished: list[None] = []
    bridge.finished.connect(lambda: finished.append(None))

    bridge.start(lambda _cancel, progress: (progress("진행"), "완료")[1])
    qtbot.waitUntil(lambda: bool(finished))

    assert seen.count(("success", "완료")) == 1
    assert sum(kind == "terminal" for kind, _ in seen) == 1
    assert bridge.lifecycle is WorkerLifecycle.TERMINAL
    assert not bridge.active


def test_throttled_progress_flushes_latest_value_while_worker_is_running(qtbot):
    bridge = WorkerBridge(progress_interval_ms=20)
    release = Event()
    progress: list[str] = []
    bridge.progress.connect(progress.append)

    def operation(_cancel, emit_progress):
        emit_progress("첫 상태")
        emit_progress("최신 상태")
        release.wait()
        return "완료"

    bridge.start(operation)
    try:
        qtbot.waitUntil(lambda: progress == ["첫 상태", "최신 상태"], timeout=1000)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not bridge.active)


def test_error_and_late_progress_are_terminal_once(qtbot):
    bridge = WorkerBridge(progress_interval_ms=0)
    errors: list[WorkerError] = []
    successes: list[object] = []
    terminals: list[None] = []
    bridge.succeeded.connect(successes.append)
    bridge.failed.connect(errors.append)
    bridge.terminal.connect(lambda: terminals.append(None))

    def fail(_cancel, progress):
        progress("before")
        raise RuntimeError("boom")

    bridge.start(fail)
    qtbot.waitUntil(lambda: bool(terminals))
    bridge._on_progress("late progress")
    bridge._on_success("late result")

    assert not successes
    assert len(errors) == len(terminals) == 1
    assert errors[0].code == "UI_OPERATION_FAILED"


def test_worker_base_exception_emits_one_typed_terminal_failure(qtbot):
    bridge = WorkerBridge()
    errors: list[WorkerError] = []
    terminals: list[None] = []
    bridge.failed.connect(errors.append)
    bridge.terminal.connect(lambda: terminals.append(None))

    def fail_with_base_exception(_cancel, _progress):
        raise KeyboardInterrupt("interrupted")

    bridge.start(fail_with_base_exception)
    qtbot.waitUntil(lambda: bool(terminals))

    assert len(errors) == len(terminals) == 1
    assert errors[0].code == "UI_OPERATION_FAILED"
    assert errors[0].cause_type == "KeyboardInterrupt"


def test_cancel_hook_base_exception_emits_one_typed_terminal_failure(qtbot):
    bridge = WorkerBridge()
    errors: list[WorkerError] = []
    terminals: list[None] = []
    cancellations: list[None] = []
    bridge.failed.connect(errors.append)
    bridge.terminal.connect(lambda: terminals.append(None))
    bridge.cancelled.connect(lambda: cancellations.append(None))

    release = Event()

    def wait_for_cancel(cancel, _progress):
        while not cancel.is_set():
            sleep(0.005)
        release.wait()
        return "late result"

    def failing_cancel_hook():
        raise KeyboardInterrupt("cancel interrupted")

    bridge.start(wait_for_cancel, cancel_hook=failing_cancel_hook)
    qtbot.waitUntil(lambda: bridge.active)
    bridge.cancel()
    bridge.cancel()
    qtbot.waitUntil(lambda: bool(errors))

    assert bridge.active
    assert len(errors) == len(terminals) == 1
    assert not cancellations
    assert errors[0].code == "UI_OPERATION_FAILED"
    assert errors[0].cause_type == "KeyboardInterrupt"

    release.set()
    qtbot.waitUntil(lambda: not bridge.active)


def test_cancel_and_close_own_the_worker_lifetime(qtbot):
    bridge = WorkerBridge()
    cancelled: list[None] = []
    bridge.cancelled.connect(lambda: cancelled.append(None))

    def wait_for_cancel(cancel, _progress):
        while not cancel.is_set():
            sleep(0.005)
        return "late result"

    bridge.start(wait_for_cancel)
    qtbot.waitUntil(lambda: bridge.active)
    bridge.close()
    qtbot.waitUntil(lambda: not bridge.active)

    assert len(cancelled) == 1
    assert not bridge.active


def test_cancel_rejects_late_result_and_progress(qtbot):
    bridge = WorkerBridge(progress_interval_ms=0)
    cancelled: list[None] = []
    succeeded: list[object] = []
    progress: list[object] = []
    release = Event()
    bridge.cancelled.connect(lambda: cancelled.append(None))
    bridge.succeeded.connect(succeeded.append)
    bridge.progress.connect(progress.append)

    def wait_for_cancel(cancel, emit_progress):
        while not cancel.is_set():
            sleep(0.005)
        emit_progress("late progress")
        release.wait()
        return "late result"

    bridge.start(wait_for_cancel, cancel_hook=lambda: None)
    qtbot.waitUntil(lambda: bridge.lifecycle is WorkerLifecycle.RUNNING)
    bridge.cancel()

    assert bridge.active
    assert not cancelled
    release.set()
    qtbot.waitUntil(lambda: bool(cancelled))
    qtbot.waitUntil(lambda: bridge.lifecycle is WorkerLifecycle.TERMINAL)
    qtbot.waitUntil(lambda: not bridge.active)

    assert not succeeded
    assert not progress
    assert len(cancelled) == 1


def test_cancel_hook_err_emits_failure_once_and_keeps_worker_active(qtbot):
    bridge = WorkerBridge()
    errors: list[WorkerError] = []
    terminals: list[None] = []
    cancellations: list[None] = []
    release = Event()
    hook_calls = 0
    bridge.failed.connect(errors.append)
    bridge.terminal.connect(lambda: terminals.append(None))
    bridge.cancelled.connect(lambda: cancellations.append(None))

    def wait_for_cancel(cancel, _progress):
        while not cancel.is_set():
            sleep(0.005)
        release.wait()
        return "late result"

    def reject_cancel():
        nonlocal hook_calls
        hook_calls += 1
        return Err((ErrorInfo("CANCEL_REJECTED", "error.cancel_rejected"),))

    bridge.start(wait_for_cancel, cancel_hook=reject_cancel)
    qtbot.waitUntil(lambda: bridge.active)
    bridge.cancel()
    bridge.cancel()
    qtbot.waitUntil(lambda: bool(errors))

    assert bridge.active
    assert hook_calls == 1
    assert len(errors) == len(terminals) == 1
    assert not cancellations
    assert errors[0].code == "CANCEL_REJECTED"

    release.set()
    qtbot.waitUntil(lambda: not bridge.active)


def test_cancel_hook_ok_acknowledges_until_worker_exit(qtbot):
    bridge = WorkerBridge()
    cancelled: list[None] = []
    release = Event()
    hook_calls = 0
    bridge.cancelled.connect(lambda: cancelled.append(None))

    def wait_for_cancel(cancel, _progress):
        while not cancel.is_set():
            sleep(0.005)
        release.wait()
        return "late result"

    def acknowledge_cancel():
        nonlocal hook_calls
        hook_calls += 1
        return Ok(None)

    bridge.start(wait_for_cancel, cancel_hook=acknowledge_cancel)
    qtbot.waitUntil(lambda: bridge.active)
    bridge.cancel()
    bridge.cancel()

    assert bridge.active
    assert hook_calls == 1
    assert not cancelled

    release.set()
    qtbot.waitUntil(lambda: bool(cancelled))
    qtbot.waitUntil(lambda: not bridge.active)

    assert len(cancelled) == 1


def test_close_is_asynchronous_and_retains_thread_until_finished(qtbot):
    bridge = WorkerBridge()
    close_finished: list[None] = []
    release = Event()
    bridge.close_finished.connect(lambda: close_finished.append(None))

    def slow_cancel(cancel, _progress):
        while not cancel.is_set():
            sleep(0.005)
        release.wait()
        return "late result"

    bridge.start(slow_cancel)
    qtbot.waitUntil(lambda: bridge.active)
    bridge.close()

    assert bridge.lifecycle is WorkerLifecycle.CLOSING
    assert bridge.active
    assert not close_finished

    release.set()
    qtbot.waitUntil(lambda: bool(close_finished))

    assert bridge.lifecycle is WorkerLifecycle.TERMINAL
    assert not bridge.active


def test_restart_rejected_until_finished_then_allowed(qtbot):
    bridge = WorkerBridge()
    terminals: list[None] = []
    completions: list[str] = []
    progress: list[str] = []
    finished: list[None] = []
    release = Event()
    bridge.terminal.connect(lambda: terminals.append(None))
    bridge.succeeded.connect(completions.append)
    bridge.progress.connect(progress.append)
    bridge.finished.connect(lambda: finished.append(None))

    def first_operation(_cancel, emit_progress):
        emit_progress("first progress")
        release.wait()
        return "first"

    bridge.start(first_operation)
    qtbot.waitUntil(lambda: bridge.lifecycle is WorkerLifecycle.RUNNING)
    try:
        bridge.start(lambda _cancel, _progress: "racing restart")
    except RuntimeError:
        pass
    else:
        raise AssertionError("restart must wait for QThread completion")

    release.set()
    qtbot.waitUntil(lambda: bool(terminals))
    qtbot.waitUntil(lambda: bool(finished))
    assert bridge.lifecycle is WorkerLifecycle.TERMINAL
    assert not bridge.active
    second_release = Event()

    bridge.start(
        lambda _cancel, emit_progress: (
            second_release.wait(),
            emit_progress("second progress"),
            "second",
        )[2]
    )
    qtbot.waitUntil(lambda: bridge.lifecycle is WorkerLifecycle.RUNNING)
    second_release.set()
    qtbot.waitUntil(lambda: completions == ["first", "second"])
    qtbot.waitUntil(lambda: len(finished) == 2)

    assert bridge.lifecycle is WorkerLifecycle.TERMINAL
    assert progress == ["first progress", "second progress"]
    assert len(terminals) == 2


@dataclass
class _MutablePayload:
    value: str


@dataclass(frozen=True)
class _FrozenPayload:
    value: str


def test_mutable_and_qobject_payloads_are_rejected(qtbot):
    for payload in ({}, [], _MutablePayload("변경 가능"), QObject()):
        bridge = WorkerBridge()
        errors: list[WorkerError] = []
        bridge.failed.connect(errors.append)

        bridge.start(lambda _cancel, _progress, payload=payload: payload)
        qtbot.waitUntil(lambda errors=errors: bool(errors))

        assert errors[0].code == "UI_NON_VALUE_PAYLOAD"


def test_mapping_proxy_payload_is_rejected_after_backing_dict_mutation(qtbot):
    backing = {"state": "before"}
    payload = MappingProxyType(backing)
    bridge = WorkerBridge()
    errors: list[WorkerError] = []
    release = Event()
    bridge.failed.connect(errors.append)

    def return_payload(_cancel, _progress):
        release.wait()
        return payload

    bridge.start(return_payload)
    qtbot.waitUntil(lambda: bridge.active)
    backing["state"] = "after"
    release.set()
    qtbot.waitUntil(lambda: bool(errors))

    assert payload["state"] == "after"
    assert errors[0].code == "UI_NON_VALUE_PAYLOAD"


def test_frozen_payload_is_accepted(qtbot):
    bridge = WorkerBridge()
    values: list[_FrozenPayload] = []
    bridge.succeeded.connect(values.append)

    bridge.start(lambda _cancel, _progress: _FrozenPayload("고정"))
    qtbot.waitUntil(lambda: bool(values))

    assert values == [_FrozenPayload("고정")]


def test_production_result_with_enum_is_accepted(qtbot):
    bridge = WorkerBridge()
    values: list[SessionCreateResult] = []
    result = SessionCreateResult(
        True,
        "session",
        1,
        "generation",
        IndexState.CURRENT,
        "operation",
    )
    bridge.succeeded.connect(values.append)

    bridge.start(lambda _cancel, _progress: result)
    qtbot.waitUntil(lambda: bool(values))

    assert values == [result]


def test_failure_payload_is_a_frozen_value_only_dto(qtbot):
    bridge = WorkerBridge()
    errors: list[WorkerError] = []
    source = ErrorInfo(
        "UI_OPERATION_FAILED",
        "error.ui_operation_failed",
        context={"reason": "immutable"},
    )
    bridge.failed.connect(errors.append)

    bridge.start(lambda _cancel, _progress: Err((source,)))
    qtbot.waitUntil(lambda: bool(errors))

    assert errors == [
        WorkerError(
            "UI_OPERATION_FAILED",
            "error.ui_operation_failed",
            None,
            (("reason", "immutable"),),
            False,
            None,
        )
    ]
    assert isinstance(errors[0].context, tuple)
    assert WorkerError.__dataclass_params__.frozen


def test_retired_wrapper_retention_is_bounded_across_restarts(qtbot):
    bridge = WorkerBridge()
    finished: list[None] = []
    release = Event()
    bridge.finished.connect(lambda: finished.append(None))

    bridge.start(lambda _cancel, _progress: "first")
    qtbot.waitUntil(lambda: len(finished) == 1)
    first_thread = bridge._retired_threads[-1]
    first_worker = bridge._retired_workers[-1]
    assert bridge._retired_threads.maxlen == 64
    assert bridge._retired_workers.maxlen == 64

    bridge.start(lambda _cancel, _progress: (release.wait(), "second")[1])
    qtbot.waitUntil(lambda: bridge.lifecycle is WorkerLifecycle.RUNNING)

    assert tuple(bridge._retired_threads) == (first_thread,)
    assert tuple(bridge._retired_workers) == (first_worker,)

    release.set()
    qtbot.waitUntil(lambda: len(finished) == 2)

    assert len(bridge._retired_threads) == 2
    assert len(bridge._retired_workers) == 2
    assert bridge._retired_threads[-1] is not first_thread
    assert bridge._retired_workers[-1] is not first_worker


def test_controller_bounds_completed_bridge_retention(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(),
        write_enabled=True,
    )

    controller._start(window.scan_page, "completed", lambda _cancel, _progress: "done")

    qtbot.waitUntil(lambda: controller._active_bridge is None)
    first = controller._retired_bridges[-1]
    assert isinstance(first, WorkerBridge)
    assert len(controller._retired_bridges) == 1

    controller._start(window.scan_page, "second", lambda _cancel, _progress: "done")
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert len(controller._retired_bridges) == 2
    assert controller._retired_bridges[-1] is not first
