"""Screen 3 dashboard: a passive UI over immutable index entries."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.ui.dashboard_model import DashboardSelection, DashboardTableModel
from omr_grader.ui.trash_dialog import TrashDialog, TrashRequest

_DASHBOARD_ACTIONS = frozenset(
    {"detail", "delete", "backup", "restore", "combined", "trash", "trash_restore", "trash_delete"}
)
_GLOBAL_DASHBOARD_ACTIONS = frozenset({"restore", "trash"})


@dataclass(frozen=True, slots=True)
class DashboardRequest:
    """Frozen controller intent containing no Qt object or mutable payload."""

    action: str
    selection: DashboardSelection

    def __post_init__(self) -> None:
        if type(self.action) is not str:
            raise TypeError("action must be str")
        if self.action not in _DASHBOARD_ACTIONS:
            raise ValueError("unsupported dashboard action")
        if type(self.selection) is not DashboardSelection:
            raise TypeError("selection must be exactly DashboardSelection")


@dataclass(frozen=True, slots=True)
class DashboardGlobalRequest:
    """Frozen controller intent for a dashboard action without a selection."""

    action: str

    def __post_init__(self) -> None:
        if type(self.action) is not str:
            raise TypeError("action must be str")
        if self.action not in _GLOBAL_DASHBOARD_ACTIONS:
            raise ValueError("unsupported global dashboard action")


class DashboardPage(QWidget):
    """Exam-management dashboard with controller-bound value-only signals."""

    request_emitted = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardPage")
        self._write_enabled = True
        self._busy = False
        self._build_ui()
        self._refresh_state()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("dashboardScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAccessibleName("시험 관리 대시보드 내용")
        content = QWidget()
        content.setObjectName("dashboardContent")
        content.setMinimumWidth(760)
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)
        title = QLabel("시험 관리 대시보드")
        title.setObjectName("dashboardTitle")
        title.setProperty("role", "page-title")
        root.addWidget(title)
        toolbar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("dashboardSearch")
        self.search_edit.setPlaceholderText("시험명 검색...")
        self.search_edit.setAccessibleName("시험명 검색")
        self.search_edit.textChanged.connect(self._apply_filters)
        toolbar.addWidget(self.search_edit, 2)
        self.year_combo = QComboBox()
        self.year_combo.setObjectName("dashboardYearFilter")
        self.year_combo.setAccessibleName("시험 연도 필터")
        self.year_combo.addItem("연도 전체", None)
        self.year_combo.currentIndexChanged.connect(self._apply_filters)
        toolbar.addWidget(self.year_combo)
        root.addLayout(toolbar)
        actions = QHBoxLayout()
        self.backup_button = self._button("백업하기", "dashboardBackupButton", "backup")
        self.restore_button = self._button("백업 복구하기", "dashboardRestoreButton", "restore")
        self.combined_button = self._button(
            "통합 성적표 생성", "dashboardCombinedButton", "combined"
        )
        self.trash_button = self._button("휴지통 보기", "dashboardTrashButton", "trash")
        for button in (
            self.backup_button,
            self.restore_button,
            self.combined_button,
            self.trash_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)
        table_frame = QFrame()
        table_frame.setObjectName("dashboardTableCard")
        table_layout = QVBoxLayout(table_frame)
        self.table = QTableView()
        self.table.setObjectName("dashboardTable")
        self.table.setAccessibleName("시험 채점 기록 목록")
        self.table.setModel(DashboardTableModel(self.table))
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.model.modelReset.connect(self._install_row_actions)
        self.model.dataChanged.connect(lambda *_: self._refresh_state())
        self.table.setSortingEnabled(True)
        self.table.clicked.connect(self._toggle_clicked_row)
        self.table.doubleClicked.connect(self._open_detail)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._refresh_state())
        self.table.setMinimumHeight(300)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(72)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.resizeSection(2, 180)
        table_layout.addWidget(self.table)
        root.addWidget(table_frame)
        row_actions = QHBoxLayout()
        row_actions.addStretch()
        self.detail_button = self._button("상세 보기", "dashboardDetailButton", "detail")
        self.delete_button = self._button("삭제", "dashboardDeleteButton", "delete")
        row_actions.addWidget(self.detail_button)
        row_actions.addWidget(self.delete_button)
        root.addLayout(row_actions)
        root.addStretch()
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)

    def _button(self, text: str, name: str, action: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setAccessibleName(text)
        button.clicked.connect(lambda: self._request(action))
        return button

    def _toggle_clicked_row(self, index: QModelIndex) -> None:
        if not index.isValid() or index.column() == 6:
            return
        if index.column() != 0:
            selection_index = self.model.index(index.row(), 0)
            checked = (
                self.model.data(selection_index, Qt.ItemDataRole.CheckStateRole)
                == Qt.CheckState.Checked
            )
            self.model.setData(
                selection_index,
                Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole,
            )
        self._refresh_state()

    def _install_row_actions(self) -> None:
        for row in range(self.model.rowCount()):
            cell = QWidget(self.table)
            cell.setObjectName("dashboardActionCell")
            layout = QHBoxLayout(cell)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(4)
            for text, name, action in (
                ("상세 보기", "dashboardDetailButton", "detail"),
                ("삭제", "dashboardDeleteButton", "delete"),
            ):
                button = QPushButton(text, cell)
                button.setObjectName(name)
                button.setAccessibleName(text)
                button.clicked.connect(
                    lambda _checked=False, action=action, row=row: self._request_row(
                        action, row
                    )
                )
                layout.addWidget(button)
            self.table.setIndexWidget(self.model.index(row, 6), cell)

    def _request_row(self, action: str, row: int) -> None:
        if not 0 <= row < self.model.rowCount():
            return
        self.table.selectRow(row)
        self._request(action)

    @property
    def model(self) -> DashboardTableModel:
        model = self.table.model()
        if not isinstance(model, DashboardTableModel):
            raise RuntimeError("dashboard table model is unavailable")
        return model

    def set_entries(self, entries: tuple[DashboardIndexEntry, ...]) -> None:
        current_entry = self._current_entry()
        selected_id = current_entry.session_id if current_entry is not None else None
        self.model.set_entries(entries)
        years = sorted(
            {
                int(entry.graded_at[:4])
                for entry in entries
                if entry.graded_at is not None and entry.graded_at[:4].isdigit()
            },
            reverse=True,
        )
        current = self.year_combo.currentData()
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        self.year_combo.addItem("연도 전체", None)
        for year in years:
            self.year_combo.addItem(f"{year}년", year)
        index = self.year_combo.findData(current)
        self.year_combo.setCurrentIndex(max(index, 0))
        self.year_combo.blockSignals(False)
        self._restore_current_id(selected_id)
        self._refresh_state()

    def set_write_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        self._write_enabled = enabled
        self._refresh_state()

    def set_busy(self, busy: bool) -> None:
        if type(busy) is not bool:
            raise TypeError("busy must be bool")
        self._busy = busy
        self._refresh_state()

    def _apply_filters(self) -> None:
        self.model.set_filters(self.search_edit.text(), self.year_combo.currentData(), None)
        self._refresh_state()

    def _current_entry(self) -> DashboardIndexEntry | None:
        return self.model.entry_at(self.table.currentIndex().row())

    def _restore_current_id(self, session_id: str | None) -> None:
        if session_id is None:
            return
        for row in range(self.model.rowCount()):
            entry = self.model.entry_at(row)
            if entry is not None and entry.session_id == session_id:
                self.table.selectRow(row)
                break

    def _selection(self) -> DashboardSelection | None:
        checked = self.model.selection()
        if checked is not None:
            return checked
        entry = self._current_entry()
        if entry is None:
            return None
        return DashboardSelection((entry.session_id,), (entry.revision,))

    def _request(self, action: str) -> None:
        if action not in _DASHBOARD_ACTIONS:
            raise ValueError("unsupported dashboard action")
        if self._busy or (
            action in {"delete", "backup", "restore", "combined"} and not self._write_enabled
        ):
            return
        if action in _GLOBAL_DASHBOARD_ACTIONS:
            self.request_emitted.emit(DashboardGlobalRequest(action))
            return
        selection = self._selection()
        if action == "detail":
            entry = self._current_entry()
            if entry is None:
                return
            selection = DashboardSelection((entry.session_id,), (entry.revision,))
        if action == "delete":
            entry = self._current_entry()
            if (
                entry is None
                or QMessageBox.question(
                    self,
                    "삭제 확인",
                    f"'{entry.exam_name}' 시험을 휴지통으로 이동하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            selection = DashboardSelection((entry.session_id,), (entry.revision,))
        if selection is None:
            return
        if action == "backup" and len(selection.session_ids) != 1:
            return
        self.request_emitted.emit(DashboardRequest(action, selection))

    def _open_detail(self, index: QModelIndex) -> None:
        if not self._busy:
            self._request("detail")

    def _refresh_state(self) -> None:
        writable = self._write_enabled and not self._busy
        one = self._current_entry() is not None
        selection = self._selection()
        selected_count = 0 if selection is None else len(selection.session_ids)
        self.detail_button.setEnabled(not self._busy and one)
        self.delete_button.setEnabled(writable and one)
        self.backup_button.setEnabled(writable and selected_count == 1)
        self.combined_button.setEnabled(writable and selected_count > 0)
        self.restore_button.setEnabled(writable)
        self.trash_button.setEnabled(not self._busy)

    def create_trash_dialog(self, entries: tuple[DashboardIndexEntry, ...]) -> TrashDialog:
        dialog = TrashDialog(self)
        dialog.set_entries(entries)
        dialog.set_write_enabled(self._write_enabled)
        dialog.set_busy(self._busy)
        dialog.request_emitted.connect(self._forward_trash)
        return dialog

    def _forward_trash(self, request: TrashRequest) -> None:
        if not isinstance(request, TrashRequest):
            raise TypeError("request must be TrashRequest")
        if self._busy or (
            request.action in {"restore", "delete"} and not self._write_enabled
        ):
            return
        wrapped = DashboardRequest(
            f"trash_{request.action}", DashboardSelection(request.session_ids, request.revisions)
        )
        self.request_emitted.emit(wrapped)


__all__ = ["DashboardGlobalRequest", "DashboardPage", "DashboardRequest"]
