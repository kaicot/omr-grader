import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.scan_page import ScanPage, ValidatedProfileState

HINT = "스캔을 선택하면 인식 프로필이 자동으로 지정됩니다."
DETECTING = "⏳ 답안지 양식을 확인하는 중입니다…"
TIDYING = "⏳ 확인한 쪽으로 양식을 정리하는 중…"
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


def _attention(page) -> tuple[bool, bool]:
    """Whether the profile list and the import button are highlighted."""
    return (
        page.profile_combo.property("attention") is True,
        page.profile_import_button.property("attention") is True,
    )


def test_form_status_starts_as_a_visible_grey_hint(qtbot):
    page = _page(qtbot)

    assert page.form_status_label.objectName() == "formStatusLabel"
    assert page.form_status_label.wordWrap()
    assert not page.form_status_label.isHidden()
    assert page.form_status_label.text() == HINT
    assert page.form_status_label.property("role") == "hint"
    assert page.form_progress_bar.isHidden()
    assert _attention(page) == (False, False)


def test_form_status_follows_detecting_detected_warned_and_failed(qtbot):
    page = _page(qtbot)

    page.set_form_detecting()
    assert page.form_status_label.text() == DETECTING
    assert page.form_status_label.property("role") == ""
    assert not page.form_progress_bar.isHidden()
    assert (page.form_progress_bar.minimum(), page.form_progress_bar.maximum()) == (0, 0)

    page.set_form_detected("자동 인식: 학번 8자리 · 객관식 100문항 (1~100)")
    assert page.form_status_label.text() == "✓ 자동 인식: 학번 8자리 · 객관식 100문항 (1~100)"
    assert page.form_status_label.property("role") == "success"
    assert page.form_progress_bar.isHidden()
    assert _attention(page) == (False, False)

    page.set_form_detected("자동 인식: 학번 8자리", warning=True)
    assert page.form_status_label.text() == "⚠ 자동 인식: 학번 8자리"
    assert page.form_status_label.property("role") == "warning"
    assert page.form_progress_bar.isHidden()
    assert _attention(page) == (False, False)

    page.set_form_detection_failed("답안지에서 OMR 양식을 찾지 못했습니다.")
    assert page.form_status_label.text() == "✗ 답안지에서 OMR 양식을 찾지 못했습니다."
    assert page.form_status_label.property("role") == "error"
    assert page.form_progress_bar.isHidden()
    assert _attention(page) == (True, True)


def test_form_status_text_may_span_lines_and_only_the_first_gets_the_symbol(qtbot):
    page = _page(qtbot)

    page.set_form_detected("요약\n설명 하나\n설명 둘")
    assert page.form_status_label.text() == "✓ 요약\n설명 하나\n설명 둘"
    page.set_form_detected("요약\n주의", warning=True)
    assert page.form_status_label.text() == "⚠ 요약\n주의"
    page.set_form_detection_failed("실패\n이유")
    assert page.form_status_label.text() == "✗ 실패\n이유"


def test_form_detection_progress_counts_pages_and_then_tidies_up(qtbot):
    page = _page(qtbot)
    bar = page.form_progress_bar

    page.set_form_detection_progress(0, 24)
    assert page.form_status_label.text() == "⏳ 답안지 양식을 확인하는 중 (0 / 24쪽)"
    assert page.form_status_label.property("role") == ""
    assert not bar.isHidden()
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 24, 0)

    page.set_form_detection_progress(7, 24)
    assert page.form_status_label.text() == "⏳ 답안지 양식을 확인하는 중 (7 / 24쪽)"
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 24, 7)

    for done in (24, 30):
        page.set_form_detection_progress(done, 24)
        assert page.form_status_label.text() == TIDYING
        assert not bar.isHidden()
        assert (bar.minimum(), bar.maximum()) == (0, 0)

    # A new count after tidying up shows the page count again.
    page.set_form_detection_progress(1, 24)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 24, 1)

    page.set_form_detected("자동 인식")
    assert bar.isHidden()


@pytest.mark.parametrize(
    ("done", "total", "error"),
    (
        (-1, 5, ValueError),
        (1, -5, ValueError),
        (1.5, 5, TypeError),
        (1, "5", TypeError),
        (True, 5, TypeError),
        (None, 5, TypeError),
    ),
)
def test_form_detection_progress_rejects_bad_counts(qtbot, done, total, error):
    page = _page(qtbot)

    with pytest.raises(error):
        page.set_form_detection_progress(done, total)

    assert page.form_status_label.text() == HINT
    assert page.form_progress_bar.isHidden()


@pytest.mark.parametrize("value", (1, "yes", None))
def test_form_detected_warning_is_a_keyword_only_bool(qtbot, value):
    page = _page(qtbot)

    with pytest.raises(TypeError):
        page.set_form_detected("자동 인식", warning=value)
    with pytest.raises(TypeError):
        page.set_form_detected("자동 인식", True)

    assert page.form_status_label.text() == HINT


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


def test_run_waits_while_pages_are_being_checked(qtbot):
    page = _ready_page(qtbot)

    page.set_form_detection_progress(3, 12)
    assert not page.run_button.isEnabled()
    assert page.run_hint_label.text() == "인식 프로필을 확인하는 중입니다."
    page.set_form_detection_progress(12, 12)
    assert not page.run_button.isEnabled()

    page.set_form_detected("자동 인식", warning=True)
    assert page.run_button.isEnabled()


def test_reset_returns_the_form_status_to_the_hint_and_releases_the_run_gate(qtbot):
    page = _ready_page(qtbot)
    page.set_form_detecting()

    page.reset_button.click()

    assert page.form_status_label.text() == HINT
    assert page.form_status_label.property("role") == "hint"
    assert not page.form_status_label.isHidden()
    assert page.form_progress_bar.isHidden()
    page.exam_name_edit.setText("다시 입력")
    page.set_profiles((_profile(),))
    page.set_source(PDF)
    assert page.run_button.isEnabled()

    page.set_form_detection_failed("실패")
    assert _attention(page) == (True, True)
    page.reset_button.click()
    assert page.form_status_label.text() == HINT
    assert page.form_status_label.property("role") == "hint"
    assert _attention(page) == (False, False)


def test_clearing_the_source_returns_the_form_status_to_the_hint(qtbot):
    page = _ready_page(qtbot)
    page.set_form_detecting()

    page.set_source(None)

    assert page.form_status_label.text() == HINT
    assert page.form_status_label.property("role") == "hint"
    assert page.form_progress_bar.isHidden()
    page.set_form_detected("자동 인식")
    page.set_source(None)
    assert page.form_status_label.text() == HINT
    page.set_form_detection_failed("실패")
    page.set_source(None)
    assert page.form_status_label.text() == HINT
    assert _attention(page) == (False, False)
    # The check that was running described scans that are gone, so it no longer gates a run.
    page.exam_name_edit.setText("다시 입력")
    page.set_source(PDF)
    assert page.run_button.isEnabled()


def test_a_scan_that_fails_to_load_clears_the_status_of_the_scans_it_replaces(qtbot):
    page = _ready_page(qtbot)
    page.set_form_detected("자동 인식")

    page.set_source(ImportSelection(ImportKind.FOLDER, ("C:/input/a", "C:/input/b")))

    assert page.current_source() is None
    assert page.form_status_label.text() == HINT


def test_failed_detection_highlights_the_manual_profile_controls_until_one_is_picked(qtbot):
    page = _ready_page(qtbot)
    page.set_form_detection_failed("양식을 찾지 못했습니다.")
    assert _attention(page) == (True, True)

    # Changing the list from code is not a pick by the user.
    page.profile_combo.setCurrentIndex(0)
    page.profile_combo.setCurrentIndex(1)
    assert _attention(page) == (True, True)
    assert page.manual_profile_choices == 0

    page.profile_combo.activated.emit(1)

    assert _attention(page) == (False, False)
    assert page.manual_profile_choices == 1
    # The failure message stays; only the highlight was answered.
    assert page.form_status_label.property("role") == "error"


def test_keyboard_pick_in_the_profile_list_clears_the_highlight(qtbot):
    page = _ready_page(qtbot)
    page.show()
    page.set_form_detection_failed("양식을 찾지 못했습니다.")
    page.profile_combo.setFocus()

    qtbot.keyClick(page.profile_combo, Qt.Key.Key_Up)

    assert page.manual_profile_choices == 1
    assert _attention(page) == (False, False)


@pytest.mark.parametrize(
    "answer",
    (
        lambda page: page.select_profile("C:/Profiles/basic.omrtemplate"),
        lambda page: page.set_form_detected("자동 인식"),
        lambda page: page.set_form_detected("자동 인식", warning=True),
        lambda page: page.set_form_detecting(),
        lambda page: page.set_form_detection_progress(1, 3),
        lambda page: page.reset_button.click(),
        lambda page: page.set_source(None),
    ),
    ids=("imported", "found", "warned", "detecting", "progress", "reset", "source-cleared"),
)
def test_highlight_clears_when_the_profile_problem_is_answered(qtbot, answer):
    page = _ready_page(qtbot)
    page.set_form_detection_failed("양식을 찾지 못했습니다.")
    assert _attention(page) == (True, True)

    answer(page)

    assert _attention(page) == (False, False)


def test_form_status_sits_between_the_section_title_and_the_profile_row(qtbot):
    page = _page(qtbot)
    page.resize(1200, 1600)
    page.show()
    page.set_form_detecting()
    QApplication.processEvents()

    def top(widget):
        return widget.mapTo(page, QPoint(0, 0)).y()

    card = page.findChild(QFrame, "scanProfileCard")
    title = card.findChild(QLabel, "scanSectionLabel")
    assert top(title) < top(page.form_status_label) < top(page.form_progress_bar)
    assert top(page.form_progress_bar) < top(page.profile_combo) < top(page.profile_summary)
    page.set_form_detected("자동 인식: 학번 8자리 · 객관식 100문항 (1~100)")
    QApplication.processEvents()
    assert page.form_status_label.isVisible()
    assert page.form_progress_bar.isHidden()
    assert top(title) < top(page.form_status_label) < top(page.profile_combo)
    # The import button shares the combo's row, and nothing draws a separate drop zone.
    assert page.profile_combo.x() < page.profile_import_button.x()
    assert abs(top(page.profile_combo) - top(page.profile_import_button)) < 20
    assert page.profile_widget.isHidden()
    assert (
        page.form_status_label.mapTo(page, QPoint(0, 0)).x()
        == page.profile_combo.mapTo(page, QPoint(0, 0)).x()
    )
