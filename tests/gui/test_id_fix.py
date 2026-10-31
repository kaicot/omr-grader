"""'학번 고치기': the dialog, the detail page marks, and the controller flow (synthetic IDs)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from omr_grader.application.detail_presenter import (
    DetailLoadResult,
    DetailPageDisplay,
    DetailStudentDisplay,
    DetailSummaryDisplay,
    NormalizedCell,
)
from omr_grader.application.dto import CommitGenerationResult
from omr_grader.application.id_fix_use_case import StudentIdFixCommand
from omr_grader.domain.enums import IndexState
from omr_grader.domain.errors import Ok
from omr_grader.ui import id_fix_dialog
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.detail_page import DetailPage
from omr_grader.ui.id_fix_dialog import IdFixDialog, check_student_id
from omr_grader.ui.main_window import MainWindow
from tests.helpers.synthetic_omr import DISK_RADIUS, encode_png, render_sheet_with_geometry

READ = "20260103"
MEANT = "20260203"
ROSTER = ((MEANT, "가람"), ("20260002", "나래"))


def _sheet() -> tuple[bytes, tuple[NormalizedCell, ...]]:
    image, geometry = render_sheet_with_geometry({}, READ, seed=3)
    height, width = np.asarray(image).shape[:2]
    nodes = np.asarray(geometry.id_nodes)  # [digit, column] -> (x, y)
    cells = tuple(
        NormalizedCell(
            "id",
            None,
            digit,
            (float(nodes[digit, column, 0]) - DISK_RADIUS) / width,
            (float(nodes[digit, column, 1]) - DISK_RADIUS) / height,
            2 * DISK_RADIUS / width,
            2 * DISK_RADIUS / height,
        )
        for digit in range(10)
        for column in range(8)
    )
    return encode_png(image), cells


def _student(
    student_id: str = READ, fixed_from: str | None = None, image: bool = True
) -> DetailStudentDisplay:
    raster, cells = _sheet() if image else (None, ())
    return DetailStudentDisplay(
        "work-1", student_id, "", 2, "80", (), raster, cells, fixed_from=fixed_from
    )


def _display(*students: DetailStudentDisplay, fixable: bool = True) -> DetailPageDisplay:
    return DetailPageDisplay(
        "session-1",
        4,
        "26졸업고사_p2",
        DetailSummaryDisplay(len(students), "80", "90", "70"),
        students,
        "handle-1",
        ROSTER,
        fixable,
    )


def _loaded(request) -> Ok:
    """A detail_load stub answering with the requested sheet, as the real service does."""
    student = DetailStudentDisplay(request.work_item_id, MEANT, "가람", 1, "90", ())
    return Ok(DetailLoadResult(request.correlation_id, student))


def test_a_typed_id_is_checked_against_the_roster_and_the_other_sheets() -> None:
    roster = dict(ROSTER)

    assert not check_student_id("2026020", current_id=READ, roster=roster, others={}).savable
    assert not check_student_id(READ, current_id=READ, roster=roster, others={}).savable
    found = check_student_id(MEANT, current_id=READ, roster=roster, others={})
    assert found.savable and found.on_roster and not found.warning
    assert found.message == f"✓ 명단: 가람 ({MEANT}) · 이 시험에서 겹치는 학번 없음"
    stranger = check_student_id("20269999", current_id=READ, roster=roster, others={})
    assert stranger.savable and stranger.warning and "명단에 없는 학번" in stranger.message
    clash = check_student_id("20260002", current_id=READ, roster=roster, others={"20260002": "나래"})
    assert clash.savable and clash.warning and "중복확인필요" in clash.message


def test_the_dialog_picks_a_student_by_name_and_frames_the_differing_column(qtbot) -> None:
    dialog = IdFixDialog("시험", _student(), ROSTER, {"20260002": "나래"})
    qtbot.addWidget(dialog)

    assert not dialog.save_button.isEnabled()
    assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()
    assert not dialog.undo_button.isVisibleTo(dialog)
    dialog.search.setCurrentText(f"가람 · {MEANT}")

    assert dialog.id_edit.text() == MEANT
    assert dialog.save_button.isEnabled()
    assert "가람" in dialog.check_label.text()
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert (dialog.chosen_id, dialog.undo) == (MEANT, False)


def test_a_sheet_without_a_picture_or_id_cells_still_opens(qtbot) -> None:
    dialog = IdFixDialog("시험", _student("", image=False), ROSTER, {})
    qtbot.addWidget(dialog)

    assert "그림이 없습니다" in dialog.preview.text()
    dialog.id_edit.setText(MEANT)
    assert dialog.save_button.isEnabled()


def test_a_fixed_sheet_offers_to_go_back(qtbot) -> None:
    dialog = IdFixDialog("시험", _student(MEANT, fixed_from=READ), ROSTER, {})
    qtbot.addWidget(dialog)

    assert dialog.undo_button.isVisibleTo(dialog)
    assert READ in dialog.undo_button.text()
    dialog.undo_button.click()
    assert (dialog.chosen_id, dialog.undo) == (None, True)


def test_a_name_not_on_the_roster_asks_before_saving(qtbot, monkeypatch) -> None:
    dialog = IdFixDialog("시험", _student(), ROSTER, {})
    qtbot.addWidget(dialog)
    asked: list[str] = []
    monkeypatch.setattr(
        id_fix_dialog.QMessageBox,
        "question",
        lambda *args: asked.append(args[2]) or id_fix_dialog.QMessageBox.StandardButton.No,
    )
    dialog.id_edit.setText("20269999")

    dialog._save()

    assert asked and dialog.chosen_id is None


def test_the_detail_page_marks_ids_to_check_and_offers_the_fix(qtbot) -> None:
    page = DetailPage()
    qtbot.addWidget(page)
    unknown = _student(image=False)
    fixed = DetailStudentDisplay("work-2", MEANT, "가람", 1, "90", (), fixed_from=READ)

    page.set_display(_display(unknown, fixed))

    assert "학번 확인 필요 1명" in page.summary_label.text()
    assert page.model.data(page.model.index(0, 1), Qt.ItemDataRole.BackgroundRole) is not None
    assert page.model.data(page.model.index(1, 1)) == f"{MEANT} (수정)"
    assert page.id_fix_button.isEnabled()
    page.set_display(_display(unknown, fixable=False))
    assert not page.id_fix_button.isEnabled()


def test_the_controller_commits_the_fix_and_reopens_the_detail_page(qtbot, monkeypatch) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    commands: list[StudentIdFixCommand] = []
    closed: list[str] = []
    reopened = replace(_display(_student(MEANT, fixed_from=READ, image=False)), revision=5)

    def fix(command: StudentIdFixCommand) -> Ok:
        commands.append(command)
        return Ok(
            CommitGenerationResult(True, "session-1", 5, "g5", IndexState.STALE, command.operation_id)
        )

    def open_detail(selection) -> Ok:
        assert selection.revisions == (5,)
        return Ok(reopened)

    def accept(self) -> int:
        self.chosen_id = MEANT
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(IdFixDialog, "exec", accept)
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            id_fix=fix,
            dashboard_detail=open_detail,
            detail_close=lambda request: closed.append(request.detail_handle) or Ok(None),
            detail_load=_loaded,
        ),
        write_enabled=True,
    )
    window.detail_page.set_display(_display(_student(image=False)))

    window.detail_page.id_fix_button.click()

    # Saving locks the page: no going back while the new revision is being made.
    assert not window.detail_page.back_button.isEnabled()
    qtbot.waitUntil(
        lambda: window.detail_page._display is not None
        and window.detail_page._display.revision == 4 + 1,
        timeout=5000,
    )
    qtbot.waitUntil(lambda: window.detail_page.back_button.isEnabled(), timeout=5000)
    assert [(c.work_item_id, c.student_id, c.expected_revision) for c in commands] == [
        ("work-1", MEANT, 4)
    ]
    assert closed == ["handle-1"]
    assert "학번을 고쳤습니다" in window.status_label.text()
    controller.close()


@pytest.mark.parametrize("undo", (True, False))
def test_cancelling_or_undoing_reaches_the_service_only_when_asked(qtbot, monkeypatch, undo) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    commands: list[StudentIdFixCommand] = []

    def answer(self) -> int:
        self.undo = undo
        return QDialog.DialogCode.Accepted if undo else QDialog.DialogCode.Rejected

    monkeypatch.setattr(IdFixDialog, "exec", answer)
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            id_fix=lambda command: commands.append(command) or Ok(None),
            detail_load=_loaded,
        ),
        write_enabled=True,
    )
    window.detail_page.set_display(_display(_student(MEANT, fixed_from=READ, image=False)))

    window.detail_page.id_fix_button.click()

    if undo:
        qtbot.waitUntil(lambda: bool(commands), timeout=5000)
        assert commands[0].student_id is None
    else:
        assert commands == []
    controller.close()
