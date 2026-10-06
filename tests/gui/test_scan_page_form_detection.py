import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.scan_page import ScanPage, ValidatedProfileState

DETECTING = "답안지 양식을 자동으로 확인하는 중입니다…"
PDF = ImportSelection(ImportKind.PDF, ("C:/input/scans.pdf",))


def _profile() -> ValidatedProfileState:
    return ValidatedProfileState(
        name="기본 100문항",
        path="C:/Profiles/basic.omrtemplate",
        dimensions=(1682, 1190),
        grid_summary="학번 8 × 10 · 답안 영역 5개 · 총 100문항",
        is_default=True,
        validated=True,
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


def _page(qtbot) -> ScanPage:
    page = ScanPage()
    qtbot.addWidget(page)
    return page


def _ready_page(qtbot) -> ScanPage:
    page = _page(qtbot)
    page.set_profiles((_profile(),))
    page.exam_name_edit.setText("26-2 생리학 중간고사")
    page.set_source(PDF)
    return page


def test_set_source_emits_source_changed_once_with_the_selection(qtbot):
    page = _page(qtbot)
    changes = QSignalSpy(page.source_changed)

    page.set_source(PDF)

    assert changes.count() == 1
    assert changes.at(0)[0] == PDF


def test_folder_selection_by_set_source_emits_the_folder_selection(qtbot):
    page = _page(qtbot)
    changes = QSignalSpy(page.source_changed)
    folder = ImportSelection(ImportKind.FOLDER, ("C:/input/scans",))

    page.set_source(folder)

    assert changes.count() == 1
    assert changes.at(0)[0] == folder


def test_dropped_source_emits_source_changed(qtbot):
    page = _page(qtbot)
    changes = QSignalSpy(page.source_changed)

    _drop(page.source_widget, ["C:/input/scans"])
    _drop(page.source_widget, ["C:/input/other.PDF"])

    assert [changes.at(index)[0] for index in range(changes.count())] == [
        ImportSelection(ImportKind.FOLDER, ("C:/input/scans",)),
        ImportSelection(ImportKind.PDF, ("C:/input/other.PDF",)),
    ]


def test_browse_result_reaches_source_changed_through_the_selection_widget(qtbot):
    page = _page(qtbot)
    changes = QSignalSpy(page.source_changed)

    # A browse result lands in the widget exactly like a drop; the page's own slot reacts.
    page.source_widget.set_selection(("C:/input/scans",))

    assert changes.count() == 1
    assert changes.at(0)[0] == ImportSelection(ImportKind.FOLDER, ("C:/input/scans",))


def test_clearing_cancelling_or_rejecting_a_source_does_not_emit(qtbot):
    page = _ready_page(qtbot)
    changes = QSignalSpy(page.source_changed)

    page.set_source(None)
    page.set_source_picker_cancelled()
    _drop(page.source_widget, ["C:/input/a", "C:/input/b"])
    page.set_source(ImportSelection(ImportKind.FOLDER, ("C:/input/a", "C:/input/b")))

    assert changes.count() == 0


def test_busy_page_ignores_source_changes_without_emitting(qtbot):
    page = _ready_page(qtbot)
    changes = QSignalSpy(page.source_changed)
    page.set_busy(True, "operation-1")

    # The widget is disabled while busy; deliver the selection to the page's own slot anyway.
    page.source_widget.set_selection(("C:/input/later",))

    assert changes.count() == 0
    assert page.source_widget.selection == ImportSelection(ImportKind.FOLDER, ("C:/input/later",))


def test_form_status_is_hidden_and_empty_until_used(qtbot):
    page = _page(qtbot)

    assert page.form_status_label.objectName() == "formStatusLabel"
    assert page.form_status_label.wordWrap()
    assert page.form_status_label.isHidden()
    assert page.form_status_label.text() == ""


def test_form_status_follows_detecting_detected_and_failed(qtbot):
    page = _page(qtbot)

    page.set_form_detecting()
    assert not page.form_status_label.isHidden()
    assert page.form_status_label.text() == DETECTING
    assert page.form_status_label.property("role") == ""

    page.set_form_detected("자동 인식: 학번 8자리 · 객관식 100문항 (1~100)")
    assert not page.form_status_label.isHidden()
    assert page.form_status_label.text() == "자동 인식: 학번 8자리 · 객관식 100문항 (1~100)"
    assert page.form_status_label.property("role") == "success"

    page.set_form_detection_failed("답안지에서 OMR 양식을 찾지 못했습니다.")
    assert not page.form_status_label.isHidden()
    assert page.form_status_label.text() == "답안지에서 OMR 양식을 찾지 못했습니다."
    assert page.form_status_label.property("role") == "error"


@pytest.mark.parametrize("method", ("set_form_detected", "set_form_detection_failed"))
@pytest.mark.parametrize("value", ("", None, 3))
def test_form_status_results_require_text(qtbot, method, value):
    page = _page(qtbot)

    with pytest.raises(ValueError):
        getattr(page, method)(value)


def test_run_waits_for_form_detection_but_not_for_its_outcome(qtbot):
    page = _ready_page(qtbot)
    assert page.run_button.isEnabled()

    page.set_form_detecting()
    assert not page.run_button.isEnabled()

    page.set_form_detected("자동 인식")
    assert page.run_button.isEnabled()

    page.set_form_detecting()
    page.set_form_detection_failed("양식을 찾지 못했습니다.")
    # A failed detection leaves the manual profile choice usable.
    assert page.run_button.isEnabled()


def test_reset_clears_and_hides_the_form_status_and_releases_the_run_gate(qtbot):
    page = _ready_page(qtbot)
    page.set_form_detecting()

    page.reset_button.click()

    assert page.form_status_label.isHidden()
    assert page.form_status_label.text() == ""
    assert page.form_status_label.property("role") == ""
    page.exam_name_edit.setText("다시 입력")
    page.set_profiles((_profile(),))
    page.set_source(PDF)
    assert page.run_button.isEnabled()

    page.set_form_detection_failed("실패")
    page.reset_button.click()
    assert page.form_status_label.isHidden()
    assert page.form_status_label.text() == ""


def test_form_status_sits_directly_under_the_profile_row(qtbot):
    page = _page(qtbot)
    page.resize(1200, 900)
    page.show()
    page.set_form_detected("자동 인식: 학번 8자리 · 객관식 100문항 (1~100)")
    QApplication.processEvents()

    combo_top = page.profile_combo.mapTo(page, QPoint(0, 0)).y()
    status_top = page.form_status_label.mapTo(page, QPoint(0, 0)).y()
    drop_top = page.profile_widget.mapTo(page, QPoint(0, 0)).y()
    assert combo_top < status_top < drop_top
    assert page.form_status_label.isVisible()
    # Aligned with the profile field column, not the form labels.
    assert (
        page.form_status_label.mapTo(page, QPoint(0, 0)).x()
        == page.profile_combo.mapTo(page, QPoint(0, 0)).x()
    )
