"""Persistent, keyboard-accessible application shell for OMR Grader."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .dashboard_page import DashboardPage
from .detail_page import DetailPage
from .grading_page import GradingPage
from .scan_page import ScanPage
from .settings_page import SettingsPage
from .theme import Theme, apply_theme


class MainWindow(QMainWindow):
    """Main shell retaining page instances while the user moves between workflows.

    `ScanPage` and `GradingPage` are expected to be QWidget subclasses constructible
    without arguments.  Integrators may instead pass preconfigured page instances.
    Workflow pages are kept in scroll areas so their complete controls remain reachable
    at the minimum window size. The controller owns write authority through
    `set_write_authority(bool)`.
    """

    page_changed = Signal(str)
    write_authority_requested = Signal(bool)
    close_requested = Signal()
    detail_navigation_requested = Signal(int)

    SCAN_PAGE = 0
    GRADING_PAGE = 1
    EXAM_PAGE = 2
    SETTINGS_PAGE = 3

    def __init__(
        self,
        scan_page: ScanPage | None = None,
        grading_page: GradingPage | None = None,
        dashboard_page: DashboardPage | None = None,
        detail_page: DetailPage | None = None,
        settings_page: SettingsPage | None = None,
        *,
        session_name: str = "진행 중인 세션이 없습니다",
        write_enabled: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = Theme.LIGHT
        self._write_enabled = write_enabled
        self._close_requires_controller = False
        self._close_requested = False
        self._close_permitted = False
        self.diagnostic_log_path: str | None = None
        self.setObjectName("mainWindow")
        self.setWindowTitle("OMR Grader")
        self.setMinimumSize(1280, 800)
        self.initial_size = QSize(1500, 1000)
        self.resize(self.initial_size)
        self.setAccessibleName("OMR Grader 메인 창")
        self.help_dialog, self.help_browser = self._create_help_dialog()

        root = QWidget(self)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.sidebar = self._create_sidebar()
        root_layout.addWidget(self.sidebar)

        content = QWidget(root)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._create_top_bar())
        self.pages = QStackedWidget(content)
        self.pages.setObjectName("contentPages")
        self.pages.setAccessibleName("작업 화면")
        self.pages.setAccessibleDescription("선택한 OMR 작업 화면을 표시합니다.")
        self.pages.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scan_page: ScanPage = scan_page if scan_page is not None else ScanPage()
        self.grading_page: GradingPage = grading_page if grading_page is not None else GradingPage()
        self.dashboard_page: DashboardPage = (
            dashboard_page if dashboard_page is not None else DashboardPage()
        )
        self.detail_page: DetailPage = detail_page if detail_page is not None else DetailPage()
        self.exam_page = QStackedWidget(content)
        self.exam_page.setObjectName("examManagementPage")
        self.exam_page.setAccessibleName("시험 관리")
        self.exam_page.addWidget(self.dashboard_page)
        self.exam_page.addWidget(self.detail_page)
        self.settings_page: SettingsPage = (
            settings_page if settings_page is not None else SettingsPage()
        )
        self.page_scroll_areas: list[QScrollArea] = []
        for page, label in (
            (self.scan_page, "OMR 스캔"),
            (self.grading_page, "정답/채점"),
            (self.exam_page, "시험 관리"),
            (self.settings_page, "환경 설정"),
        ):
            page.setAccessibleName(label)
            scroll_area = QScrollArea(content)
            scroll_area.setObjectName(f"{page.objectName() or 'workflow'}ScrollArea")
            scroll_area.setAccessibleName(f"{label} 작업 영역")
            scroll_area.setAccessibleDescription(
                f"{label} 화면의 모든 항목을 스크롤하여 사용할 수 있습니다."
            )
            scroll_area.setWidgetResizable(True)
            scroll_area.setFrameShape(QFrame.Shape.NoFrame)
            scroll_area.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            scroll_area.setWidget(page)
            self.page_scroll_areas.append(scroll_area)
            self.pages.addWidget(scroll_area)
        content_layout.addWidget(self.pages, 1)
        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)

        status_bar = QStatusBar(self)
        status_bar.setObjectName("statusBar")
        status_bar.setAccessibleName("작업 상태")
        self.status_label = QLabel(status_bar)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAccessibleName("현재 작업 상태")
        self.session_status_label = QLabel(status_bar)
        self.session_status_label.setObjectName("sessionStatusLabel")
        self.session_status_label.setAccessibleName("현재 세션 상태")
        status_bar.addWidget(self.status_label, 1)
        status_bar.addPermanentWidget(self.session_status_label)
        self.setStatusBar(status_bar)

        self.pages.currentChanged.connect(self._on_page_changed)
        self.set_current_session(session_name)
        self.set_grading_available(False)
        self.set_write_authority(write_enabled)
        self.navigate_to(self.SCAN_PAGE)
        self._set_tab_order()
        self._apply_current_theme()

    def _create_sidebar(self) -> QFrame:
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(280)
        sidebar.setAccessibleName("주요 탐색 메뉴")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 20)
        layout.setSpacing(8)

        brand = QHBoxLayout()
        mark = QLabel("◒", sidebar)
        mark.setObjectName("brandMark")
        mark.setAccessibleName("OMR Grader 로고")
        brand_text = QVBoxLayout()
        self.brand_title = QLabel("OMR Grader", sidebar)
        self.brand_title.setObjectName("brandTitle")
        self.brand_subtitle = QLabel("정확한 답안 판독과 채점", sidebar)
        self.brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(self.brand_title)
        brand_text.addWidget(self.brand_subtitle)
        brand.addWidget(mark)
        brand.addLayout(brand_text, 1)
        layout.addLayout(brand)
        layout.addSpacing(28)

        self.navigation = QButtonGroup(self)
        self.navigation.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, text, icon in (
            (self.SCAN_PAGE, "OMR 스캔", "▣"),
            (self.GRADING_PAGE, "정답/채점", "✓"),
            (self.EXAM_PAGE, "시험 관리", "▤"),
            (self.SETTINGS_PAGE, "환경 설정", "⚙"),
        ):
            button = QPushButton(f"{icon}   {text}", sidebar)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setAccessibleName(text)
            button.setAccessibleDescription(f"{text} 화면으로 이동")
            button.setProperty("pageIndex", index)
            button.clicked.connect(
                lambda checked=False, page_index=index: self.navigate_to(page_index)
            )
            self.navigation.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)

        self.session_card = QFrame(sidebar)
        self.session_card.setObjectName("sessionCard")
        self.session_card.setAccessibleName("현재 세션")
        card_layout = QVBoxLayout(self.session_card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        caption = QLabel("현재 세션", self.session_card)
        caption.setObjectName("sessionCaption")
        self.session_name_label = QLabel(self.session_card)
        self.session_name_label.setObjectName("sessionName")
        self.session_name_label.setWordWrap(True)
        self.session_name_label.setAccessibleName("현재 시험 세션")
        self.session_progress_label = QLabel("작업 대기 중", self.session_card)
        self.session_progress_label.setObjectName("sessionProgress")
        self.session_progress_label.setWordWrap(True)
        self.session_progress_label.setAccessibleName("현재 작업 진행 상태")
        card_layout.addWidget(caption)
        card_layout.addWidget(self.session_name_label)
        card_layout.addWidget(self.session_progress_label)
        layout.addWidget(self.session_card)
        return sidebar

    def _create_top_bar(self) -> QFrame:
        top_bar = QFrame(self)
        top_bar.setObjectName("topBar")
        top_bar.setFixedHeight(72)
        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(28, 0, 28, 0)
        self.page_title = QLabel("OMR 스캔", top_bar)
        self.page_title.setObjectName("pageTitle")
        self.page_title.setAccessibleName("현재 화면")
        layout.addWidget(self.page_title, 1)
        self.theme_button = QPushButton("테마", top_bar)
        self.theme_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setAccessibleName("화면 테마 전환")
        self.theme_button.clicked.connect(self.toggle_theme)
        self.help_button = QPushButton("?  도움말", top_bar)
        self.help_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.help_button.setObjectName("helpButton")
        self.help_button.setAccessibleName("도움말")
        self.help_button.clicked.connect(self.show_help)
        layout.addWidget(self.theme_button)
        layout.addWidget(self.help_button)
        return top_bar

    def _create_help_dialog(self) -> tuple[QDialog, QTextBrowser]:
        dialog = QDialog(self)
        dialog.setObjectName("helpDialog")
        dialog.setWindowTitle("OMR Grader 도움말 / 사용 설명서")
        dialog.setMinimumSize(760, 620)
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setObjectName("helpBrowser")
        browser.setOpenExternalLinks(True)
        self._help_html = """
            <h1>OMR Grader 사용 설명서</h1>
            <h2>1. OMR 스캔</h2>
            <ol>
              <li>시험명을 입력합니다.</li>
              <li><b>OMR 프로필 불러오기</b> 또는 끌어놓기로
                  <code>.omrtemplate</code> 파일을 선택합니다.</li>
              <li>프로필 정보에 <b>검증 완료</b>가 표시됐는지 확인합니다.</li>
              <li>필요하면 응시 학생 명단 엑셀을 선택합니다.</li>
              <li>스캔 이미지 폴더 또는 PDF를 선택합니다.</li>
              <li><b>OMR 인식 실행</b>을 눌러 응답 결과를 생성합니다.</li>
            </ol>
            <h2>2. 정답/채점</h2>
            <p>스캔이 완료되면 정답표 엑셀을 불러와 검증한 뒤 채점을 실행합니다.
               공란·복수 마킹 등은 확인 대상으로 표시됩니다.</p>
            <h2>3. 시험 관리</h2>
            <p>저장된 시험을 열어 학생별 응답을 확인·수정하고, 백업·복원 및
               통합 성적표 기능을 사용할 수 있습니다.</p>
            <h2>4. 환경 설정</h2>
            <p>기본 OMR 프로필, 인식 감도와 다중 처리 설정을 저장할 수 있습니다.</p>
            <h2>문제 해결 및 Q&amp;A</h2>
            <p>문의와 오류 제보:
              <a href="https://github.com/kaicot/omr-grader">
                https://github.com/kaicot/omr-grader
              </a>
            </p>
            <p>프로그램개발: 조승현(kaic21@gmail.com)</p>
            """
        browser.setHtml(self._help_html)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(browser, 1)
        layout.addWidget(buttons)
        return dialog, browser

    def _set_tab_order(self) -> None:
        widgets = [*self.nav_buttons, self.theme_button, self.help_button, self.pages]
        for current, following in zip(widgets, widgets[1:], strict=False):
            self.setTabOrder(current, following)
        for widget in widgets:
            widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Keep the primary navigation sequence deterministic across Qt platforms."""
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                widgets = [*self.nav_buttons, self.theme_button, self.help_button, self.pages]
                if isinstance(watched, QWidget) and watched in widgets:
                    backwards = event.key() == Qt.Key.Key_Backtab or bool(
                        event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                    )
                    offset = -1 if backwards else 1
                    reason = (
                        Qt.FocusReason.BacktabFocusReason
                        if backwards
                        else Qt.FocusReason.TabFocusReason
                    )
                    if watched is self.pages:
                        scroll_area = self.pages.currentWidget()
                        if isinstance(scroll_area, QScrollArea):
                            content = scroll_area.widget()
                            if isinstance(content, QWidget):
                                controls = content.findChildren(QWidget)
                                if backwards:
                                    controls.reverse()
                                for control in controls:
                                    if (
                                        control.isVisible()
                                        and control.isEnabled()
                                        and control.focusPolicy() != Qt.FocusPolicy.NoFocus
                                    ):
                                        control.setFocus(reason)
                                        return True
                    current_index = widgets.index(watched)
                    for distance in range(1, len(widgets) + 1):
                        candidate = widgets[(current_index + offset * distance) % len(widgets)]
                        if (
                            candidate.isEnabled()
                            and candidate.isVisible()
                            and candidate.focusPolicy() != Qt.FocusPolicy.NoFocus
                        ):
                            candidate.setFocus(reason)
                            return True
            if (
                isinstance(watched, QPushButton)
                and watched in [*self.nav_buttons, self.theme_button, self.help_button]
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            ):
                watched.click()
                return True
        return super().eventFilter(watched, event)

    def navigate_to(self, page_index: int) -> None:
        """Show an existing page instance; page input is deliberately never recreated."""
        if page_index < 0 or page_index >= self.pages.count():
            raise ValueError(f"Unknown page index: {page_index}")
        button = self.navigation.button(page_index)
        if button is not None and not button.isEnabled():
            self.set_status("이 작업은 OMR 스캔의 응답 결과를 만든 뒤 사용할 수 있습니다.")
            return
        if (
            self.pages.currentIndex() == self.EXAM_PAGE
            and self.exam_page.currentWidget() is self.detail_page
        ):
            self.detail_navigation_requested.emit(page_index)
            return
        if button is not None:
            button.setChecked(True)
        self.pages.setCurrentIndex(page_index)

    def set_grading_available(self, available: bool) -> None:
        """Enable grading only after the scan workflow has produced responses."""
        button = self.navigation.button(self.GRADING_PAGE)
        if button is not None:
            button.setEnabled(available)
            button.setAccessibleDescription(
                "정답/채점 화면으로 이동" if available else "OMR 스캔 완료 후 사용할 수 있음"
            )
        if not available and self.pages.currentIndex() == self.GRADING_PAGE:
            self.navigate_to(self.SCAN_PAGE)

    def set_current_session(self, session_name: str) -> None:
        """Set the persistent session context from an immutable display value."""
        if not isinstance(session_name, str):
            raise TypeError("session_name must be a string")
        display_name = session_name.strip() or "진행 중인 세션이 없습니다"
        self.session_name_label.setText(display_name)
        self.session_name_label.setToolTip(display_name)
        self.session_status_label.setText(f"세션: {display_name}")
        self.session_status_label.setToolTip(display_name)

    def set_session_progress(self, message: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        self.session_progress_label.setText(message)
        self.session_progress_label.setToolTip(message)

    def set_diagnostic_log_path(self, path: str) -> None:
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        self.diagnostic_log_path = path

    def show_diagnostic(self, message: str) -> None:
        """Keep startup diagnostics visible without preventing help or navigation."""
        self.set_status(message, role="error")

    def set_write_authority(self, enabled: bool) -> None:
        """Receive the controller-owned, value-only write-authority state."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        self._write_enabled = enabled
        self.scan_page.set_write_enabled(enabled)
        self.grading_page.set_write_enabled(enabled)
        self.dashboard_page.set_write_enabled(enabled)
        self.settings_page.set_write_enabled(enabled)
        self.detail_page.set_write_enabled(enabled)
        if enabled:
            self.set_status("시스템 준비 완료")
        else:
            self.set_status(
                "읽기 전용 모드: 저장 및 변경 작업을 사용할 수 없습니다.",
                role="error",
            )

    def request_write_authority(self, enabled: bool) -> None:
        """Request a controller-owned write-authority update using a bool value."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        self.write_authority_requested.emit(enabled)

    def set_status(self, message: str, *, role: str | None = None) -> None:
        """Expose a status area for controllers without coupling to page internals."""
        self.status_label.setText(message)
        self.status_label.setProperty("role", role or "")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def show_help(self) -> None:
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()
        self.set_status("도움말 / 사용 설명서를 열었습니다.")

    def set_close_requires_controller(self, required: bool) -> None:
        """Record whether a close must wait for an active worker to finish."""
        if type(required) is not bool:
            raise TypeError("required must be bool")
        self._close_requires_controller = required
        if not required:
            self._close_requested = False

    def cancel_close_request(self) -> None:
        """Permit a later close attempt after the controller keeps this window open."""
        self._close_requested = False

    def allow_close(self) -> None:
        """Perform the controller-authorized final close exactly once."""
        if self._close_permitted:
            return
        self._close_permitted = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._close_permitted or not self._close_requires_controller:
            event.accept()
            return
        event.ignore()
        if not self._close_requested:
            self._close_requested = True
            self.close_requested.emit()
        if self._close_permitted:
            event.accept()

    def set_theme(self, theme: Theme | str) -> None:
        self._theme = Theme(theme)
        self._apply_current_theme()

    def toggle_theme(self) -> None:
        self.set_theme(Theme.DARK if self._theme is Theme.LIGHT else Theme.LIGHT)

    @property
    def theme(self) -> Theme:
        return self._theme

    @property
    def write_enabled(self) -> bool:
        return self._write_enabled

    def show_dashboard(self) -> None:
        """Return to the persisted exam-management dashboard."""
        self.exam_page.setCurrentWidget(self.dashboard_page)

    def confirm_detail_exit(self) -> str:
        """Ask the user to save, discard, or cancel an unsaved detail edit."""
        choice = QMessageBox.warning(
            self,
            "저장되지 않은 수정사항",
            "수정사항을 저장하시겠습니까?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice is QMessageBox.StandardButton.Save:
            return "save"
        if choice is QMessageBox.StandardButton.Discard:
            return "discard"
        return "cancel"

    def show_detail(self) -> None:
        """Show the persisted detail page without changing sidebar navigation."""
        self.exam_page.setCurrentWidget(self.detail_page)

    def _apply_current_theme(self) -> None:
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_theme(application, self._theme)
        link_color = "#93C5FD" if self._theme is Theme.DARK else "#1D4ED8"
        self.help_browser.document().setDefaultStyleSheet(
            f"a {{ color: {link_color}; font-weight: 700; text-decoration: underline; }}"
        )
        self.help_browser.setHtml(self._help_html)
        self.theme_button.setText("밝은 테마" if self._theme is Theme.DARK else "어두운 테마")

    def _on_page_changed(self, page_index: int) -> None:
        button = self.navigation.button(page_index)
        if button is not None:
            button.setChecked(True)
        title = ("OMR 스캔", "정답/채점", "시험 관리", "환경 설정")[page_index]
        self.page_title.setText(title)
        self.set_status(f"현재 활성 화면: {title}")
        self.page_changed.emit(title)


__all__ = ["MainWindow"]
