from __future__ import annotations

from threading import Event
from types import MappingProxyType

from PySide6.QtCore import QCoreApplication, QEvent

from omr_grader.application.detail_presenter import (
    DetailAnswerDisplay,
    DetailAnswerEdit,
    DetailLoadResult,
    DetailPageDisplay,
    DetailPageRequest,
    DetailPreviewResult,
    DetailSaveResult,
    DetailStudentDisplay,
    DetailSummaryDisplay,
    NormalizedCell,
)
from omr_grader.application.dto import (
    CancelOperationCommand,
    CommitGenerationResult,
    RegradeCommand,
    ScanCommand,
    ScanProgress,
    SessionCreateResult,
    Settings,
    SettingsSaveCommand,
    SettingsSaveResult,
)
from omr_grader.application.grading_presenter import (
    ConnectedSessionDisplay,
    GradingPageRequest,
    GradingProgressDisplay,
)
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.bootstrap import _canonical_response_workbook_selection
from omr_grader.domain.enums import IndexState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.ui.app_controller import AppController, FreshResponseIntent, ServicePorts
from omr_grader.ui.dashboard_model import DashboardSelection
from omr_grader.ui.dashboard_page import DashboardRequest
from omr_grader.ui.grading_page import GradingPage
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPage, ScanPageRequest, ValidatedProfileState
from omr_grader.ui.worker_bridge import WorkerBridge
from omr_grader.workbooks.schemas import RESPONSE_SHEET_NAME


def _window(qtbot) -> tuple[MainWindow, ScanPage, GradingPage]:
    window = MainWindow()
    qtbot.addWidget(window)
    return window, window.scan_page, window.grading_page


def _ready_ports(**kwargs) -> ServicePorts:
    return ServicePorts(
        settings_load=lambda: Ok(SettingsState(Settings("", 3, False), 1)),
        **kwargs,
    )


def test_bootstrap_response_workbook_route_is_strict_and_canonical() -> None:
    assert _canonical_response_workbook_selection("generated-response.xlsx") == (
        "generated-response.xlsx",
        RESPONSE_SHEET_NAME,
    )
    assert _canonical_response_workbook_selection("legacy-response.xls") is None


def test_dashboard_detail_request_uses_controller_port_and_persisted_detail(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    request = DashboardRequest("detail", DashboardSelection(("session",), (1,)))
    display = DetailPageDisplay("session", 1, "시험", DetailSummaryDisplay(0, "0", "0", "0"), ())
    calls: list[DashboardRequest] = []
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            dashboard_detail=lambda value: calls.append(DashboardRequest("detail", value))
            or Ok(display),
        ),
        write_enabled=True,
    )

    window.dashboard_page.request_emitted.emit(request)
    qtbot.waitUntil(lambda: calls == [request])
    qtbot.waitUntil(lambda: window.exam_page.currentWidget() is window.detail_page)

    assert calls == [request]
    assert window.exam_page.currentWidget() is window.detail_page
    assert window.detail_page.title_label.text() == "시험 상세 결과"
    window.detail_page.request_back()
    assert window.exam_page.currentWidget() is window.dashboard_page
    controller.close()


def test_detail_save_accepts_advanced_revision_with_replaced_handle(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    student = DetailStudentDisplay(
        "work-item",
        "00000001",
        "홍길동",
        1,
        "0",
        (DetailAnswerDisplay(1, 1, False),),
        cells=(NormalizedCell("answer", 1, 2, 0.1, 0.1, 0.1, 0.1),),
        id_digits=(0, 0, 0, 0, 0, 0, 0, 1),
    )
    current = DetailPageDisplay(
        "session", 1, "시험", DetailSummaryDisplay(1, "0", "0", "0"), (student,), "old-handle"
    )
    committed = DetailPageDisplay(
        "session", 2, "시험", DetailSummaryDisplay(1, "0", "0", "0"), (student,), "new-handle"
    )
    saved = []
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            detail_load=lambda value: Ok(DetailLoadResult(value.correlation_id, student)),
            detail_preview=lambda value: Ok(DetailPreviewResult(value.correlation_id, current)),
            detail_save=lambda value: saved.append(value)
            or Ok(DetailSaveResult(value.correlation_id, committed)),
        ),
        write_enabled=True,
    )
    window.detail_page.set_display(current)
    window.detail_page.graphics_view.cell_activated.emit(student.cells[0])
    window.detail_page.request_save()

    assert saved and saved[-1].detail_handle == "old-handle"
    assert window.detail_page._display == committed
    controller.close()


def test_detail_cancel_clears_pending_sidebar_navigation(qtbot, monkeypatch) -> None:
    window, scan, grading = _window(qtbot)
    display = DetailPageDisplay(
        "session", 1, "시험", DetailSummaryDisplay(0, "0", "0", "0"), (), "handle"
    )
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)
    window.detail_page.set_display(display)
    window.show_detail()
    window.detail_page._edits[("answer", "work-item", 1)] = DetailAnswerEdit("work-item", 1, 1, 2)
    monkeypatch.setattr(window, "confirm_detail_exit", lambda: "cancel")

    controller._detail_navigation_requested(2)

    assert controller._pending_detail_exit is None
    assert controller._pending_navigation_page is None
    assert window.exam_page.currentWidget() is window.detail_page
    controller.close()


def test_malformed_detail_save_result_preserves_edits_and_allows_retry(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    request = DetailPageRequest(
        "session",
        1,
        "save",
        (DetailAnswerEdit("work-item", 1, 1, 2),),
        "handle",
        "correlation",
    )
    controller = AppController(
        window, scan, grading, ServicePorts(detail_save=lambda _: Ok(object())), write_enabled=True
    )
    window.detail_page.set_display(
        DetailPageDisplay(
            "session", 1, "시험", DetailSummaryDisplay(0, "0", "0", "0"), (), "handle"
        )
    )
    window.detail_page._edits[("answer", "work-item", 1)] = request.edits[0]
    controller._pending_detail_exit = request
    controller._pending_navigation_page = 2

    window.detail_page.request_save()

    assert window.detail_page.is_dirty and window.detail_page.save_button.isEnabled()
    assert controller._pending_detail_exit is None
    assert controller._pending_navigation_page is None
    controller.close()


def test_settings_save_is_disabled_when_service_is_unavailable(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)

    window.settings_page.save_requested.emit(object())

    assert "현재 작업 서비스를 사용할 수 없습니다" in window.settings_page.status_label.text()
    controller.close()


def test_settings_empty_profile_can_save_other_valid_settings(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)
    window.settings_page.set_settings(Settings("", 3, False), 1)
    requests = []
    window.settings_page.save_requested.connect(requests.append)

    window.settings_page.sensitivity_slider.setValue(5)
    window.settings_page.multiprocessing_checkbox.setChecked(True)
    window.settings_page.save_button.click()

    assert len(requests) == 1
    assert requests[0].settings == Settings("", 5, True)
    controller.close()


def _grading_request() -> GradingPageRequest:
    return GradingPageRequest(
        "session",
        1,
        "responses.xlsx",
        "answers.xlsx",
        "Sheet1",
        "grade-operation",
        "other_response",
        True,
    )


class FakeGradingService:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[RegradeCommand] = []

    def regrade(self, command: RegradeCommand) -> object:
        self.calls.append(command)
        return self.result


def test_fresh_response_cancel_creates_no_session_or_import(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    picker_calls: list[FreshResponseIntent] = []
    import_calls: list[object] = []
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            fresh_response_picker=lambda intent: picker_calls.append(intent) or None,
            import_fresh_response_selection=lambda intent, selection: import_calls.append(
                (intent, selection)
            ),
        ),
        write_enabled=True,
    )

    controller.start_fresh_response()

    assert len(picker_calls) == 1
    assert import_calls == []
    assert controller._active_bridge is None
    assert window.session_name_label.text() == "진행 중인 세션이 없습니다"
    controller.close()


def test_fresh_response_write_denial_does_not_open_picker(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    picker_calls: list[FreshResponseIntent] = []
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(fresh_response_picker=lambda intent: picker_calls.append(intent) or None),
        write_enabled=False,
    )

    controller.start_fresh_response()

    assert picker_calls == []
    assert "쓸 권한" in scan.progress_label.text()
    controller.close()


def test_fresh_response_committed_generation_one_connects_and_navigates(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    imported: list[tuple[FreshResponseIntent, tuple[str, str]]] = []
    display = ConnectedSessionDisplay("fresh-session", 1, "새 시험", "responses.xlsx")
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            fresh_response_picker=lambda _: ("responses.xlsx", RESPONSE_SHEET_NAME),
            import_fresh_response_selection=lambda intent, selection: imported.append(
                (intent, selection)
            )
            or Ok(display),
        ),
        write_enabled=True,
    )

    controller.start_fresh_response()
    qtbot.waitUntil(lambda: controller._active_bridge is None)

    assert len(imported) == 1
    assert imported[0][0].operation_id
    assert imported[0][1] == ("responses.xlsx", RESPONSE_SHEET_NAME)
    assert grading._session == display
    assert window.session_name_label.text() == "새 시험"
    assert window.pages.currentIndex() == window.GRADING_PAGE
    controller.close()


def test_fresh_response_invalid_generation_fails_closed(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            fresh_response_picker=lambda _: ("responses.xlsx", RESPONSE_SHEET_NAME),
            import_fresh_response_selection=lambda _, __: Ok(
                ConnectedSessionDisplay("fresh-session", 0, "새 시험", "responses.xlsx")
            ),
        ),
        write_enabled=True,
    )

    controller.start_fresh_response()
    qtbot.waitUntil(lambda: "올바르지 않은 응답" in scan.progress_label.text())

    assert window.pages.currentIndex() == window.SCAN_PAGE
    assert "응답 결과를 먼저" in grading.session_label.text()
    controller.close()


def test_fresh_response_busy_blocks_double_submit(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    started, release = Event(), Event()
    picker_calls: list[FreshResponseIntent] = []
    import_calls: list[FreshResponseIntent] = []

    def importer(intent: FreshResponseIntent, _: tuple[str, str]) -> Ok[ConnectedSessionDisplay]:
        import_calls.append(intent)
        started.set()
        release.wait()
        return Ok(ConnectedSessionDisplay("fresh-session", 1, "새 시험", "responses.xlsx"))

    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            fresh_response_picker=lambda intent: picker_calls.append(intent)
            or ("responses.xlsx", RESPONSE_SHEET_NAME),
            import_fresh_response_selection=importer,
        ),
        write_enabled=True,
    )

    controller.start_fresh_response()
    assert started.wait(1)
    assert not scan.cancel_button.isEnabled()
    controller.start_fresh_response()
    release.set()
    qtbot.waitUntil(lambda: controller._active_bridge is None)

    assert len(picker_calls) == 1
    assert len(import_calls) == 1
    controller.close()


def test_other_response_import_does_not_change_answer_key_and_retries(qtbot):
    window, scan, grading = _window(qtbot)
    request = _grading_request()
    selections: list[tuple[str, str]] = []
    attempts = 0

    def pick_response(value: GradingPageRequest) -> tuple[str, str]:
        assert value is request
        return ("other-responses.xlsx", "Responses")

    def import_response(
        value: GradingPageRequest, selection: tuple[str, str]
    ) -> Ok[ConnectedSessionDisplay] | Err:
        nonlocal attempts
        attempts += 1
        assert value is request
        selections.append(selection)
        if attempts == 1:
            return Err((ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied"),))
        return Ok(ConnectedSessionDisplay("other-session", 2, "다른 시험", selection[0]))

    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            other_response_picker=pick_response,
            import_response_selection=import_response,
        ),
        write_enabled=True,
    )
    grading.set_answer_key_selection("answers.xlsx", "Sheet1")
    grading.set_connected_session(
        ConnectedSessionDisplay("session-a", 1, "기존 시험", "existing.xlsx")
    )
    grading.set_operation_id("existing-grade")
    grading.set_result_available("session-a", 1)

    controller._pick_other_response(request)
    assert grading.result_button.isHidden()
    qtbot.waitUntil(lambda: "쓸 권한" in grading.error_label.text())
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert grading.key_label.text() == "선택한 정답표: answers.xlsx (시트: Sheet1)"

    controller._pick_other_response(request)
    qtbot.waitUntil(lambda: "다른 시험" in grading.session_label.text())
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert grading.result_button.isHidden()
    assert grading._operation_id is not None

    assert selections == [
        ("other-responses.xlsx", "Responses"),
        ("other-responses.xlsx", "Responses"),
    ]
    assert window.session_name_label.text() == "다른 시험"
    controller.close()


def test_response_import_requires_connected_session_display(qtbot):
    window, scan, grading = _window(qtbot)
    request = _grading_request()
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(
            other_response_picker=lambda _: ("other.xlsx", "Sheet1"),
            import_response_selection=lambda _, __: Ok("not a session"),
        ),
        write_enabled=True,
    )

    controller._pick_other_response(request)
    qtbot.waitUntil(lambda: "올바르지 않은 응답" in grading.error_label.text())

    assert "응답 결과를 먼저" in grading.session_label.text()
    assert grading.result_button.isHidden()
    controller.close()


def test_grading_calls_regrade_service(qtbot):
    window, scan, grading = _window(qtbot)
    service = FakeGradingService(
        Ok(
            CommitGenerationResult(
                True,
                "session",
                2,
                "generation-2",
                IndexState.CURRENT,
                "grade-operation",
            )
        )
    )
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            grading=service,
            session_display=lambda _: Ok(
                ConnectedSessionDisplay("session", 2, "시험", "responses.xlsx", True)
            ),
        ),
        write_enabled=True,
    )
    request = _grading_request()
    grading.set_connected_session(ConnectedSessionDisplay("session", 1, "시험", "responses.xlsx"))
    grading.set_operation_id(request.operation_id)

    controller.start_grading(request)
    qtbot.waitUntil(lambda: not grading.result_button.isHidden())

    assert service.calls == [
        RegradeCommand("session", 1, "answers.xlsx", "Sheet1", "grade-operation")
    ]
    controller.close()


def test_grading_success_requires_matching_connected_identity(qtbot):
    window, scan, grading = _window(qtbot)
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            grading=FakeGradingService(Ok("graded")),
            session_display=lambda _: Ok(
                ConnectedSessionDisplay("session", 2, "시험", "responses.xlsx", True)
            ),
        ),
        write_enabled=True,
    )
    grading.set_connected_session(
        ConnectedSessionDisplay("different-session", 1, "다른 시험", "other.xlsx")
    )
    grading.set_operation_id("grade-operation")

    controller.start_grading(_grading_request())
    qtbot.waitUntil(lambda: "올바르지 않은 응답" in grading.error_label.text())

    assert grading.result_button.isHidden()
    controller.close()


def test_invalid_session_display_never_mutates_success_ui(qtbot):
    window, scan, grading = _window(qtbot)
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            scan=FakeScanService(_committed_scan_result),
            session_display=lambda _: MappingProxyType({"session_id": "invalid"}),
        ),
        write_enabled=True,
    )

    controller.start_scan(_request())
    qtbot.waitUntil(lambda: "올바르지 않은 응답" in scan.progress_label.text())

    assert "응답 결과를 먼저" in grading.session_label.text()
    assert window.session_name_label.text() == "진행 중인 세션이 없습니다"
    controller.close()


def test_dashboard_materialization_error_never_marks_session_complete(qtbot):
    window, scan, grading = _window(qtbot)
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            scan=FakeScanService(_committed_scan_result),
            session_display=lambda _: Err(
                (
                    ErrorInfo(
                        "DASHBOARD_SESSION_NOT_FOUND",
                        "error.dashboard_session_not_found",
                        context={"reason": "저장된 시험을 찾을 수 없습니다."},
                    ),
                )
            ),
        ),
        write_enabled=True,
    )

    controller.start_scan(_request())
    qtbot.waitUntil(lambda: "저장된 시험을 찾을 수 없습니다." in scan.progress_label.text())

    assert "응답 결과를 먼저" in grading.session_label.text()
    assert window.session_name_label.text() == "진행 중인 세션이 없습니다"
    assert window.status_label.text() == "실패"
    controller.close()


def test_context_operations_propagate_progress_and_cooperative_cancel(qtbot):
    window, scan, grading = _window(qtbot)
    scan_cancelled = Event()
    grading_cancelled = Event()
    grading_cancel_commands: list[CancelOperationCommand] = []

    def scan_context(command, cancelled, progress):
        progress(ScanProgress(1, 2, 0, 100, 100))
        cancelled.wait(2)
        scan_cancelled.set()
        return Ok(command.operation_id)

    def grading_context(command, cancelled, progress):
        progress(GradingProgressDisplay(1, 2, 1, 1, "채점 중"))
        cancelled.wait(2)
        grading_cancelled.set()
        return Ok(command.operation_id)

    def cancel_grading(command: CancelOperationCommand) -> None:
        grading_cancel_commands.append(command)

    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            scan_context=scan_context,
            grading_context=grading_context,
            cancel_operation=cancel_grading,
        ),
        write_enabled=True,
    )

    controller.start_scan(_request())
    qtbot.waitUntil(lambda: "1 / 2" in scan.progress_label.text())
    scan_operation_id = controller._active_operation_id
    controller.cancel_active(scan_operation_id)
    qtbot.waitUntil(scan_cancelled.is_set)
    qtbot.waitUntil(lambda: controller._active_bridge is None)

    request = _grading_request()
    controller.start_grading(request)
    qtbot.waitUntil(lambda: controller._active_bridge is not None)
    controller.cancel_active(request.operation_id)
    qtbot.waitUntil(grading_cancelled.is_set)
    qtbot.waitUntil(lambda: controller._active_bridge is None)

    assert grading_cancel_commands == [CancelOperationCommand(request.operation_id)]
    assert grading._operation_id is not None


def test_worker_exceptions_are_presented_as_typed_errors(qtbot):
    window, scan, grading = _window(qtbot)
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)
    raised: list[str] = []

    def raise_exception(_, __):
        raised.append("exception")
        raise RuntimeError("broken")

    controller._start(scan, "exception", raise_exception)
    qtbot.waitUntil(lambda: "broken" in scan.progress_label.text())
    qtbot.waitUntil(lambda: controller._active_bridge is None)

    def raise_base_exception(_, __):
        raised.append("base-exception")
        raise KeyboardInterrupt("interrupted")

    controller._start(scan, "base-exception", raise_base_exception)
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert "interrupted" in scan.progress_label.text()

    assert raised == ["exception", "base-exception"]
    controller.close()


def test_completed_bridges_are_qt_disposed_after_bounded_retention(qtbot):
    window, scan, grading = _window(qtbot)
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)

    for number in range(65):
        controller._start(scan, f"operation-{number}", lambda _, __: "complete")
        qtbot.waitUntil(lambda: controller._active_bridge is None)

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()

    assert len(controller.findChildren(WorkerBridge)) <= 64
    controller.close()


def test_late_terminal_and_close_suppress_view_updates(qtbot):
    window, scan, grading = _window(qtbot)
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)
    controller._active_page = scan
    controller._active_operation_id = "active"

    controller._terminal()
    controller._terminal()
    controller._failed(ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied"))
    assert "쓸 권한" not in scan.progress_label.text()

    controller.close()
    controller._active_page = scan
    controller._succeeded("late")
    assert scan.progress_label.text() != "late"


def test_runtime_write_denial_blocks_new_writes_and_cancel_uses_command(qtbot):
    window, scan, grading = _window(qtbot)
    service = BlockingScanService()
    controller = AppController(
        window, scan, grading, _ready_ports(scan=service), write_enabled=True
    )
    controller.start_scan(_request())
    qtbot.waitUntil(lambda: len(service.calls) == 1)
    operation_id = controller._active_operation_id

    controller.set_write_authority(False)
    controller.start_scan(_request())
    qtbot.waitUntil(lambda: "쓸 권한" in scan.progress_label.text())

    assert len(service.calls) == 1
    controller.cancel_active(operation_id)
    qtbot.waitUntil(lambda: bool(service.cancelled))
    assert service.cancelled == [CancelOperationCommand(operation_id)]
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    controller.close()


class FakeScanService:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[ScanCommand] = []
        self.cancelled: list[CancelOperationCommand] = []

    def run_scan(self, command: ScanCommand) -> object:
        self.calls.append(command)
        if callable(self.result):
            return self.result(command)
        return self.result

    def cancel_scan(self, command: CancelOperationCommand) -> None:
        self.cancelled.append(command)


class BlockingScanService(FakeScanService):
    def __init__(self) -> None:
        super().__init__(Ok("complete"))
        self.release = Event()

    def run_scan(self, command: ScanCommand) -> object:
        self.calls.append(command)
        self.release.wait()
        return self.result

    def cancel_scan(self, command: CancelOperationCommand) -> None:
        self.cancelled.append(command)
        self.release.set()


def _request() -> ScanPageRequest:
    return ScanPageRequest(
        exam_name="시험",
        profile=ValidatedProfileState(
            name="기본 프로필",
            path="profile.omrtemplate",
            dimensions=(1682, 1190),
            grid_summary="100문항",
            validated=True,
        ),
        roster_path=None,
        source=ImportSelection(ImportKind.FOLDER, ("page.png",)),
        sensitivity=3,
        session_id="session",
    )


def _committed_scan_result(command: ScanCommand) -> Ok[SessionCreateResult]:
    return Ok(
        SessionCreateResult(
            True,
            command.session_id,
            1,
            "generation-1",
            IndexState.CURRENT,
            command.operation_id,
        )
    )


def test_scan_success_updates_session_and_preserves_page(qtbot):
    window, scan, grading = _window(qtbot)
    service = FakeScanService(_committed_scan_result)
    session_results: list[SessionCreateResult] = []

    def session_display(result: SessionCreateResult) -> Ok[ConnectedSessionDisplay]:
        session_results.append(result)
        return Ok(ConnectedSessionDisplay("session", 1, "완료", "responses.xlsx"))

    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(scan=service, session_display=session_display),
        write_enabled=True,
    )

    scan.recognition_requested.emit(_request())
    qtbot.waitUntil(
        lambda: scan.progress_label.text() == "OMR 인식과 응답결과 생성이 완료되었습니다."
    )

    assert service.calls and not scan.cancel_button.isEnabled()
    assert len(session_results) == 1
    assert session_results[0].session_id == "session"
    assert session_results[0].operation_id == service.calls[0].operation_id
    assert window.session_name_label.text() == "완료"
    controller.close()


def test_scan_error_is_presented_in_korean(qtbot):
    window, scan, grading = _window(qtbot)
    failure = ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied")
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(scan=FakeScanService(Err((failure,)))),
        write_enabled=True,
    )

    scan.recognition_requested.emit(_request())
    qtbot.waitUntil(lambda: scan.progress_label.text() == "실행 폴더에 쓸 권한이 없습니다.")

    assert scan.progress_label.text() == "실행 폴더에 쓸 권한이 없습니다."
    controller.close()


def test_write_denied_never_calls_mutating_service(qtbot):
    window, scan, grading = _window(qtbot)
    service = FakeScanService(Ok("완료"))
    controller = AppController(
        window, scan, grading, ServicePorts(scan=service), write_enabled=False
    )

    scan.recognition_requested.emit(_request())
    qtbot.waitUntil(lambda: scan.progress_label.text() == "실행 폴더에 쓸 권한이 없습니다.")

    assert service.calls == []
    assert not scan.run_button.isEnabled()
    controller.close()


def test_settings_save_rejects_stale_or_mismatched_authoritative_reload(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    loaded = SettingsState(Settings("", 3, False), 1)
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(settings_load=lambda: Ok(loaded)),
        write_enabled=True,
    )
    command = SettingsSaveCommand(Settings("", 5, False), 1, "settings-operation")

    controller._finish_settings_save(SettingsSaveResult(True, 2, command.operation_id), command)

    assert controller._settings_snapshot == loaded.settings
    assert "저장되었습니다" not in window.settings_page.status_label.text()


def test_settings_save_delivers_authoritative_reload_warnings(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    current = SettingsState(Settings("", 3, False), 1)
    committed = SettingsState(Settings("", 5, False), 2)
    loads = iter(
        (Ok(current), Ok(committed, (ErrorInfo("PROFILE_WARNING", "warning.profile_warning"),)))
    )
    controller = AppController(
        window,
        scan,
        grading,
        ServicePorts(settings_load=lambda: next(loads)),
        write_enabled=True,
    )
    command = SettingsSaveCommand(Settings("", 5, False), 1, "settings-operation")
    controller._active_operation_id = command.operation_id

    controller._finish_settings_save(SettingsSaveResult(True, 2, command.operation_id), command)

    assert controller._settings_snapshot == committed.settings
    assert window.status_label.property("role") == "error"


def test_cancel_requires_the_exact_active_operation_id(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    controller = AppController(window, scan, grading, ServicePorts(), write_enabled=True)
    bridge = WorkerBridge(parent=controller)
    controller._active_bridge = bridge
    controller._active_operation_id = "active-operation"

    controller.cancel_active()
    controller.cancel_active("")
    controller.cancel_active("other-operation")

    assert bridge.lifecycle.value == "idle"
