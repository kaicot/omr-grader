import re

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from omr_grader.ui import scan_page as scan_page_module
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPage, ScanPageRequest, ValidatedProfileState
from omr_grader.ui.theme import Theme, stylesheet_for, tokens_for

PDF = ImportSelection(ImportKind.PDF, ("C:/input/scans.pdf",))


class _Clock:
    """A monotonic clock the test advances by hand instead of sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(scan_page_module, "monotonic", clock)
    return clock


@pytest.fixture(
    params=(None, (960, 540), (1280, 720), (1366, 768), (1536, 864), (1920, 1080)),
    ids=("current-screen", "960x540", "1280x720", "1366x768", "1536x864", "1920x1080"),
)
def available_work_area(request, monkeypatch, qapp):
    """Emulate logical work areas while retaining Qt's real frames and layouts."""
    if request.param is None:
        return qapp.primaryScreen().availableGeometry()
    available = QRect(0, 0, *request.param)
    monkeypatch.setattr(
        MainWindow, "_available_geometry_for_current_screen", lambda self: QRect(available)
    )
    return available


def _tab_to(qtbot, widget):
    for _ in range(20):
        focused = QApplication.focusWidget()
        if focused is widget:
            return
        assert focused is not None
        qtbot.keyClick(focused, Qt.Key.Key_Tab)
        QApplication.processEvents()
    assert QApplication.focusWidget() is widget


def _fully_in_viewport(widget, viewport):
    return viewport.rect().contains(QRect(widget.mapTo(viewport, QPoint(0, 0)), widget.size()))


def _profile(*, validated=True, errors=(), is_default=False, duplicate_outcome=None):
    return ValidatedProfileState(
        name="기본 100문항",
        path="C:/Profiles/basic.omrtemplate",
        dimensions=(1682, 1190),
        grid_summary="학번 8 × 10 · 답안 영역 5개 · 총 100문항",
        validation_errors=errors,
        is_default=is_default,
        duplicate_outcome=duplicate_outcome,
        validated=validated,
    )


def _drop(widget, paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDropEvent(
        QPointF(4, 4),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.dropEvent(event)


def _ready_page(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.set_profiles((_profile(is_default=True),))
    page.exam_name_edit.setText("26-2 생리학 중간고사")
    page.set_source(ImportSelection(ImportKind.PDF, ("C:/input/scans.pdf",)))
    return page


def test_run_is_gated_until_validated_profile_and_required_inputs_are_ready(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)

    page.exam_name_edit.setText("시험")
    page.set_source(ImportSelection(ImportKind.FOLDER, ("C:/input",)))
    page.set_profiles((_profile(validated=False, errors=("학번 영역이 없습니다.",)),))
    page.profile_combo.setCurrentIndex(1)

    assert not page.run_button.isEnabled()
    assert "검증 실패" in page.profile_summary.text()

    page.set_profiles((_profile(is_default=True, duplicate_outcome="다른 이름으로 저장"),))

    assert page.run_button.isEnabled()
    assert "검증 완료" in page.profile_summary.text()
    assert "기준 크기 1682 × 1190" in page.profile_summary.toolTip()
    assert "총 100문항" in page.profile_summary.toolTip()
    assert "기본 프로필" in page.profile_summary.toolTip()
    assert "중복 처리: 다른 이름으로 저장" in page.profile_summary.toolTip()


def test_run_emits_immutable_validated_profile_request(qtbot):
    page = _ready_page(qtbot)
    page.set_roster("C:/input/roster.xlsx", count=2)

    with qtbot.waitSignal(page.recognition_requested) as signal:
        qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)

    request = signal.args[0]
    assert isinstance(request, ScanPageRequest)
    assert request.exam_name == "26-2 생리학 중간고사"
    assert request.profile == _profile(is_default=True)
    assert request.profile_path == "C:/Profiles/basic.omrtemplate"
    assert request.source.paths == ("C:/input/scans.pdf",)
    assert request.sensitivity == 5
    assert request.roster_path == "C:/input/roster.xlsx"


def test_scan_primary_buttons_scroll_into_view_and_activate_by_keyboard(qtbot, available_work_area):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.nav_buttons[window.SCAN_PAGE].setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()

    page = window.scan_page
    button = page.fresh_response_button
    scroll_area = window.page_scroll_areas[window.SCAN_PAGE]
    viewport = scroll_area.viewport()

    assert available_work_area.contains(window.frameGeometry())
    assert window.pages.currentIndex() == window.SCAN_PAGE
    assert button.isVisible()
    assert button.text() == "응답 엑셀로 시작"
    assert button.accessibleName() == "응답 엑셀로 새 세션 시작"
    assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert button.isEnabled()

    # A control may start outside the viewport; keyboard focus must reveal it
    # before activation, including on the default 800px offscreen test display.
    _tab_to(qtbot, button)
    assert _fully_in_viewport(button, viewport)
    fresh_requests = QSignalSpy(page.fresh_response_requested)
    qtbot.keyClick(button, Qt.Key.Key_Space)
    assert fresh_requests.count() == 1
    qtbot.keyClick(button, Qt.Key.Key_Return)
    assert fresh_requests.count() == 2

    page.set_profiles((_profile(is_default=True),))
    page.exam_name_edit.setText("26-2 생리학 중간고사")
    page.set_source(ImportSelection(ImportKind.PDF, ("C:/input/scans.pdf",)))
    QApplication.processEvents()
    scroll_area.horizontalScrollBar().setValue(0)
    # The input sections scroll on their own; focus brings the last control into that view.
    sections = page.sections_scroll_area
    _tab_to(qtbot, page.sensitivity_slider)
    assert _fully_in_viewport(page.sensitivity_slider, sections.viewport())
    # The run button sits outside them, so it is on screen without any scrolling.
    _tab_to(qtbot, page.run_button)
    assert page.run_button.isEnabled()
    assert _fully_in_viewport(page.run_button, viewport)
    assert scroll_area.horizontalScrollBar().value() == 0
    recognition_requests = QSignalSpy(page.recognition_requested)
    qtbot.keyClick(page.run_button, Qt.Key.Key_Space)
    assert recognition_requests.count() == 1
    assert recognition_requests.at(0)[0].exam_name == "26-2 생리학 중간고사"

    page.set_busy(True, "import-1", cancellable=False)
    assert not page.fresh_response_button.isEnabled()
    assert not page.cancel_button.isEnabled()
    page.set_busy(False)
    page.set_write_enabled(False)
    assert not page.fresh_response_button.isEnabled()


def test_initial_frame_fits_work_area_and_scan_footer_remains_scrollable(qtbot, available_work_area):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    QApplication.processEvents()

    assert window.size() == window.initial_size
    assert available_work_area.contains(window.frameGeometry())
    assert available_work_area.contains(window.windowHandle().frameGeometry())
    # Verify that the simulated work areas were really used instead of all
    # cases silently running at the offscreen plugin's smaller default size.
    if available_work_area.width() < MainWindow.PREFERRED_INITIAL_SIZE.width():
        assert window.frameGeometry().width() == available_work_area.width()
    if available_work_area.height() < MainWindow.PREFERRED_INITIAL_SIZE.height():
        assert window.frameGeometry().height() == available_work_area.height()
    page = window.scan_page
    scroll_area = window.page_scroll_areas[window.SCAN_PAGE]
    viewport = scroll_area.viewport()
    scrollbar = scroll_area.verticalScrollBar()
    assert scroll_area.widgetResizable()
    if page.height() > viewport.height():
        assert scrollbar.isVisible()
        assert scrollbar.maximum() > 0
        scrollbar.setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.keyClick(scrollbar, Qt.Key.Key_End)
        QApplication.processEvents()
        assert scrollbar.value() == scrollbar.maximum()
    else:
        assert scrollbar.maximum() == 0
    footer_bottom = page.session_footer.mapTo(viewport, page.session_footer.rect().bottomLeft())
    assert 0 <= footer_bottom.y() <= viewport.rect().bottom()
    # The action row never scrolls away, even where the sections no longer fit.
    assert _fully_in_viewport(page.run_button, viewport)
    assert _fully_in_viewport(page.cancel_button, viewport)


def test_sections_scroll_on_their_own_below_the_header_and_above_the_actions(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.show()
    QApplication.processEvents()

    sections = page.sections_scroll_area
    assert sections.widgetResizable()
    assert sections.frameShape() == QFrame.Shape.NoFrame
    assert sections.focusPolicy() == Qt.FocusPolicy.NoFocus
    content = sections.widget()
    for card in page.findChildren(QFrame):
        if card.objectName().startswith("scan") and card.objectName().endswith("Card"):
            assert content.isAncestorOf(card)
    for outside in (
        page.fresh_response_button,
        page.reset_button,
        page.progress_label,
        page.run_hint_label,
        page.cancel_button,
        page.run_button,
        page.session_footer,
    ):
        assert not content.isAncestorOf(outside)

    def top(widget):
        return widget.mapTo(page, QPoint(0, 0)).y()

    sections_top, sections_bottom = top(sections), top(sections) + sections.height()
    assert top(page.fresh_response_button) < sections_top
    assert top(page.reset_button) < sections_top
    assert sections_bottom <= top(page.progress_label) < top(page.run_button)
    assert top(page.run_button) < top(page.session_footer)


def test_a_short_window_scrolls_the_sections_but_keeps_the_actions_visible(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.resize(900, 480)
    page.show()
    QApplication.processEvents()

    sections = page.sections_scroll_area
    assert sections.verticalScrollBar().maximum() > 0
    assert page.rect().contains(QRect(page.run_button.pos(), page.run_button.size()))
    assert page.rect().contains(QRect(page.session_footer.pos(), page.session_footer.size()))
    # A control far down the sections is reachable by scrolling them, not the page.
    sections.ensureWidgetVisible(page.sensitivity_slider)
    assert sections.verticalScrollBar().value() > 0
    slider_origin = page.sensitivity_slider.mapTo(sections.viewport(), QPoint(0, 0))
    assert sections.viewport().rect().contains(slider_origin)


def test_run_and_cancel_actions_are_in_the_bottom_row_beside_the_readiness_hint(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.resize(1200, 900)
    page.show()
    QApplication.processEvents()

    def top(widget):
        return widget.mapTo(page, QPoint(0, 0)).y()

    sections_bottom = top(page.sections_scroll_area) + page.sections_scroll_area.height()
    for button in (page.run_button, page.cancel_button):
        assert top(button) >= sections_bottom
    assert top(page.run_hint_label) >= sections_bottom
    assert page.run_hint_label.x() < page.cancel_button.x() < page.run_button.x()
    assert page.run_hint_label.objectName() == "scanRunHint"
    assert page.run_button.text() == "OMR 인식 실행"
    assert page.cancel_button.text() == "인식 취소"
    assert page.fresh_response_button.text() == "응답 엑셀로 시작"
    assert page.reset_button.text() == "초기화 / 재설정"
    assert not hasattr(page, "help_button")


def test_the_five_sections_keep_the_input_order_and_numbered_titles(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.resize(1200, 1600)
    page.show()
    QApplication.processEvents()

    sections = (
        ("scanExamCard", "1. 시험명 *"),
        ("scanRosterCard", "2. 응시 학생 명단 (선택)"),
        ("scanSourceCard", "3. 스캔 파일/폴더 선택 *"),
        ("scanProfileCard", "4. 인식 프로필 (스캔을 고르면 자동 지정)"),
        ("scanSensitivityCard", "5. 고급 인식 설정"),
    )
    cards = [page.findChild(QFrame, name) for name, _ in sections]
    assert all(card is not None for card in cards)
    tops = [card.mapTo(page, QPoint(0, 0)).y() for card in cards]
    assert tops == sorted(tops)
    assert len(set(tops)) == len(tops)
    for (_, title), card in zip(sections, cards, strict=True):
        assert [label.text() for label in card.findChildren(QLabel, "scanSectionLabel")] == [title]
    exam, roster, source, profile, settings = cards
    assert exam.isAncestorOf(page.exam_name_edit)
    assert roster.isAncestorOf(page.roster_widget)
    assert roster.isAncestorOf(page.sample_roster_button)
    assert source.isAncestorOf(page.source_widget)
    assert source.isAncestorOf(page.source_folder_button)
    assert source.isAncestorOf(page.source_pdf_button)
    for owned in (
        page.form_status_label,
        page.form_progress_bar,
        page.profile_combo,
        page.profile_import_button,
        page.profile_summary,
    ):
        assert profile.isAncestorOf(owned)
    assert settings.isAncestorOf(page.sensitivity_slider)


def test_profile_card_replaces_the_profile_drop_zone_and_info_row(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.resize(1200, 1600)
    page.show()
    QApplication.processEvents()

    texts = {label.text() for label in page.findChildren(QLabel)}
    assert "프로필 끌어놓기" not in texts
    assert "프로필 정보" not in texts
    assert page.profile_import_button.text() == "다른 프로필 불러오기"
    assert page.profile_import_button.objectName() == "profileImportButton"
    assert page.profile_widget.kind is ImportKind.PROFILE
    assert page.profile_widget.isHidden()
    assert page.profile_combo.mapTo(page, QPoint(0, 0)).y() < (
        page.profile_summary.mapTo(page, QPoint(0, 0)).y()
    )


def test_sample_roster_button_is_grouped_with_roster_heading(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.resize(1200, 900)
    page.show()
    QApplication.processEvents()

    button_top = page.sample_roster_button.mapTo(page, QPoint(0, 0)).y()
    roster_top = page.roster_widget.mapTo(page, QPoint(0, 0)).y()
    assert button_top < roster_top


def test_source_folder_pdf_and_roster_drops_enforce_mode_and_extensions(qtbot):
    page = _ready_page(qtbot)
    rejected = []
    page.source_widget.rejected.connect(rejected.append)
    page.roster_widget.rejected.connect(rejected.append)
    page.source_widget.clear()
    assert "이미지(JPG, PNG) 폴더나 PDF 파일" in page.source_widget._detail.text()

    _drop(page.source_widget, ["C:/input/scans"])
    assert page.source_widget.selection == ImportSelection(ImportKind.FOLDER, ("C:/input/scans",))
    assert page.run_button.isEnabled()

    _drop(page.source_widget, ["C:/input/scans.PDF"])
    assert page.source_widget.selection == ImportSelection(ImportKind.PDF, ("C:/input/scans.PDF",))
    assert not hasattr(page, "folder_radio")
    assert not hasattr(page, "pdf_radio")

    _drop(page.roster_widget, ["C:/input/roster.XLSM"])
    assert page.roster_widget.selection == ImportSelection(
        ImportKind.ROSTER, ("C:/input/roster.XLSM",)
    )
    _drop(page.roster_widget, ["C:/input/roster.pdf"])
    assert page.roster_widget.selection == ImportSelection(
        ImportKind.ROSTER, ("C:/input/roster.XLSM",)
    )
    assert rejected == ["명단 엑셀 파일(.xlsx 또는 .xlsm)만 선택할 수 있습니다."]


def test_unified_source_buttons_request_picker_and_cancel_preserves_selection(qtbot):
    page = _ready_page(qtbot)

    with qtbot.waitSignal(page.source_browse_requested) as folder:
        qtbot.mouseClick(page.source_folder_button, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(page.source_browse_requested) as pdf:
        qtbot.mouseClick(page.source_pdf_button, Qt.MouseButton.LeftButton)

    assert folder.args == [ImportKind.FOLDER]
    assert pdf.args == [ImportKind.PDF]
    assert page.source_widget.kind is ImportKind.SOURCE
    prior = page.source_widget.selection
    page.set_source_picker_cancelled()

    assert page.source_widget.selection == prior
    assert page.source_widget.property("pickerState") == "cancelled"
    assert page.run_button.isEnabled()


def test_profile_browse_keyboard_and_drop_emit_explicit_requests(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)

    with qtbot.waitSignal(page.profile_browse_requested):
        page.profile_import_button.setFocus()
        qtbot.keyClick(page.profile_import_button, Qt.Key.Key_Space)

    with qtbot.waitSignal(page.profile_drop_requested) as dropped:
        with qtbot.waitSignal(page.profile_import_requested) as imported:
            _drop(page.profile_widget, ["C:/outside/template.omrtemplate"])

    expected = ImportSelection(ImportKind.PROFILE, ("C:/outside/template.omrtemplate",))
    assert dropped.args == [expected]
    assert imported.args == [expected]


def _drag_enter(widget, paths) -> bool:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDragEnterEvent(
        QPoint(4, 4),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.dragEnterEvent(event)
    return event.isAccepted()


def test_profile_file_dropped_on_the_profile_card_is_imported_once(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    card = page.findChild(QFrame, "scanProfileCard")
    dropped = QSignalSpy(page.profile_drop_requested)
    imported = QSignalSpy(page.profile_import_requested)

    assert card.acceptDrops()
    _drop(card, ["C:/outside/template.omrtemplate"])

    expected = ImportSelection(ImportKind.PROFILE, ("C:/outside/template.omrtemplate",))
    assert dropped.count() == 1
    assert dropped.at(0)[0] == expected
    assert imported.count() == 1
    assert imported.at(0)[0] == expected
    assert page.profile_widget.selection == expected


def test_other_files_dropped_on_the_profile_card_are_refused(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    card = page.findChild(QFrame, "scanProfileCard")
    imported = QSignalSpy(page.profile_import_requested)
    refusals = []
    page.profile_widget.rejected.connect(refusals.append)

    _drop(card, ["C:/input/roster.xlsx"])
    _drop(card, ["C:/outside/a.omrtemplate", "C:/outside/b.omrtemplate"])

    assert imported.count() == 0
    assert refusals == ["OMR 프로필 파일(.omrtemplate)만 선택할 수 있습니다."] * 2


def test_dragging_a_profile_over_the_card_highlights_only_acceptable_drags(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    card = page.findChild(QFrame, "scanProfileCard")
    assert card.property("dragActive") is False

    assert not _drag_enter(card, ["C:/input/scan.png"])
    assert card.property("dragActive") is False
    assert _drag_enter(card, ["C:/outside/template.omrtemplate"])
    assert card.property("dragActive") is True
    card.dragLeaveEvent(QDragLeaveEvent())
    assert card.property("dragActive") is False


def test_profile_card_ignores_drops_while_busy_or_importing(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    card = page.findChild(QFrame, "scanProfileCard")
    imported = QSignalSpy(page.profile_import_requested)

    page.set_busy(True, "operation-1")
    assert not _drag_enter(card, ["C:/outside/template.omrtemplate"])
    _drop(card, ["C:/outside/template.omrtemplate"])
    page.set_busy(False)
    page.set_profile_importing("C:/outside/other.omrtemplate")
    _drop(card, ["C:/outside/template.omrtemplate"])

    assert imported.count() == 0


def test_profile_picker_cancellation_is_still_recorded(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)

    page.set_profile_picker_cancelled()

    assert page.profile_widget.property("pickerState") == "cancelled"


def test_run_hint_names_the_first_unmet_condition_and_matches_the_run_gate(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    hint = page.run_hint_label
    imported = _profile(is_default=True)

    def shows(text: str, *, runnable: bool = False, role: str = "hint") -> None:
        assert hint.text() == text
        assert hint.property("role") == role
        assert page.run_button.isEnabled() is runnable

    shows("시험명과 스캔을 고르면 실행할 수 있습니다.")
    page.exam_name_edit.setText("시험")
    shows("스캔 파일이나 폴더를 고르면 실행할 수 있습니다.")
    page.exam_name_edit.setText("   ")
    shows("시험명과 스캔을 고르면 실행할 수 있습니다.")
    page.set_source(PDF)
    shows("시험명을 입력하면 실행할 수 있습니다.")
    page.exam_name_edit.setText("시험")
    shows("인식 프로필을 고르면 실행할 수 있습니다.")
    page.set_profiles((_profile(validated=False, errors=("학번 영역이 없습니다.",)),))
    page.profile_combo.setCurrentIndex(1)
    shows("인식 프로필을 고르면 실행할 수 있습니다.")
    page.set_form_detecting()
    shows("인식 프로필을 확인하는 중입니다.")
    page.set_form_detected("자동 인식")
    page.set_profile_importing("C:/outside/basic.omrtemplate")
    shows("프로필을 불러오는 중입니다.")
    page.set_profiles((imported,))
    assert page.select_profile(imported.path)
    shows("준비되었습니다. 'OMR 인식 실행'을 누르세요.", runnable=True, role="success")

    page.set_busy(True, "operation-1")
    shows("")
    page.set_busy(False)
    shows("준비되었습니다. 'OMR 인식 실행'을 누르세요.", runnable=True, role="success")
    page.set_write_enabled(False)
    shows("실행 폴더에 쓸 수 없어 인식을 시작할 수 없습니다.", role="error")
    page.set_busy(True, "operation-2")
    shows("실행 폴더에 쓸 수 없어 인식을 시작할 수 없습니다.", role="error")
    page.set_busy(False)
    page.set_write_enabled(True)
    shows("준비되었습니다. 'OMR 인식 실행'을 누르세요.", runnable=True, role="success")


def test_run_hint_returns_to_the_first_condition_after_reset_and_cleared_source(qtbot):
    page = _ready_page(qtbot)
    assert page.run_hint_label.text() == "준비되었습니다. 'OMR 인식 실행'을 누르세요."

    page.set_source(None)
    assert page.run_hint_label.text() == "스캔 파일이나 폴더를 고르면 실행할 수 있습니다."
    page.set_source(PDF)
    page.reset_button.click()

    assert page.run_hint_label.text() == "시험명과 스캔을 고르면 실행할 수 있습니다."


def test_imported_profile_is_selected_with_visible_confirmation(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    imported = _profile()

    page.set_profile_importing("C:/outside/basic.omrtemplate")
    assert "불러오는 중" in page.profile_summary.text()

    page.set_profiles((imported,))
    assert page.select_profile(imported.path)

    assert page.profile_combo.currentData() == imported
    assert "검증 완료" in page.profile_summary.text()
    assert "인식 프로필로 지정했습니다" in page.progress_label.text()


def test_busy_locks_mutation_and_cancel_cleanup_reenables_inputs(qtbot):
    page = _ready_page(qtbot)
    page.set_roster("C:/input/roster.xlsx", count=2)
    source = page.source_widget.selection
    roster = page.roster_widget.selection
    page.set_busy(True, "operation-1")

    assert page.cancel_button.isEnabled()
    assert not page.run_button.isEnabled()
    assert not page.exam_name_edit.isEnabled()
    assert not page.profile_widget.isEnabled()
    with qtbot.waitSignal(page.cancel_requested) as signal:
        qtbot.mouseClick(page.cancel_button, Qt.MouseButton.LeftButton)
    assert signal.args == ["operation-1"]

    page.set_cancelled()

    assert not page.cancel_button.isEnabled()
    assert page.exam_name_edit.isEnabled()
    assert page.source_widget.isEnabled()
    assert page.progress_label.text() == "OMR 인식이 취소되었습니다."
    assert not page.progress_bar.isVisible()
    assert page.roster_widget.isEnabled()
    assert page.sensitivity_slider.isEnabled()
    assert page.source_widget.selection == source
    assert page.roster_widget.selection == roster


def test_busy_shows_an_indeterminate_bar_and_a_clock_that_ticks_every_second(qtbot, clock):
    page = _ready_page(qtbot)
    assert not page._progress_timer.isActive()

    page.set_busy(True, "operation-1")

    assert not page.progress_bar.isHidden()
    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 0)
    assert page.progress_label.text() == "OMR 인식을 준비하고 있습니다. 취소할 수 있습니다."
    assert page._progress_timer.isActive()
    assert page._progress_timer.interval() == 1000
    clock.advance(7)
    # The timer's own timeout drives the redraw between worker events.
    page._progress_timer.timeout.emit()
    assert page.progress_label.text() == (
        "OMR 인식을 준비하고 있습니다. 취소할 수 있습니다. (경과 7초)"
    )

    page.set_busy(True, "operation-2", cancellable=False)
    assert page.progress_label.text() == "응답 결과를 안전하게 가져오고 있습니다. (경과 7초)"
    page.set_busy(False)
    assert page.progress_bar.isHidden()
    assert not page._progress_timer.isActive()


def test_prepare_phase_shows_pages_prepared_without_a_remaining_time(qtbot, clock):
    page = _ready_page(qtbot)
    page.set_busy(True, "operation-1")
    clock.advance(12)

    page.set_progress(3, 12, elapsed_seconds=999, eta_seconds=40, phase="prepare")

    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 12)
    assert page.progress_bar.value() == 3
    assert page.progress_label.text() == "스캔 파일을 준비하는 중 (3 / 12쪽) · 경과 12초"


def test_recognize_phase_shows_counts_elapsed_remaining_and_outcomes(qtbot, clock):
    page = _ready_page(qtbot)
    page.set_busy(True, "operation-1")
    clock.advance(65)

    page.set_progress(4, 10, failed=1, elapsed_seconds=1, eta_seconds=125, phase="recognize")

    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 10)
    assert page.progress_bar.value() == 5
    assert page.progress_bar.format() == "%v / %m (%p%)"
    assert page.progress_label.text() == (
        "답안지를 판독하는 중 (5 / 10쪽) · 경과 1분 5초 · 남은 시간 약 2분 5초"
        " (성공 4, 확인 필요 1)"
    )
    assert page.cancel_button.isEnabled()


def test_recognize_is_the_default_phase(qtbot, clock):
    page = _ready_page(qtbot)

    page.set_progress(1, 2)

    assert page.progress_label.text().startswith("답안지를 판독하는 중 (1 / 2쪽)")


@pytest.mark.parametrize(
    ("completed", "total", "eta", "remaining_shown"),
    ((0, 10, 30, False), (3, 10, None, False), (3, 10, 30, True), (10, 10, 30, False)),
)
def test_remaining_time_needs_an_estimate_and_work_left(
    qtbot, clock, completed, total, eta, remaining_shown
):
    page = _ready_page(qtbot)

    page.set_progress(completed, total, eta_seconds=eta)

    text = page.progress_label.text()
    assert ("남은 시간" in text) is remaining_shown
    assert ("성공" in text) is (completed > 0)
    assert "계산 중" not in text


def test_recognize_remaining_time_counts_down_between_events_and_then_goes(qtbot, clock):
    page = _ready_page(qtbot)
    page.set_progress(2, 10, eta_seconds=30)
    assert "남은 시간 약 30초" in page.progress_label.text()

    clock.advance(10)
    page._refresh_progress()
    assert "경과 10초 · 남은 시간 약 20초" in page.progress_label.text()

    clock.advance(19)
    page._refresh_progress()
    assert "경과 29초 · 남은 시간 약 1초" in page.progress_label.text()

    clock.advance(1)
    page._refresh_progress()
    assert "경과 30초" in page.progress_label.text()
    assert "남은 시간" not in page.progress_label.text()

    # The next worker event brings a fresh estimate.
    page.set_progress(3, 10, eta_seconds=8)
    assert "경과 30초 · 남은 시간 약 8초" in page.progress_label.text()


def test_save_phase_is_indeterminate_and_keeps_the_clock_running(qtbot, clock):
    page = _ready_page(qtbot)
    page.set_progress(10, 10, phase="recognize")
    assert page.progress_bar.maximum() == 10
    clock.advance(3)

    page.set_progress(10, 10, phase="save")

    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 0)
    assert page.progress_label.text() == "인식 결과를 저장하는 중… · 경과 3초"
    clock.advance(1)
    page._progress_timer.timeout.emit()
    assert page.progress_label.text() == "인식 결과를 저장하는 중… · 경과 4초"
    assert page._progress_timer.isActive()


def test_saving_results_cannot_be_cancelled(qtbot, clock):
    page = _ready_page(qtbot)
    page.set_busy(True, "operation")
    page.set_progress(5, 10, phase="recognize")
    assert page.cancel_button.isEnabled()

    page.set_progress(10, 10, phase="save")

    assert not page.cancel_button.isEnabled()
    page.set_cancelled()
    page.set_busy(True, "next")
    assert page.cancel_button.isEnabled()


def test_elapsed_time_comes_from_the_pages_own_clock(qtbot, clock):
    page = _ready_page(qtbot)

    # Without set_busy() the first progress call starts the clock; the argument is ignored.
    page.set_progress(1, 10, elapsed_seconds=999)
    assert "경과 0초" in page.progress_label.text()
    assert page._progress_timer.isActive()

    clock.advance(5)
    page._refresh_progress()
    assert "경과 5초" in page.progress_label.text()
    clock.advance(60)
    page.set_progress(2, 10, elapsed_seconds=1)
    assert "경과 1분 5초" in page.progress_label.text()


@pytest.mark.parametrize(
    ("seconds", "text"),
    (
        (0, "0초"),
        (-4, "0초"),
        (59.9, "59초"),
        (60, "1분 0초"),
        (125, "2분 5초"),
        (3599, "59분 59초"),
        (3600, "1시간 0분"),
        (7384, "2시간 3분"),
    ),
)
def test_durations_read_as_seconds_minutes_or_hours(seconds, text):
    assert ScanPage._format_duration(seconds) == text


@pytest.mark.parametrize(
    "arguments",
    (
        {"completed": -1, "total": 5},
        {"completed": 1, "total": -5},
        {"completed": 3, "total": 5, "failed": 3},
        {"completed": 1, "total": 5, "elapsed_seconds": -1},
        {"completed": 1, "total": 5, "eta_seconds": True},
        {"completed": 1, "total": 5, "phase": "finish"},
        {"completed": 1, "total": 5, "phase": None},
    ),
)
def test_progress_rejects_invalid_values_without_changing_the_page(qtbot, clock, arguments):
    page = _ready_page(qtbot)
    before = page.progress_label.text()

    with pytest.raises(ValueError):
        page.set_progress(**arguments)

    assert page.progress_label.text() == before
    assert not page._progress_timer.isActive()
    assert page.progress_bar.isHidden()


def test_every_way_of_finishing_stops_the_clock(qtbot, clock):
    page = _ready_page(qtbot)
    page.show()
    finishers = (
        lambda: page.set_result(None, "완료"),
        lambda: page.set_error("오류"),
        lambda: page.set_cancelled(),
        lambda: page.set_busy(False),
    )
    for finish in finishers:
        page.set_busy(True, "operation-1")
        page.set_progress(1, 4, eta_seconds=9)
        assert page._progress_timer.isActive()

        finish()

        assert not page._progress_timer.isActive()
        assert not page._busy
        frozen = page.progress_label.text()
        clock.advance(30)
        page._refresh_progress()
        assert page.progress_label.text() == frozen
        # The next operation starts counting from zero again.
        page.set_busy(True, "operation-2")
        assert page.progress_label.text() == "OMR 인식을 준비하고 있습니다. 취소할 수 있습니다."
        page.set_busy(False)


def test_terminal_busy_reset_keeps_the_final_message(qtbot, clock):
    page = _ready_page(qtbot)
    page.show()
    page.set_busy(True, "operation-1")
    page.set_progress(2, 10)

    page.set_error("판독에 실패했습니다.")
    page.set_busy(False)

    assert "판독에 실패했습니다." in page.progress_label.text()
    assert "마지막 진행 2 / 10 (20%)" in page.progress_label.text()
    assert not page._progress_timer.isActive()
    assert page.progress_bar.isHidden()


def test_failure_preserves_last_numeric_progress(qtbot):
    page = _ready_page(qtbot)
    page.show()
    page.set_progress(1, 10)

    page.set_error("판독 가능한 OMR 페이지가 없습니다.")

    assert page.progress_bar.isVisible()
    assert page.progress_bar.value() == 1
    assert page.progress_bar.maximum() == 10
    assert "마지막 진행 1 / 10 (10%)" in page.progress_label.text()
    assert "판독 가능한 OMR 페이지가 없습니다." in page.progress_label.text()


def test_default_sensitivity_is_five(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)

    assert page.sensitivity_slider.value() == 5
    assert page.sensitivity_label.text() == "인식 수준 5 / 10"


def test_picker_cancellation_preserves_prior_input_and_marks_state(qtbot):
    page = _ready_page(qtbot)
    prior = page.source_widget.selection

    page.set_source_picker_cancelled()

    assert page.source_widget.selection == prior
    assert page.source_widget.property("pickerState") == "cancelled"
    assert "기존 선택을 유지" in page.source_widget._detail.text()


def test_write_denied_preserves_help_but_blocks_mutation(qtbot):
    page = _ready_page(qtbot)
    page.set_write_enabled(False, "쓰기 권한이 없습니다.")

    assert not page.run_button.isEnabled()
    assert not page.source_widget.isEnabled()
    assert page.progress_label.text() == "쓰기 권한이 없습니다."


def test_sensitivity_range_and_accessible_controls(qtbot):
    page = ScanPage()
    qtbot.addWidget(page)
    page.sensitivity_slider.setValue(10)

    assert page.sensitivity_slider.minimum() == 1
    assert page.sensitivity_slider.maximum() == 10
    assert page.sensitivity_label.text() == "인식 수준 10 / 10"
    assert page.run_button.accessibleName() == "OMR 시험지 인식 및 응답결과 생성"


def _contrast(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", tuple(Theme), ids=lambda theme: theme.value)
def test_theme_styles_the_profile_card_roles_and_highlights_in_both_themes(theme):
    stylesheet = stylesheet_for(theme)
    tokens = tokens_for(theme)

    for selector in (
        "QFrame#scanProfileCard",
        'QFrame#scanProfileCard[dragActive="true"]',
        'QComboBox#profileCombo[attention="true"]',
        'QPushButton#profileImportButton[attention="true"]',
        "QProgressBar#formProgressBar",
        "QLabel#profileSummary",
        'QLabel[role="hint"]',
        'QLabel[role="success"]',
        'QLabel[role="error"]',
        'QLabel#formStatusLabel[role="warning"]',
    ):
        assert selector in stylesheet
    # The caution color is its own token and stays readable on the card surface.
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", tokens.warning)
    assert tokens.warning not in (tokens.success, tokens.error)
    assert tokens.warning in stylesheet
    assert _contrast(tokens.warning, tokens.bg_surface) >= 4.5
