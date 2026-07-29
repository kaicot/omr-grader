from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QScrollArea

from omr_grader.ui.dashboard_page import DashboardPage
from omr_grader.ui.detail_page import DetailPage
from omr_grader.ui.grading_page import GradingPage
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPage
from omr_grader.ui.settings_page import SettingsPage
from omr_grader.ui.theme import Theme, palette_for, stylesheet_for


class _ScanPage(ScanPage):
    @property
    def write_enabled(self) -> bool:
        return self._write_enabled


class _GradingPage(GradingPage):
    @property
    def write_enabled(self) -> bool:
        return self._write_enabled


def _window(qtbot) -> tuple[MainWindow, _ScanPage, _GradingPage]:
    scan = _ScanPage()
    grading = _GradingPage()
    window = MainWindow(scan, grading)
    qtbot.addWidget(window)
    window.show()
    return window, scan, grading


def test_navigation_preserves_all_workflow_pages_and_page_state(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    scan.exam_name_edit.setText("2026년 2학기 중간고사")
    window.set_grading_available(True)

    assert isinstance(window.dashboard_page, DashboardPage)
    assert isinstance(window.detail_page, DetailPage)
    assert isinstance(window.settings_page, SettingsPage)
    expected_pages = (
        window.scan_page,
        window.grading_page,
        window.exam_page,
        window.settings_page,
    )
    for index, page in enumerate(expected_pages):
        qtbot.mouseClick(window.nav_buttons[index], Qt.MouseButton.LeftButton)
        assert window.pages.currentIndex() == index
        scroll_area = window.pages.currentWidget()
        assert isinstance(scroll_area, QScrollArea)
        assert scroll_area is window.page_scroll_areas[index]
        assert scroll_area.widget() is page

    qtbot.mouseClick(window.nav_buttons[MainWindow.SCAN_PAGE], Qt.MouseButton.LeftButton)
    assert scan.exam_name_edit.text() == "2026년 2학기 중간고사"
    assert window.page_scroll_areas[MainWindow.GRADING_PAGE].widget() is grading


def test_grading_navigation_requires_scan_result_then_activates(qtbot) -> None:
    window, _, grading = _window(qtbot)
    grading_button = window.navigation.button(MainWindow.GRADING_PAGE)

    assert not grading_button.isEnabled()
    qtbot.mouseClick(grading_button, Qt.MouseButton.LeftButton)
    assert window.pages.currentIndex() == MainWindow.SCAN_PAGE

    window.set_grading_available(True)
    grading_button.setFocus()
    qtbot.keyClick(grading_button, Qt.Key.Key_Space)
    assert window.pages.currentIndex() == MainWindow.GRADING_PAGE
    assert window.pages.currentWidget() is window.page_scroll_areas[MainWindow.GRADING_PAGE]
    assert window.pages.currentWidget().widget() is grading


def test_shell_accessibility_status_and_write_authority_contract(qtbot) -> None:
    window, scan, grading = _window(qtbot)

    assert window.minimumWidth() >= 1280
    assert window.minimumHeight() >= 800
    assert window.sidebar.width() == 280
    assert window.help_button.accessibleName() == "도움말"
    assert all(button.accessibleName() for button in window.nav_buttons)
    stylesheet = stylesheet_for(Theme.LIGHT)
    assert "QLabel#brandTitle" in stylesheet
    assert "background: transparent" in stylesheet
    assert "QFrame#scanExamCard" in stylesheet
    assert "QFrame#rosterImportWidget" in stylesheet
    assert "QPushButton#navButton:focus" in stylesheet

    window.set_current_session("2026년 2학기 중간고사")
    assert window.session_name_label.text() == "2026년 2학기 중간고사"
    assert window.session_status_label.text() == "세션: 2026년 2학기 중간고사"

    requested: list[bool] = []
    window.write_authority_requested.connect(requested.append)
    window.request_write_authority(False)
    assert requested == [False]
    assert window.write_enabled

    window.set_write_authority(False)
    assert not window.write_enabled
    assert not scan.write_enabled
    assert not grading.write_enabled
    assert not window.dashboard_page.detail_button.isEnabled()
    assert not window.settings_page.save_button.isEnabled()
    assert window.help_button.isEnabled()
    assert "읽기 전용" in window.status_label.text()


def test_minimum_size_keeps_scan_and_grading_workflows_scrollable(qtbot) -> None:
    window, scan, grading = _window(qtbot)
    scan.setMinimumHeight(1500)
    grading.setMinimumHeight(1500)
    window.resize(1280, 800)
    qtbot.waitUntil(lambda: window.isVisible())

    for index in (MainWindow.SCAN_PAGE, MainWindow.GRADING_PAGE):
        if index == MainWindow.GRADING_PAGE:
            window.set_grading_available(True)
        qtbot.mouseClick(window.nav_buttons[index], Qt.MouseButton.LeftButton)
        scroll_area = window.page_scroll_areas[index]
        assert scroll_area.widgetResizable()
        qtbot.waitUntil(
            lambda scroll_area=scroll_area: scroll_area.verticalScrollBar().maximum() > 0
        )


def test_theme_help_and_sidebar_tab_keyboard_activation(qtbot) -> None:
    window, _, _ = _window(qtbot)
    window.set_grading_available(True)

    light_palette = palette_for(Theme.LIGHT)
    dark_palette = palette_for(Theme.DARK)
    assert light_palette.color(QPalette.ColorRole.Window) == QColor("#F4F7FB")
    assert dark_palette.color(QPalette.ColorRole.Window) == QColor("#17212B")
    assert light_palette.color(QPalette.ColorRole.WindowText) == QColor("#172B4D")
    assert dark_palette.color(QPalette.ColorRole.WindowText) == QColor("#F1F5F9")

    assert window.theme_button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert window.help_button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    window.nav_buttons[MainWindow.SCAN_PAGE].setFocus()
    qtbot.waitUntil(window.nav_buttons[MainWindow.SCAN_PAGE].hasFocus)
    for page_index, key in (
        (MainWindow.GRADING_PAGE, Qt.Key.Key_Space),
        (MainWindow.EXAM_PAGE, Qt.Key.Key_Return),
        (MainWindow.SETTINGS_PAGE, Qt.Key.Key_Space),
    ):
        qtbot.keyClick(window.focusWidget(), Qt.Key.Key_Tab)
        button = window.nav_buttons[page_index]
        qtbot.waitUntil(button.hasFocus)
        qtbot.keyClick(button, key)
        assert window.pages.currentIndex() == page_index

    qtbot.keyClick(window.nav_buttons[MainWindow.SETTINGS_PAGE], Qt.Key.Key_Tab)
    qtbot.waitUntil(window.theme_button.hasFocus)
    qtbot.keyClick(window.theme_button, Qt.Key.Key_Space)
    assert window.theme is Theme.DARK
    assert window.theme_button.text() == "밝은 테마"

    qtbot.keyClick(window.theme_button, Qt.Key.Key_Tab)
    qtbot.waitUntil(window.help_button.hasFocus)
    qtbot.keyClick(window.help_button, Qt.Key.Key_Return)
    assert "도움말" in window.status_label.text()


def test_primary_tab_order_skips_disabled_navigation_in_both_directions(qtbot) -> None:
    window, _, _ = _window(qtbot)
    scan_button = window.nav_buttons[MainWindow.SCAN_PAGE]
    exam_button = window.nav_buttons[MainWindow.EXAM_PAGE]

    assert not window.nav_buttons[MainWindow.GRADING_PAGE].isEnabled()
    scan_button.setFocus()
    qtbot.waitUntil(scan_button.hasFocus)

    qtbot.keyClick(scan_button, Qt.Key.Key_Tab)
    qtbot.waitUntil(exam_button.hasFocus)

    qtbot.keyClick(
        exam_button,
        Qt.Key.Key_Backtab,
        modifier=Qt.KeyboardModifier.ShiftModifier,
    )
    qtbot.waitUntil(scan_button.hasFocus)


def test_detail_sidebar_navigation_is_deferred_to_controller(qtbot) -> None:
    window, _, _ = _window(qtbot)
    window.navigate_to(MainWindow.EXAM_PAGE)
    window.show_detail()
    requested: list[int] = []
    window.detail_navigation_requested.connect(requested.append)

    window.navigate_to(MainWindow.SETTINGS_PAGE)

    assert requested == [MainWindow.SETTINGS_PAGE]
    assert window.pages.currentIndex() == MainWindow.EXAM_PAGE
    assert window.exam_page.currentWidget() is window.detail_page
