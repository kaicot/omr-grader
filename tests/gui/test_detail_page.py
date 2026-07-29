from __future__ import annotations

import pytest

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
from omr_grader.ui.detail_page import DetailPage

_RASTER = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0fa\x00\x00"
    b"\x0fa\x01\xa8?\xa7i\x00\x00\x00\x0cIDAT\x08\x99c```\x00\x00\x00\x04"
    b"\x00\x01\xa3\n\x15\xe3\x00\x00\x00\x00IEND\xaeB`\x82"
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
                (DetailAnswerDisplay(1, 2, True),),
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
                (DetailAnswerDisplay(1, 3, True),),
                id_digits=(0, 0, 0, 0, 0, 0, 0, 1),
                id_conflict="중복 학번",
            ),
        ),
    )


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


def test_typed_edits_coalesce_and_save_locks_until_correlated_completion(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())
    load = []
    page.work_item_load_requested.connect(load.append)
    page._selected_row_changed(page.model.index(0, 0), page.model.index(0, 0))
    page.apply_loaded_work_item(
        DetailLoadResult(load[-1].correlation_id, _display(b"raster").students[0])
    )
    cell = _display().students[0].cells[0]
    page._activate_cell(cell)
    assert page.pending_edits[0].before == 2 and page.pending_edits[0].after is None
    page._activate_cell(cell)
    assert not page.is_dirty
    page._activate_cell(NormalizedCell("answer", 1, 3, 0.2, 0.1, 0.1, 0.1))
    edits = page.pending_edits
    saved = []
    page.save_requested.connect(saved.append)
    page.request_save()
    assert saved and not page.id_inputs[0].isEnabled()
    page._activate_cell(cell)
    assert page.pending_edits == edits
    page.save_completed(DetailSaveResult(saved[-1].correlation_id, _display(revision=3)))
    assert page.is_dirty
    page.save_completed(DetailSaveResult(saved[-1].correlation_id, _display(revision=4)))
    assert not page.is_dirty

def test_save_failure_releases_matching_attempt_and_ignores_stale_failure(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    requested = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    page.apply_loaded_work_item(
        DetailLoadResult(requested[-1].correlation_id, _display(b"raster").students[0])
    )
    page._activate_cell(NormalizedCell("answer", 1, 3, 0.2, 0.1, 0.1, 0.1))
    saved = []
    page.save_requested.connect(saved.append)

    page.request_save()
    first = saved[-1]
    page.save_failed(first.correlation_id)

    assert page.is_dirty and page.save_button.isEnabled()
    page.request_save()
    second = saved[-1]
    page.save_failed(first.correlation_id)

    assert second.correlation_id != first.correlation_id
    assert page.is_dirty and not page.save_button.isEnabled()
    page.save_failed(second.correlation_id)

    assert page.is_dirty and page.save_button.isEnabled()


def test_preview_replaces_answer_and_id_display_from_projected_result(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())
    request = page._request("preview")
    assert request is not None
    projected_student = DetailStudentDisplay(
        "work-1",
        "12345678",
        "홍길동",
        2,
        "70",
        (DetailAnswerDisplay(1, 4, False),),
        None,
        (),
        (1, 2, 3, 4, 5, 6, 7, 8),
    )
    projected = DetailPageDisplay(
        "session-1",
        3,
        "생리학",
        DetailSummaryDisplay(2, "80", "90", "70"),
        (projected_student,),
    )

    page.apply_preview(DetailPreviewResult(request.correlation_id or "", projected))

    assert page.model.student_at(0).answers == (DetailAnswerDisplay(1, 4, False),)
    assert [field.text() for field in page.id_inputs] == list("12345678")

def test_correction_rejects_listing_before_values_until_lazy_authority_arrives(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())
    page._activate_cell(_display().students[0].cells[0])
    assert not page.is_dirty


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


def test_model_resets_restore_selected_row_and_raster(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    requested = []
    page.work_item_load_requested.connect(requested.append)
    page.set_display(_display())
    page.apply_loaded_work_item(
        DetailLoadResult(requested[-1].correlation_id, _display(_RASTER).students[0])
    )
    request = page._request("preview")
    assert request is not None
    page.apply_preview(DetailPreviewResult(request.correlation_id, _display()))
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

def test_read_only_keeps_viewing_and_disables_corrections(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    page.set_display(_display())
    page.set_write_enabled(False)
    page._activate_cell(NormalizedCell("answer", 1, 3, 0.2, 0.1, 0.1, 0.1))
    assert not page.is_dirty and not page.save_button.isEnabled() and page.back_button.isEnabled()


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
    assert page.back_button.accessibleName() and page.save_button.accessibleName()
    assert all(field.accessibleName() for field in page.id_inputs)
