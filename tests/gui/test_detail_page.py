from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QGraphicsView, QPushButton

from omr_grader.application.detail_presenter import (
    DetailAnswerDisplay,
    DetailLoadRequest,
    DetailLoadResult,
    DetailPageDisplay,
    DetailPageRequest,
    DetailPreviewResult,
    DetailSaveResult,
    DetailStudentDisplay,
    DetailSummaryDisplay,
    NormalizedCell,
)
from omr_grader.domain.enums import AnswerStatus
from omr_grader.domain.models import AnswerValue
from omr_grader.ui.detail_page import DetailPage

_RASTER = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0fa\x00\x00"
    b"\x0fa\x01\xa8?\xa7i\x00\x00\x00\x0cIDAT\x08\x99c```\x00\x00\x00\x04"
    b"\x00\x01\xa3\n\x15\xe3\x00\x00\x00\x00IEND\xaeB`\x82"
)

ANSWER_2 = AnswerValue((2,), AnswerStatus.NORMAL)
ANSWER_3 = AnswerValue((3,), AnswerStatus.NORMAL)
ANSWER_4 = AnswerValue((4,), AnswerStatus.NORMAL)

_IMAGE_WIDTH, _IMAGE_HEIGHT = 200, 100
_EDIT_SIGNALS = (
    "preview_requested",
    "save_requested",
    "unsaved_changes_requested",
    "discard_requested",
)


def _display(image: bytes | None = None, revision: int = 3) -> DetailPageDisplay:
    return DetailPageDisplay(
        "session-1",
        revision,
        "생리학",
        DetailSummaryDisplay(2, "80", "90", "70"),
        (
            DetailStudentDisplay(
                "work-1",
                "00000001",
                "홍길동",
                1,
                "90",
                (DetailAnswerDisplay(1, ANSWER_2, True),),
                image,
                (NormalizedCell("answer", 1, 2, 0.1, 0.1, 0.1, 0.1),),
                (0, 0, 0, 0, 0, 0, 0, 1),
            ),
            DetailStudentDisplay(
                "work-2",
                "00000001",
                "김철수",
                1,
                "90",
                (DetailAnswerDisplay(1, ANSWER_3, True),),
                id_digits=(0, 0, 0, 0, 0, 0, 0, 1),
                id_conflict="중복 학번",
            ),
        ),
    )


def _png() -> bytes:
    """A blank page big enough that a click lands on one normalized cell."""
    image = QImage(_IMAGE_WIDTH, _IMAGE_HEIGHT, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


def _loaded_page(qtbot, raster: bytes = _RASTER) -> DetailPage:
    page = DetailPage()
    qtbot.addWidget(page)
    requested = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    page.apply_loaded_work_item(
        DetailLoadResult(requested[-1].correlation_id, _display(raster).students[0])
    )
    return page


def _watch_edit_signals(page: DetailPage) -> dict[str, list[object]]:
    seen: dict[str, list[object]] = {name: [] for name in _EDIT_SIGNALS}
    for name, received in seen.items():
        getattr(page, name).connect(received.append)
    return seen


def _assert_untouched(page: DetailPage, seen: dict[str, list[object]]) -> None:
    assert page.is_dirty is False
    assert page.pending_edits == ()
    assert seen == {name: [] for name in _EDIT_SIGNALS}
    assert page.model.student_at(0).answers == (DetailAnswerDisplay(1, ANSWER_2, True),)


def test_list_is_available_before_an_image(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    requested = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    assert page.model.rowCount() == 2
    assert not page.no_image_label.isHidden()
    assert page.graphics_view.active_image_count == 0
    assert requested and requested[-1].work_item_id == "work-1"


def test_detail_page_offers_no_correction_controls(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())

    assert not hasattr(page, "save_button")
    assert page.findChildren(QPushButton, "detailSaveButton") == []
    assert all(button.text() != "수정사항 저장" for button in page.findChildren(QPushButton))
    assert not hasattr(page.graphics_view, "cell_activated")
    assert not hasattr(page.graphics_view, "set_editable")


def test_clicking_a_cell_on_the_image_changes_nothing(qtbot) -> None:
    page = _loaded_page(qtbot, _png())
    page.show()
    qtbot.waitExposed(page)
    view = page.graphics_view
    seen = _watch_edit_signals(page)
    cell = _display().students[0].cells[0]
    centre = QPointF(
        (cell.left + cell.width / 2) * _IMAGE_WIDTH, (cell.top + cell.height / 2) * _IMAGE_HEIGHT
    )
    assert view.cell_at(centre) == cell

    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(centre))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(centre))
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(centre))

    _assert_untouched(page, seen)
    assert view.active_image_count == 1


@pytest.mark.parametrize("write_enabled", [True, False])
@pytest.mark.parametrize(
    "key",
    [
        Qt.Key.Key_Return,
        Qt.Key.Key_Enter,
        Qt.Key.Key_Space,
        Qt.Key.Key_Left,
        Qt.Key.Key_Right,
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
    ],
    ids=lambda key: key.name,
)
def test_keyboard_on_the_image_changes_nothing(qtbot, key, write_enabled) -> None:
    page = _loaded_page(qtbot, _png())
    page.set_write_enabled(write_enabled)
    seen = _watch_edit_signals(page)

    qtbot.keyClick(page.graphics_view, key)
    qtbot.keyClick(page.graphics_view, Qt.Key.Key_Return)
    qtbot.keyClick(page.graphics_view, Qt.Key.Key_Space)

    _assert_untouched(page, seen)


def test_back_and_close_leave_straight_away_after_interacting_with_the_image(qtbot) -> None:
    page = _loaded_page(qtbot, _png())
    page.show()
    qtbot.waitExposed(page)
    view = page.graphics_view
    backs, closes, prompts = [], [], []
    page.back_requested.connect(backs.append)
    page.close_requested.connect(closes.append)
    page.unsaved_changes_requested.connect(prompts.append)
    centre = QPointF(0.15 * _IMAGE_WIDTH, 0.15 * _IMAGE_HEIGHT)
    assert view.cell_at(centre) is not None
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(centre))

    page.back_button.click()
    page.request_close()

    assert prompts == []
    assert len(backs) == 1 and len(closes) == 1
    assert (backs[0].intent, closes[0].intent) == ("back", "close")
    for request in (backs[0], closes[0]):
        assert (request.session_id, request.revision) == ("session-1", 3)
        assert request.edits == ()
        assert request.correlation_id


def test_navigation_requests_need_a_display(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    backs, closes = [], []
    page.back_requested.connect(backs.append)
    page.close_requested.connect(closes.append)

    page.request_back()
    page.request_close()

    assert backs == [] and closes == []


def test_preview_and_save_results_are_ignored(qtbot) -> None:
    page = _loaded_page(qtbot)
    seen = _watch_edit_signals(page)
    projected = DetailPageDisplay(
        "session-1",
        3,
        "생리학",
        DetailSummaryDisplay(2, "80", "90", "70"),
        (
            DetailStudentDisplay(
                "work-1",
                "12345678",
                "홍길동",
                2,
                "70",
                (DetailAnswerDisplay(1, ANSWER_4, False),),
                None,
                (),
                (1, 2, 3, 4, 5, 6, 7, 8),
            ),
        ),
    )

    page.apply_preview(DetailPreviewResult("any-correlation", projected))
    page.save_completed(DetailSaveResult("any-correlation", _display(revision=4)))
    page.save_failed("any-correlation")

    _assert_untouched(page, seen)
    assert page.model.rowCount() == 2
    assert page.model.student_at(0).student_id == "00000001"
    assert page.graphics_view.active_image_count == 1
    backs = []
    page.back_requested.connect(backs.append)
    page.request_back()
    assert backs[0].revision == 3


def test_selecting_students_shows_the_id_conflict_and_scores(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())

    assert "총원 2명" in page.summary_label.text()
    assert "최고점 90점" in page.summary_label.text() and "최저점 70점" in page.summary_label.text()
    assert page.title_label.text() == "생리학 상세 결과"
    assert page.conflict_label.text() == ""
    assert page.model.data(page.model.index(0, 3)) == "90"

    page.table.selectRow(1)

    assert page.conflict_label.text() == "중복 학번"
    assert page.model.data(page.model.index(1, 2)) == "김철수"
    assert page.model.data(page.model.index(1, 4)) == "O"


def test_zoom_fit_and_pan_keep_working_on_the_view(qtbot) -> None:
    page = _loaded_page(qtbot, _png())
    page.show()
    qtbot.waitExposed(page)
    view = page.graphics_view

    assert view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    page.zoom_in_button.click()
    assert view.zoom > 1.0
    page.zoom_out_button.click()
    page.zoom_out_button.click()
    assert view.zoom < 1.0
    for _ in range(4):
        page.zoom_in_button.click()
    qtbot.keyClick(view, Qt.Key.Key_Plus)
    zoomed = view.zoom
    assert zoomed > 1.0
    qtbot.keyClick(view, Qt.Key.Key_Minus)
    assert view.zoom < zoomed
    page.fit_button.click()
    assert view.zoom == 1.0

    for _ in range(6):
        page.zoom_in_button.click()
    horizontal = view.horizontalScrollBar()
    assert horizontal.maximum() > 0
    horizontal.setValue(horizontal.maximum() // 2)
    start = horizontal.value()
    centre = view.viewport().rect().center()
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=centre)
    qtbot.mouseMove(view.viewport(), pos=centre + QPoint(40, 0))
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=centre + QPoint(40, 0))
    assert horizontal.value() < start
    qtbot.keyClick(view, Qt.Key.Key_0)
    assert view.zoom == 1.0
    assert page.is_dirty is False


def test_off_selection_lazy_completion_is_rejected(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    requested = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    first = requested[-1]
    page.table.selectRow(1)
    page.apply_loaded_work_item(
        DetailLoadResult(first.correlation_id, _display(b"stale").students[0])
    )
    assert page.table.currentIndex().row() == 1
    assert page.graphics_view.active_image_count == 0


def test_returning_to_a_student_reloads_the_image_dropped_for_another(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    requested: list[DetailLoadRequest] = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    first = _display(_RASTER).students[0]
    second = replace(_display().students[1], image_bytes=_RASTER)
    page.apply_loaded_work_item(DetailLoadResult(requested[-1].correlation_id, first))
    page.table.selectRow(1)
    page.apply_loaded_work_item(DetailLoadResult(requested[-1].correlation_id, second))
    before = len(requested)

    page.table.selectRow(0)

    # Only the shown image is kept, so the first student's image is requested again.
    assert len(requested) > before
    assert requested[-1].work_item_id == "work-1"
    page.apply_loaded_work_item(DetailLoadResult(requested[-1].correlation_id, first))
    assert page.graphics_view.active_image_count == 1
    assert not page.no_image_label.isVisibleTo(page)


def test_refreshed_display_of_the_same_session_keeps_selected_row_and_raster(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    requested = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    page.apply_loaded_work_item(
        DetailLoadResult(requested[-1].correlation_id, _display(_RASTER).students[0])
    )
    page.set_display(_display(revision=4))
    assert page.table.currentIndex().row() == 0
    assert page.graphics_view.active_image_count == 1


def test_detail_dtos_reject_malformed_nested_members_and_missing_correlations() -> None:
    with pytest.raises(TypeError):
        DetailStudentDisplay("work", "id", "name", None, "0", (object(),))
    with pytest.raises(TypeError):
        DetailPageDisplay("session", 0, "exam", object(), ())
    with pytest.raises(TypeError):
        DetailPageDisplay(
            "session", 0, "exam", DetailSummaryDisplay(0, "0", "0", "0"), (object(),)
        )
    with pytest.raises(ValueError):
        DetailLoadRequest("session", 0, None, "work", "")
    with pytest.raises(ValueError):
        DetailPageRequest("session", 0, "preview", (), None, "")
    class MutableHandle(str):
        pass

    assert DetailLoadRequest("session", 0, None, "work", "correlation").detail_handle is None
    with pytest.raises(ValueError):
        DetailLoadRequest("session", 0, "", "work", "correlation")
    with pytest.raises(ValueError):
        DetailLoadRequest("session", 0, MutableHandle("handle"), "work", "correlation")
    with pytest.raises(ValueError):
        DetailLoadRequest("session", 0, bytearray(b"handle"), "work", "correlation")


def test_read_only_keeps_viewing_and_navigation(qtbot) -> None:
    page = _loaded_page(qtbot)
    seen = _watch_edit_signals(page)
    page.set_write_enabled(False)
    backs = []
    page.back_requested.connect(backs.append)

    page.back_button.click()

    assert page.model.rowCount() == 2
    assert page.graphics_view.active_image_count == 1
    assert page.back_button.isEnabled() and len(backs) == 1
    _assert_untouched(page, seen)
    page.set_write_enabled(True)
    with pytest.raises(TypeError):
        page.set_write_enabled(1)


def test_keyboard_zoom_bounds_and_minimum_accessibility(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())
    view = page.graphics_view
    for _ in range(40):
        view.zoom_in()
    assert view.zoom <= view.MAX_ZOOM
    for _ in range(80):
        view.zoom_out()
    assert view.zoom >= view.MIN_ZOOM
    assert page.minimumWidth() >= 720 and page.minimumHeight() >= 480
    assert page.back_button.accessibleName()


def test_detail_removes_student_id_editing_and_explains_excel_image_absence(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())

    assert not hasattr(page, "id_inputs")
    assert page.findChildren(type(page.back_button), "studentIdEdit") == []
    assert page.no_image_label.text() == "엑셀로 채점된 결과로 OMR 이미지가 없습니다"
    assert page.back_button.objectName() == "detailBackButton"
