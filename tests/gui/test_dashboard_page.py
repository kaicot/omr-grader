from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QHeaderView, QMessageBox, QPushButton

from omr_grader.domain.enums import ExamTerm, SessionState
from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.ui.dashboard_model import (
    COLUMN_EXAM_NAME,
    COLUMN_GRADED_AT,
    COLUMN_MANAGEMENT,
    COLUMN_SELECTION,
    HEADERS,
    DashboardSelection,
    DashboardTableModel,
    korean_search_key,
)
from omr_grader.ui.dashboard_page import DashboardGlobalRequest, DashboardPage, DashboardRequest
from omr_grader.ui.trash_dialog import TrashDialog


def _entry(
    session_id: str, name: str, year: int = 2026, term: ExamTerm = ExamTerm.SECOND
) -> DashboardIndexEntry:
    return DashboardIndexEntry(
        session_id,
        1,
        f"generation-{session_id}",
        "a" * 64,
        session_id,
        name,
        year,
        term,
        SessionState.GRADED,
        "2026-07-26T14:30:00.000000Z",
        48,
        "78.5",
        "98",
        "42",
        0,
    )


def test_model_exposes_korean_columns_search_filters_and_stable_checked_selection() -> None:
    model = DashboardTableModel()
    first, second = (
        _entry("session-a", "26-2 생리학 중간고사"),
        _entry("session-b", "26-1 약리학 기말고사", 2025, ExamTerm.FIRST),
    )
    model.set_entries((first, second))
    assert (
        tuple(
            model.headerData(column, Qt.Orientation.Horizontal)
            for column in range(model.columnCount())
        )
        == HEADERS
    )
    assert korean_search_key(" 26－2 생리학 ") == korean_search_key("26-2 생리학")
    model.set_filters("생 리 학", 2026, ExamTerm.SECOND)
    assert model.rowCount() == 1 and model.entry_at(0) == first
    model.setData(model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    model.sort(1, Qt.SortOrder.DescendingOrder)
    selection = model.selection()
    assert selection is not None
    assert selection.session_ids == ("session-a",)


def test_dashboard_command_values_reject_mutable_or_invalid_payloads() -> None:
    selection = DashboardSelection(("session-a",), (0,))
    assert DashboardRequest("combined", selection) == DashboardRequest("combined", selection)

    with pytest.raises(TypeError):
        DashboardSelection(["session-a"], (0,))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DashboardSelection(("session-a",), (0, 1))
    with pytest.raises(ValueError):
        DashboardSelection((), ())
    with pytest.raises(ValueError):
        DashboardSelection(("session-a", "session-a"), (0, 1))
    with pytest.raises(TypeError):
        DashboardSelection(("session-a",), (False,))
    with pytest.raises(ValueError):
        DashboardSelection(("session-a",), (-1,))
    with pytest.raises(ValueError):
        DashboardRequest("unknown", selection)
    with pytest.raises(TypeError):
        DashboardRequest("combined", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DashboardGlobalRequest("combined")


def test_dashboard_gates_actions_and_keyboard_table_traversal(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.set_entries((_entry("session-a", "생리학"), _entry("session-b", "약리학")))
    page.show()
    assert not page.backup_button.isEnabled() and not page.combined_button.isEnabled()
    page.model.setData(
        page.model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole
    )
    page._refresh_state()
    assert page.backup_button.isEnabled() and page.combined_button.isEnabled()
    page.table.setCurrentIndex(page.model.index(0, 1))
    QTest.keyClick(page.table, Qt.Key.Key_Down)
    assert page.table.currentIndex().row() == 1
    page.set_write_enabled(False)
    assert page.detail_button.isEnabled()
    assert not page.backup_button.isEnabled()
    assert not page.restore_button.isEnabled()

def test_dashboard_and_trash_commands_emit_once(qtbot, monkeypatch) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    entry = _entry("session-a", "생리학")
    page.set_entries((entry,))
    page.show()
    page.table.setCurrentIndex(page.model.index(0, 1))
    emitted: list[object] = []
    page.request_emitted.connect(emitted.append)

    QTest.mouseClick(page.detail_button, Qt.MouseButton.LeftButton)
    monkeypatch.setattr(
        "omr_grader.ui.dashboard_page.QMessageBox.question",
        lambda *_: QMessageBox.StandardButton.Yes,
    )
    QTest.mouseClick(page.delete_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.trash_button, Qt.MouseButton.LeftButton)

    dialog = page.create_trash_dialog((entry,))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.list_widget.setCurrentRow(0)
    QTest.mouseClick(dialog.restore_button, Qt.MouseButton.LeftButton)

    assert [request.action for request in emitted] == [
        "detail",
        "delete",
        "trash",
        "trash_restore",
    ]
    assert isinstance(emitted[0], DashboardRequest)
    assert isinstance(emitted[1], DashboardRequest)
    assert isinstance(emitted[2], DashboardGlobalRequest)
    assert isinstance(emitted[3], DashboardRequest)
    assert emitted[0].selection.session_ids == ("session-a",)
    assert emitted[1].selection.session_ids == ("session-a",)
    assert emitted[3].selection.session_ids == ("session-a",)


def test_dashboard_scroll_area_reaches_minimum_width_content(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.resize(300, 200)
    page.show()
    assert page.scroll_area.widget().minimumWidth() >= 760
    assert page.scroll_area.horizontalScrollBar().maximum() >= 0


def test_trash_requires_confirmation_for_permanent_delete_and_respects_read_only(
    qtbot, monkeypatch
) -> None:
    dialog = TrashDialog()
    qtbot.addWidget(dialog)
    dialog.set_entries((_entry("session-a", "생리학"),))
    dialog.show()
    dialog.list_widget.setCurrentRow(0)
    emitted: list[object] = []
    dialog.request_emitted.connect(emitted.append)
    monkeypatch.setattr("omr_grader.ui.trash_dialog.QMessageBox.question", lambda *_: 65536)
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    assert not emitted
    dialog.set_write_enabled(False)
    assert not dialog.restore_button.isEnabled() and not dialog.delete_button.isEnabled()


def test_dashboard_row_selection_enables_single_session_backup(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.show()
    page.set_entries((_entry("session-a", "시험 A"),))

    page.table.selectRow(0)

    assert page.backup_button.isEnabled()
    selection = page._selection()
    assert selection is not None
    assert selection.session_ids == ("session-a",)


def test_dashboard_row_click_toggles_checkbox_and_active_row(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.resize(1000, 500)
    page.set_entries((_entry("session-a", "긴 시험 이름 전체 표시"),))
    page.show()

    exam_index = page.model.index(0, COLUMN_EXAM_NAME)
    QTest.mouseClick(
        page.table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=page.table.visualRect(exam_index).center(),
    )

    assert (
        page.model.data(
            page.model.index(0, COLUMN_SELECTION),
            Qt.ItemDataRole.CheckStateRole,
        )
        == Qt.CheckState.Checked
    )
    assert page.table.currentIndex().row() == 0
    assert page.backup_button.isEnabled()


def test_dashboard_columns_are_flexible_and_row_actions_are_buttons(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.set_entries((_entry("session-a", "아주 긴 시험 이름과 전체 정보"),))
    header = page.table.horizontalHeader()

    assert header.sectionResizeMode(COLUMN_SELECTION) == QHeaderView.ResizeMode.ResizeToContents
    assert header.sectionResizeMode(COLUMN_EXAM_NAME) == QHeaderView.ResizeMode.Stretch
    assert header.sectionResizeMode(COLUMN_GRADED_AT) == QHeaderView.ResizeMode.Interactive
    assert "아주 긴 시험 이름과 전체 정보" in str(
        page.model.data(page.model.index(0, COLUMN_EXAM_NAME), Qt.ItemDataRole.ToolTipRole)
    )
    action_cell = page.table.indexWidget(page.model.index(0, COLUMN_MANAGEMENT))
    assert action_cell is not None
    assert {button.text() for button in action_cell.findChildren(QPushButton)} == {
        "상세 보기",
        "삭제",
    }


def test_dashboard_omits_term_filter(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)

    assert not hasattr(page, "term_combo")


def test_dashboard_year_filter_uses_grading_timestamp(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.set_entries((_entry("session-a", "시험 A", 2025),))

    assert page.year_combo.itemData(1) == 2026


def test_dashboard_search_updates_visible_rows_immediately(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.set_entries(
        (
            _entry("session-a", "26-2 생리학 중간고사"),
            _entry("session-b", "26-2 약리학 중간고사"),
        )
    )

    page.search_edit.setText("생 리 학")

    assert page.model.rowCount() == 1
    assert page.model.entry_at(0).session_id == "session-a"
