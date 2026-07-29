from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPage, ScanPageRequest, ValidatedProfileState


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
    assert "기준 크기 1682 × 1190" in page.profile_summary.text()
    assert "총 100문항" in page.profile_summary.text()
    assert "기본 프로필" in page.profile_summary.text()
    assert "중복 처리: 다른 이름으로 저장" in page.profile_summary.text()


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
    assert request.sensitivity == 3
    assert request.roster_path == "C:/input/roster.xlsx"


def test_fresh_response_button_is_visible_and_keyboard_accessible_at_minimum_window_size(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.setFocus(Qt.FocusReason.OtherFocusReason)
    QApplication.processEvents()

    page = window.scan_page
    button = page.fresh_response_button
    viewport = window.page_scroll_areas[window.SCAN_PAGE].viewport()
    button_rect = button.rect()
    button_top_left = button.mapTo(viewport, button_rect.topLeft())
    button_bottom_right = button.mapTo(viewport, button_rect.bottomRight())

    assert window.pages.currentIndex() == window.SCAN_PAGE
    assert button.isVisible()
    assert viewport.rect().contains(button_top_left)
    assert viewport.rect().contains(button_bottom_right)
    assert button.text() == "응답 엑셀로 시작"
    assert button.accessibleName() == "응답 엑셀로 새 세션 시작"
    assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert button.isEnabled()

    for _ in range(20):
        if QApplication.focusWidget() is button:
            break
        qtbot.keyClick(window, Qt.Key.Key_Tab)

    assert QApplication.focusWidget() is button
    with qtbot.waitSignal(page.fresh_response_requested):
        qtbot.keyClick(button, Qt.Key.Key_Space)
    with qtbot.waitSignal(page.fresh_response_requested):
        qtbot.keyClick(button, Qt.Key.Key_Return)

    page.set_busy(True, "import-1", cancellable=False)
    assert not page.fresh_response_button.isEnabled()
    assert not page.cancel_button.isEnabled()
    page.set_busy(False)
    page.set_write_enabled(False)
    assert not page.fresh_response_button.isEnabled()


def test_source_folder_pdf_and_roster_drops_enforce_mode_and_extensions(qtbot):
    page = _ready_page(qtbot)
    rejected = []
    page.source_widget.rejected.connect(rejected.append)
    page.roster_widget.rejected.connect(rejected.append)

    page.folder_radio.setChecked(True)
    _drop(page.source_widget, ["C:/input/scans"])
    assert page.source_widget.selection == ImportSelection(ImportKind.FOLDER, ("C:/input/scans",))
    assert page.run_button.isEnabled()

    page.pdf_radio.setChecked(True)
    assert page.source_widget.selection is None
    assert not page.run_button.isEnabled()
    _drop(page.source_widget, ["C:/input/scans.PDF"])
    assert page.source_widget.selection == ImportSelection(ImportKind.PDF, ("C:/input/scans.PDF",))
    _drop(page.source_widget, ["C:/input/scans.png"])
    assert page.source_widget.selection == ImportSelection(ImportKind.PDF, ("C:/input/scans.PDF",))

    _drop(page.roster_widget, ["C:/input/roster.XLSM"])
    assert page.roster_widget.selection == ImportSelection(
        ImportKind.ROSTER, ("C:/input/roster.XLSM",)
    )
    _drop(page.roster_widget, ["C:/input/roster.pdf"])
    assert page.roster_widget.selection == ImportSelection(
        ImportKind.ROSTER, ("C:/input/roster.XLSM",)
    )
    assert rejected == [
        "PDF 파일만 선택할 수 있습니다.",
        "명단 엑셀 파일(.xlsx 또는 .xlsm)만 선택할 수 있습니다.",
    ]


def test_mode_switch_discards_source_and_cancel_preserves_mode(
    qtbot,
):
    page = _ready_page(qtbot)
    page.folder_radio.setChecked(True)
    _drop(page.source_widget, ["C:/input/scans"])
    page.pdf_radio.setChecked(True)

    assert page.source_widget.kind is ImportKind.PDF
    assert page.source_widget.selection is None
    _drop(page.source_widget, ["C:/input/scans.pdf"])
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


def test_progress_includes_counts_elapsed_and_eta(qtbot):
    page = _ready_page(qtbot)

    page.set_progress(4, 10, failed=1, elapsed_seconds=65, eta_seconds=125)

    assert page.progress_bar.value() == 5
    assert "5 / 10" in page.progress_label.text()
    assert "경과 00:01:05" in page.progress_label.text()
    assert "예상 남은 시간 00:02:05" in page.progress_label.text()
    assert page.cancel_button.isEnabled()


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

    assert page.help_button.isEnabled()
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
