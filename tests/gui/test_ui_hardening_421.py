"""4.2.1 UI hardening: busy refusals, status texts, cancel button, empty dashboard, trash."""

from __future__ import annotations

import logging
from threading import Event
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QFileDialog

from omr_grader.application.dto import (
    CancelOperationCommand,
    PermanentDeleteResult,
    Settings,
    SoftDeleteResult,
    TrashRestoreResult,
)
from omr_grader.application.grading_presenter import ConnectedSessionDisplay
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.enums import CleanupState, ExamTerm, IndexState, SessionState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.infrastructure.dashboard_repository import DashboardListing
from omr_grader.infrastructure.data_import import ImportSummary
from omr_grader.ui.app_controller import (
    _BUSY_TEXT,
    AppController,
    ServicePorts,
)
from omr_grader.ui.dashboard_model import DashboardSelection
from omr_grader.ui.dashboard_page import DashboardGlobalRequest, DashboardRequest
from omr_grader.ui.grading_page import GradingProgressDisplay
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPageRequest, ValidatedProfileState
from omr_grader.ui.settings_page import SettingsPageRequest
from omr_grader.workbooks.schemas import RESPONSE_SHEET_NAME


def _entry(session_id: str = "session-a") -> DashboardIndexEntry:
    return DashboardIndexEntry(
        session_id,
        1,
        f"generation-{session_id}",
        "a" * 64,
        "261008_120000_시험A",
        "시험A",
        None,
        ExamTerm.UNSPECIFIED,
        SessionState.GRADED,
        "2026-10-08T03:00:00.000000Z",
        12,
        "70",
        "90",
        "50",
        0,
    )


def _setup(qtbot, **ports):
    window = MainWindow()
    qtbot.addWidget(window)
    ports.setdefault("settings_load", lambda: Ok(SettingsState(Settings("", 3, False), 1)))
    controller = AppController(
        window, window.scan_page, window.grading_page, ServicePorts(**ports), write_enabled=True
    )
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)
    dialogs: list[tuple[str, str]] = []
    window.show_error_dialog = lambda title, message: dialogs.append((title, message))  # type: ignore[method-assign]
    return window, controller, dialogs


def _hold(controller: AppController, window: MainWindow) -> Event:
    """Start a worker that runs until the returned event is set."""
    release = Event()
    controller._start(
        window.scan_page, uuid4().hex, lambda _c, _p: release.wait(5), kind="desktop-service"
    )
    return release


def test_grading_from_the_dashboard_is_refused_while_a_job_runs(qtbot) -> None:
    window, controller, dialogs = _setup(qtbot)
    release = _hold(controller, window)
    entry = _entry()

    controller._grade_from_dashboard(entry)

    assert dialogs == [("작업 중", _BUSY_TEXT)]
    assert not window.grading_page.has_connected_session(entry.session_id, entry.revision)
    release.set()
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)
    # The job that ended did not connect the grading page to anything either.
    assert not window.grading_page.has_connected_session(entry.session_id, entry.revision)

    controller._grade_from_dashboard(entry)

    assert window.grading_page.has_connected_session(entry.session_id, entry.revision)
    assert len(dialogs) == 1
    controller.close()


def test_the_grading_stop_button_stays_hidden_without_a_working_cancel(qtbot) -> None:
    window, controller, _ = _setup(qtbot)
    grading = window.grading_page

    assert grading.cancel_button.isHidden()
    grading.set_busy(True)
    grading.set_grading_progress(GradingProgressDisplay(1, 4, 0, None))
    grading.set_grading_progress(GradingProgressDisplay(2, 4, 0, None))
    assert grading.cancel_button.isHidden()
    assert not grading.cancel_button.isEnabled()
    grading.set_busy(False)
    controller.close()

    # A service that really stops grading brings the button back.
    window2, controller2, _ = _setup(qtbot, cancel_operation=lambda _command: Ok(None))
    window2.grading_page.set_busy(True)
    assert not window2.grading_page.cancel_button.isHidden()
    controller2.close()


def test_the_scan_cancel_still_reaches_its_service(qtbot) -> None:
    cancelled: list[CancelOperationCommand] = []

    class Scan:
        def __init__(self) -> None:
            self.release = Event()

        def run_scan(self, command, progress=None):
            self.release.wait(5)
            return Ok("done")

        def cancel_scan(self, command):
            cancelled.append(command)
            self.release.set()
            return Ok(None)

    service = Scan()
    window, controller, _ = _setup(qtbot, scan=service)
    controller.start_scan(
        ScanPageRequest(
            exam_name="시험",
            profile=ValidatedProfileState("p", "p.omrtemplate", (1, 1), "100문항", validated=True),
            roster_path=None,
            source=ImportSelection(ImportKind.FOLDER, ("page.png",)),
            sensitivity=3,
            session_id="session",
        )
    )
    operation_id = controller._active_operation_id
    bridge = controller._active_bridge
    assert bridge is not None
    controller.cancel_active(operation_id)
    # A cancelled worker counts as running until the controller has reset its pages: it is
    # never "idle" while its bridge is still there.
    idle_with_bridge: list[bool] = []

    def poll() -> bool:
        if controller._active_bridge is None:
            return True
        idle_with_bridge.append(not controller._worker_active())
        return False

    qtbot.waitUntil(poll, timeout=5000)
    assert cancelled == [CancelOperationCommand(operation_id)]
    assert not any(idle_with_bridge)
    assert not controller._worker_active()
    controller.close()


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_a_stale_index_warning_never_replaces_the_success_message(qtbot) -> None:
    stale = ErrorInfo(
        "DASHBOARD_INDEX_STALE",
        "warning.dashboard_index_stale",
        context={"reason": "목록을 다시 만들었습니다."},
    )
    loads: list[int] = []

    def load():
        loads.append(1)
        return Ok(DashboardListing((_entry(),), (stale,)), (stale,))

    window, controller, _ = _setup(
        qtbot,
        dashboard_load=load,
        dashboard_delete=lambda selection: Ok(
            SoftDeleteResult(True, "trash", IndexState.STALE, "operation")
        ),
    )
    before = len(loads)

    records = _Records()
    logger = logging.getLogger("omr_grader.ui.controller")
    logger.addHandler(records)
    try:
        controller._handle_dashboard_request(
            DashboardRequest("delete", DashboardSelection(("session-a",), (1,)))
        )
        # The delete finishes, then the list is read again and carries the warning.
        qtbot.waitUntil(lambda: len(loads) > before, timeout=5000)
        qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)
    finally:
        logger.removeHandler(records)

    assert window.status_label.text() == "시험을 휴지통으로 옮겼습니다."
    assert window.status_label.property("role") in ("", None)
    # Still logged for support.
    assert any("DASHBOARD_INDEX_STALE" in message for message in records.messages)
    controller.close()


def test_a_failed_settings_save_does_not_leave_the_saving_text(qtbot) -> None:
    window, controller, _ = _setup(
        qtbot,
        settings_save=lambda command: Err(
            (
                ErrorInfo(
                    "SETTINGS_SAVE_FAILED",
                    "error.settings_save_failed",
                    context={"reason": "저장하지 못했습니다."},
                ),
            )
        ),
    )
    page = window.settings_page

    controller._save_settings(SettingsPageRequest(Settings("", 5, False), 1))
    assert page.status_label.text() == "설정을 저장하고 있습니다."
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)

    # The page itself says why, instead of keeping the saving text.
    assert page.status_label.text() == "저장하지 못했습니다."
    assert page._busy is False
    controller.close()


def test_a_failed_profile_import_ends_the_busy_text_with_the_error(qtbot) -> None:
    window, controller, _ = _setup(
        qtbot,
        profile_import=lambda request: Err(
            (
                ErrorInfo(
                    "PROFILE_INVALID",
                    "error.profile_invalid",
                    context={"reason": "양식을 읽지 못했습니다."},
                ),
            )
        ),
    )
    page = window.settings_page

    controller._import_profile(ImportSelection(ImportKind.PROFILE, ("C:/외부/가져온.omrtemplate",)))
    assert page.status_label.text() == "OMR 프로필을 가져오고 있습니다."
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)

    assert "가져오고 있습니다" not in page.status_label.text()
    assert page._busy is False
    controller.close()


def test_a_failed_data_import_resets_both_texts(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "D:/old")
    window, controller, dialogs = _setup(
        qtbot,
        data_import=lambda folder, report: Err(
            (
                ErrorInfo(
                    "IMPORT_FAILED",
                    "error.import_failed",
                    context={"reason": "폴더가 맞지 않습니다."},
                ),
            )
        ),
    )
    page = window.settings_page

    controller._import_previous()
    assert page.update_status_label.text().startswith("이전 버전 자료를 가져오는 중")
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)

    assert page.update_status_label.text().startswith("가져오지 못했습니다.")
    assert "가져오고 있습니다" not in page.status_label.text()
    assert page._busy is False
    assert dialogs and dialogs[-1][0] == "이전 버전 자료 가져오기"
    controller.close()


def test_a_response_workbook_start_does_not_claim_ocr_recognition(qtbot) -> None:
    display = ConnectedSessionDisplay("fresh-session", 1, "새 시험", "responses.xlsx")
    window, controller, _ = _setup(
        qtbot,
        fresh_response_picker=lambda _: ("responses.xlsx", RESPONSE_SHEET_NAME),
        import_fresh_response_selection=lambda intent, selection: Ok(display),
    )

    controller.start_fresh_response()
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)

    assert window.session_progress_label.text() == "응답 엑셀을 불러왔습니다."
    assert "응답 엑셀을 불러왔습니다." in window.scan_page.progress_label.text()
    assert "OMR 인식" not in window.scan_page.progress_label.text()
    assert "OMR 인식" not in window.session_progress_label.text()
    controller.close()


def test_the_empty_dashboard_offers_the_import_but_a_filtered_one_does_not(
    qtbot, monkeypatch
) -> None:
    folders: list[str] = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "D:/old")
    listing = [DashboardListing(())]

    def importer(folder, report):
        folders.append(folder)
        return Ok(ImportSummary(0, 0, (), 0, 0, None))

    window, controller, _ = _setup(
        qtbot, dashboard_load=lambda: Ok(listing[0]), data_import=importer, first_run=True
    )
    page = window.dashboard_page
    assert page.empty_state.isVisibleTo(page)

    page.import_previous_button.click()
    qtbot.waitUntil(lambda: folders == ["D:/old"], timeout=5000)
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)

    # Exams present: no offer. A search that hides every row is not an empty folder.
    page.set_entries((_entry(),))
    assert not page.empty_state.isVisibleTo(page)
    page.search_edit.setText("없는 시험")
    assert page.model.rowCount() == 0
    assert not page.empty_state.isVisibleTo(page)
    controller.close()


def test_no_import_offer_without_the_import_service(qtbot) -> None:
    window, controller, _ = _setup(qtbot, dashboard_load=lambda: Ok(DashboardListing(())))

    assert not window.dashboard_page.empty_state.isVisibleTo(window.dashboard_page)
    controller.close()


def test_actions_that_ask_first_are_refused_before_the_question(qtbot, monkeypatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: asked.append("dir") or "D:/old"
    )
    picked: list[str] = []
    window, controller, dialogs = _setup(
        qtbot,
        data_import=lambda folder, report: Ok(ImportSummary(0, 0, (), 0, 0, None)),
        fresh_response_picker=lambda intent: picked.append("picker") or None,
        import_fresh_response_selection=lambda intent, selection: None,
        combined_export=lambda ids, config, destination: Ok(object()),
    )
    release = _hold(controller, window)
    factory_calls: list[int] = []
    controller._combined_dialog_factory = lambda *a, **k: factory_calls.append(1)  # type: ignore[assignment]
    settings_text = window.settings_page.update_status_label.text()

    controller._import_previous()
    controller.start_fresh_response()
    controller._combine_from_dashboard(DashboardSelection(("a", "b"), (1, 1)))
    window.dashboard_page._request("restore")

    assert asked == []
    assert picked == []
    assert factory_calls == []
    assert window.dashboard_page._file_dialog is None
    assert dialogs == [("작업 중", _BUSY_TEXT)] * 4
    assert window.settings_page.update_status_label.text() == settings_text
    release.set()
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)
    controller.close()


def test_an_action_asked_for_as_a_worker_ends_waits_instead_of_being_refused(qtbot) -> None:
    window, controller, dialogs = _setup(qtbot)
    results: list[object] = []
    started: list[bool] = []
    deferred: list[bool] = []
    # Runs from the terminal signal of the first worker: its operation has returned but its
    # thread is still ending, the moment a click used to be refused.
    release = _hold(controller, window)
    bridge = controller._active_bridge
    assert bridge is not None

    def ask_again() -> None:
        started.append(
            controller._start_desktop_action(
                window.dashboard_page,
                lambda: "second",
                results.append,
                window.dashboard_page.set_busy,
            )
        )
        deferred.append(controller._deferred_start is not None)

    class Relay(QObject):
        ping = Signal()

        @Slot()
        def on_ping(self) -> None:
            ask_again()

    # Queued, so it runs after the terminal handlers but before the thread has gone.
    relay = Relay()
    relay.ping.connect(relay.on_ping, Qt.ConnectionType.QueuedConnection)
    bridge.terminal.connect(relay.ping)
    release.set()
    qtbot.waitUntil(lambda: results == ["second"], timeout=5000)
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)

    assert started == [True]
    # It really arrived while the first thread was ending, and still ran afterwards.
    assert deferred == [True]
    assert dialogs == []
    assert window.dashboard_page._busy is False
    assert window.status_label.text() != "처리 중"
    controller.close()


def test_a_real_overlap_is_still_refused_with_the_busy_message(qtbot) -> None:
    window, controller, dialogs = _setup(qtbot)
    release = _hold(controller, window)

    started = controller._start_desktop_action(
        window.dashboard_page, lambda: "x", lambda _r: None, window.dashboard_page.set_busy
    )

    assert started is False
    assert dialogs == [("작업 중", _BUSY_TEXT)]
    release.set()
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)
    controller.close()


def _trash_entries() -> tuple[DashboardIndexEntry, ...]:
    return (_entry("session-a"), _entry("session-b"), _entry("session-c"))


def test_trash_batches_continue_past_a_failure_reload_and_report_counts(qtbot) -> None:
    calls: list[tuple[str, ...]] = []
    loads: list[int] = []

    def trash(request):
        calls.append(request.selection.session_ids)
        if request.selection.session_ids == ("session-b",):
            return Err(
                (
                    ErrorInfo(
                        "SESSION_LOCKED",
                        "error.session_locked",
                        context={"reason": "다른 프로그램에서 열려 있습니다."},
                    ),
                )
            )
        if request.action == "trash_restore":
            return Ok(TrashRestoreResult(True, "Data", IndexState.CURRENT, uuid4().hex))
        return Ok(
            PermanentDeleteResult(True, IndexState.CURRENT, CleanupState.COMPLETE, uuid4().hex)
        )

    def load():
        loads.append(1)
        return Ok(DashboardListing(()))

    window, controller, dialogs = _setup(qtbot, dashboard_load=load, dashboard_trash=trash)
    before = len(loads)

    controller._handle_dashboard_request(
        DashboardRequest(
            "trash_restore",
            DashboardSelection(("session-a", "session-b", "session-c"), (1, 1, 1)),
        )
    )
    qtbot.waitUntil(lambda: len(loads) > before and controller._active_bridge is None, timeout=5000)

    # The failure in the middle did not stop the third exam.
    assert calls == [("session-a",), ("session-b",), ("session-c",)]
    assert window.status_label.text() == "시험 3개 중 2개를 복원했고 1개는 하지 못했습니다."
    assert dialogs and "다른 프로그램에서 열려 있습니다." in dialogs[-1][1]
    assert "2개" in dialogs[-1][1] and "1개" in dialogs[-1][1]
    controller.close()


def test_a_failing_single_trash_action_still_reloads_the_list(qtbot) -> None:
    loads: list[int] = []

    def load():
        loads.append(1)
        return Ok(DashboardListing(()))

    window, controller, dialogs = _setup(
        qtbot,
        dashboard_load=load,
        dashboard_trash=lambda request: Err(
            (
                ErrorInfo(
                    "SESSION_LOCKED", "error.session_locked", context={"reason": "잠겨 있습니다."}
                ),
            )
        ),
    )
    before = len(loads)

    controller._handle_dashboard_request(
        DashboardRequest("trash_delete", DashboardSelection(("session-a",), (1,)))
    )
    qtbot.waitUntil(lambda: len(loads) > before and controller._active_bridge is None, timeout=5000)

    assert window.status_label.text() == "시험 1개를 영구 삭제하지 못했습니다."
    assert dialogs
    controller.close()


def test_a_failed_dashboard_action_reloads_the_list(qtbot) -> None:
    loads: list[int] = []

    def load():
        loads.append(1)
        return Ok(DashboardListing(()))

    window, controller, _ = _setup(
        qtbot,
        dashboard_load=load,
        dashboard_delete=lambda selection: Err(
            (
                ErrorInfo(
                    "SESSION_LOCKED", "error.session_locked", context={"reason": "잠겨 있습니다."}
                ),
            )
        ),
    )
    before = len(loads)

    controller._handle_dashboard_request(
        DashboardRequest("delete", DashboardSelection(("session-a",), (1,)))
    )
    qtbot.waitUntil(lambda: len(loads) > before and controller._active_bridge is None, timeout=5000)

    controller.close()


def test_refresh_requests_during_a_job_wait_without_a_dialog(qtbot) -> None:
    window, controller, dialogs = _setup(qtbot, dashboard_load=lambda: Ok(DashboardListing(())))
    release = _hold(controller, window)

    controller._handle_dashboard_request(DashboardGlobalRequest("refresh"))

    assert dialogs == []
    release.set()
    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=5000)
    controller.close()
