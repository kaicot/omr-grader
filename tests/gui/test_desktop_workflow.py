from __future__ import annotations

from threading import Event

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from omr_grader.application.dto import (
    AnswerKeyValidation,
    CommitGenerationResult,
    ScanProgress,
    SessionCreateResult,
    Settings,
)
from omr_grader.application.grading_presenter import (
    ConnectedSessionDisplay,
    GradingProgressDisplay,
)
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    IndexState,
    KeyQuestionStatus,
)
from omr_grader.domain.errors import Ok
from omr_grader.domain.models import AnswerKeyEntry, AnswerKeySnapshot, AnswerValue
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ValidatedProfileState


def test_desktop_navigation_help_and_idle_close(qtbot):
    """Exercise all live workflow pages through the shell's native controls."""
    window = MainWindow()
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(),
        write_enabled=True,
        parent=window,
    )
    qtbot.addWidget(window)
    window.show()
    window.resize(window.minimumSize())
    window.scan_page.setMinimumHeight(1500)
    window.grading_page.setMinimumHeight(1500)

    scan_button, grading_button, exam_button, settings_button = window.nav_buttons
    window.scan_page.exam_name_edit.setText("키보드 탐색 시험")
    assert not grading_button.isEnabled()
    QTest.mouseClick(grading_button, Qt.MouseButton.LeftButton)
    assert window.pages.currentIndex() == MainWindow.SCAN_PAGE

    window.set_grading_available(True)
    scan_button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(scan_button.hasFocus)
    for button, page_index, key in (
        (grading_button, MainWindow.GRADING_PAGE, Qt.Key.Key_Space),
        (exam_button, MainWindow.EXAM_PAGE, Qt.Key.Key_Return),
        (settings_button, MainWindow.SETTINGS_PAGE, Qt.Key.Key_Space),
    ):
        QTest.keyClick(window.focusWidget(), Qt.Key.Key_Tab)
        qtbot.waitUntil(button.hasFocus)
        QTest.keyClick(button, key)
        assert window.pages.currentIndex() == page_index
        assert window.pages.currentWidget() is window.page_scroll_areas[page_index]

    QTest.keyClick(settings_button, Qt.Key.Key_Tab)
    qtbot.waitUntil(window.theme_button.hasFocus)
    QTest.keyClick(window.theme_button, Qt.Key.Key_Space)
    assert window.theme_button.text() == "밝은 테마"
    QTest.keyClick(window.theme_button, Qt.Key.Key_Tab)
    qtbot.waitUntil(window.help_button.hasFocus)
    QTest.keyClick(window.help_button, Qt.Key.Key_Return)
    assert "도움말" in window.status_label.text()

    for button, page_index in (
        (scan_button, MainWindow.SCAN_PAGE),
        (grading_button, MainWindow.GRADING_PAGE),
    ):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert window.pages.currentIndex() == page_index
        scroll_area = window.page_scroll_areas[page_index]
        qtbot.waitUntil(
            lambda scroll_area=scroll_area: scroll_area.verticalScrollBar().maximum() > 0
        )

    QTest.mouseClick(scan_button, Qt.MouseButton.LeftButton)
    assert window.scan_page.exam_name_edit.text() == "키보드 탐색 시험"
    window.close()
    assert not window.isVisible()
    assert controller.write_enabled


class _DesktopAnswerKeyService:
    def validate_answer_key(self, _request):
        entries = tuple(
            AnswerKeyEntry(
                question,
                AnswerValue((1,), AnswerStatus.NORMAL),
                "1",
                KeyQuestionStatus.ANSWER,
            )
            for question in range(1, 101)
        )
        snapshot = AnswerKeySnapshot(
            1,
            AnswerKeySnapshotKind.WORKBOOK,
            "answers.xlsx",
            "a" * 64,
            "Sheet1",
            "v1",
            entries,
            (),
        )
        return Ok(AnswerKeyValidation(snapshot))


def test_native_scan_to_grade_progress_import_and_write_denial(qtbot):
    """Drive the controller's real native controls through a complete typed workflow."""
    window = MainWindow()
    other_sessions: list[str] = []
    scan_sessions: list[str] = []
    result_navigation: list[tuple[str, int]] = []

    def scan_context(command, _cancelled, progress):
        scan_sessions.append(command.session_id)
        progress(ScanProgress(1, 2, 0, 100, 100))
        progress(ScanProgress(2, 2, 0, 200, 0))
        return Ok(
            SessionCreateResult(
                True,
                command.session_id,
                1,
                "scan-generation",
                IndexState.CURRENT,
                command.operation_id,
            )
        )

    def grading_context(command, _cancelled, progress):
        progress(GradingProgressDisplay(1, 2, 1, 1, "채점 중"))
        progress(GradingProgressDisplay(2, 2, 2, 0, "완료"))
        return Ok(
            CommitGenerationResult(
                True,
                command.session_id,
                command.expected_revision + 1,
                "grade-generation",
                IndexState.CURRENT,
                command.operation_id,
            )
        )

    def session_display(result):
        revision = result.revision
        return Ok(
            ConnectedSessionDisplay(
                result.session_id,
                revision,
                "통합 흐름 시험",
                "responses.xlsx",
                revision > 1,
            )
        )

    def import_other(_request, selection):
        other_sessions.append(selection[0])
        return Ok(ConnectedSessionDisplay("other", 1, "다른 응답 시험", selection[0]))

    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            scan_context=scan_context,
            grading_context=grading_context,
            answer_key=_DesktopAnswerKeyService(),
            answer_key_picker=lambda _request: ("answers.xlsx", "Sheet1"),
            other_response_picker=lambda _request: ("other-responses.xlsx", "Responses"),
            import_response_selection=import_other,
            session_display=session_display,
            settings_load=lambda: Ok(SettingsState(Settings("", 3, False), 1)),
            result_navigation=lambda request: result_navigation.append(
                (request.session_id, request.revision)
            ),
        ),
        write_enabled=True,
        parent=window,
    )
    qtbot.addWidget(window)
    window.show()

    window.scan_page.set_profiles(
        (
            ValidatedProfileState(
                "Tmot OMR100",
                "Tmot_OMR100.omrtemplate",
                (1682, 1190),
                "100문항 · 5지선다",
                validated=True,
                is_default=True,
            ),
        )
    )
    window.scan_page.set_source(ImportSelection(ImportKind.FOLDER, ("scan-01.png",)))
    window.scan_page.exam_name_edit.setText("통합 흐름 시험")
    assert window.scan_page.run_button.isEnabled()

    QTest.mouseClick(window.scan_page.run_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert window.nav_buttons[MainWindow.GRADING_PAGE].isEnabled()
    assert window.session_name_label.text() == "통합 흐름 시험"

    QTest.mouseClick(
        window.nav_buttons[MainWindow.GRADING_PAGE],
        Qt.MouseButton.LeftButton,
    )
    QTest.mouseClick(window.grading_page.upload_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert window.grading_page.grade_button.isEnabled(), (
        window.grading_page.error_label.text(),
        window.grading_page.validation_status_label.text(),
        window.grading_page._validation,
        window.grading_page._operation_id,
    )

    QTest.mouseClick(window.grading_page.grade_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert not window.grading_page.result_button.isHidden()
    assert window.grading_page._operation_id is not None
    QTest.mouseClick(window.grading_page.result_button, Qt.MouseButton.LeftButton)
    assert result_navigation == [(scan_sessions[0], 2)]

    QTest.mouseClick(window.grading_page.other_response_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert other_sessions == ["other-responses.xlsx"]
    assert "다른 응답 시험" in window.grading_page.session_label.text()

    controller.set_write_authority(False)
    assert not window.scan_page.run_button.isEnabled()
    assert not window.grading_page.grade_button.isEnabled()


def test_desktop_close_waits_for_slow_active_worker(qtbot):
    window = MainWindow()
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(),
        write_enabled=True,
        parent=window,
    )
    qtbot.addWidget(window)
    window.show()
    release = Event()

    def slow_operation(cancel: Event, _: object) -> object:
        cancel.wait()
        release.wait()
        return "late result"

    controller._start(window.scan_page, "slow-close", slow_operation)
    qtbot.waitUntil(
        lambda: controller._active_bridge is not None and controller._active_bridge.active
    )
    bridge = controller._active_bridge

    window.close()

    assert window.isVisible()
    release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert bridge is not None and not bridge.active


def test_desktop_repeated_active_close_requests_one_shutdown(qtbot):
    window = MainWindow()
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(),
        write_enabled=True,
        parent=window,
    )
    qtbot.addWidget(window)
    window.show()
    release = Event()
    close_requests: list[None] = []
    window.close_requested.connect(lambda: close_requests.append(None))

    def slow_operation(cancel: Event, _: object) -> object:
        cancel.wait()
        release.wait()
        return "late result"

    controller._start(window.scan_page, "repeat-close", slow_operation)
    qtbot.waitUntil(
        lambda: controller._active_bridge is not None and controller._active_bridge.active
    )
    bridge = controller._active_bridge

    window.close()
    window.close()

    assert window.isVisible()
    assert close_requests == [None]
    release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert bridge is not None and not bridge.active


def test_desktop_close_handles_terminal_race(qtbot):
    window = MainWindow()
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(),
        write_enabled=True,
        parent=window,
    )
    qtbot.addWidget(window)
    window.show()
    release = Event()

    def finishing_operation(_: Event, __: object) -> object:
        release.wait()
        return "completed"

    controller._start(window.scan_page, "terminal-race", finishing_operation)
    qtbot.waitUntil(
        lambda: controller._active_bridge is not None and controller._active_bridge.active
    )
    bridge = controller._active_bridge

    release.set()
    window.close()

    qtbot.waitUntil(lambda: not window.isVisible())
    assert bridge is not None and not bridge.active
