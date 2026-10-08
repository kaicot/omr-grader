"""Value-only trash management dialog."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.ui.dashboard_model import status_text, timestamp_text

TRASH_COLUMNS = ("시험 폴더", "상태", "응시 인원", "채점일시")


@dataclass(frozen=True, slots=True)
class TrashRequest:
    action: str
    session_ids: tuple[str, ...]
    revisions: tuple[int, ...]


class TrashDialog(QDialog):
    """Controller-owned trash view; it never reads, writes, or removes files itself."""

    request_emitted = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("trashDialog")
        self.setWindowTitle("휴지통 관리")
        self.setAccessibleName("휴지통 관리")
        self.setMinimumSize(640, 360)
        self.resize(780, 460)
        self._entries: tuple[DashboardIndexEntry, ...] = ()
        self._write_enabled = True
        self._busy = False
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)
        title = QLabel("삭제한 시험")
        title.setObjectName("trashDialogTitle")
        root.addWidget(title)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("trashSummary")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(TRASH_COLUMNS))
        self.table.setObjectName("trashTable")
        self.table.setAccessibleName("삭제한 시험 목록")
        self.table.setHorizontalHeaderLabels(list(TRASH_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 120), (2, 90), (3, 160)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(column, width)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.restore_button = QPushButton("복원")
        self.restore_button.setObjectName("trashRestoreButton")
        self.restore_button.setToolTip("고른 시험을 시험 관리 목록으로 되돌립니다.")
        self.delete_button = QPushButton("영구 삭제")
        self.delete_button.setObjectName("trashPermanentDeleteButton")
        self.delete_button.setToolTip("고른 시험을 지웁니다. 되돌릴 수 없습니다.")
        self.empty_button = QPushButton("휴지통 비우기")
        self.empty_button.setObjectName("trashEmptyButton")
        self.empty_button.setToolTip("휴지통의 모든 시험을 지웁니다. 되돌릴 수 없습니다.")
        self.close_button = QPushButton("닫기")
        self.close_button.setObjectName("trashCloseButton")
        # Restore sits apart from the two buttons that cannot be undone.
        actions.addWidget(self.restore_button)
        actions.addStretch(1)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.empty_button)
        actions.addSpacing(12)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self.restore_button.clicked.connect(lambda: self._request("restore", True))
        self.delete_button.clicked.connect(lambda: self._request("permanent_delete", True))
        self.empty_button.clicked.connect(lambda: self._request("empty", True))
        self.close_button.clicked.connect(self.reject)
        self.table.itemSelectionChanged.connect(self._refresh)
        self._refresh()

    def set_entries(self, entries: tuple[DashboardIndexEntry, ...]) -> None:
        if not isinstance(entries, tuple) or not all(
            isinstance(entry, DashboardIndexEntry) for entry in entries
        ):
            raise TypeError("entries must be a tuple of DashboardIndexEntry values")
        self._entries = entries
        self.table.clearContents()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            # The folder name starts with the date, so old exams are easy to tell apart.
            values = (
                entry.display_folder,
                status_text(entry),
                f"{entry.participant_count}명",
                timestamp_text(entry.graded_at),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, entry.session_id)
                item.setToolTip(value)
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self._refresh()

    def set_write_enabled(self, enabled: bool) -> None:
        self._write_enabled = bool(enabled)
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._refresh()

    def select_rows(self, *rows: int) -> None:
        """Select whole rows, as a click or Ctrl+click would."""
        self.table.clearSelection()
        mode = self.table.selectionMode()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for row in rows:
            self.table.selectRow(row)
        self.table.setSelectionMode(mode)

    def _selected(self) -> tuple[DashboardIndexEntry, ...]:
        ids = {
            data
            for item in self.table.selectedItems()
            if isinstance((data := item.data(Qt.ItemDataRole.UserRole)), str)
        }
        return tuple(entry for entry in self._entries if entry.session_id in ids)

    def _confirmation(self, action: str, count: int) -> tuple[str, str]:
        if action == "restore":
            return "복원", f"선택한 시험 {count}개를 시험 관리 목록으로 복원합니다."
        if action == "empty":
            return (
                "휴지통 비우기",
                f"휴지통의 시험 {count}개를 모두 영구 삭제합니다.\n이 작업은 되돌릴 수 없습니다.",
            )
        return (
            "영구 삭제",
            f"선택한 시험 {count}개를 영구 삭제합니다.\n이 작업은 되돌릴 수 없습니다.",
        )

    def _request(self, action: str, confirm: bool) -> None:
        if not self._write_enabled or self._busy:
            return
        entries = self._entries if action == "empty" else self._selected()
        if not entries:
            return
        if confirm:
            title, message = self._confirmation(action, len(entries))
            if (
                QMessageBox.question(
                    self,
                    title,
                    message,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        self.request_emitted.emit(
            TrashRequest(
                action,
                tuple(item.session_id for item in entries),
                tuple(item.revision for item in entries),
            )
        )
        # The list is now out of date; the dashboard reloads and the trash can be reopened.
        self.accept()

    def _refresh(self) -> None:
        selected = self._selected()
        enabled = self._write_enabled and not self._busy
        self.restore_button.setEnabled(enabled and bool(selected))
        self.delete_button.setEnabled(enabled and bool(selected))
        self.empty_button.setEnabled(enabled and bool(self._entries))
        if not self._entries:
            summary = "휴지통이 비어 있습니다."
        else:
            summary = (
                f"삭제한 시험 {len(self._entries)}개 · 복원하면 시험 관리 목록으로 돌아갑니다."
            )
            if selected:
                summary += f" ({len(selected)}개 선택)"
            if not self._write_enabled:
                summary += " 읽기 전용으로 실행 중이라 복원하거나 지울 수 없습니다."
        self.summary_label.setText(summary)


__all__ = ["TRASH_COLUMNS", "TrashDialog", "TrashRequest"]
