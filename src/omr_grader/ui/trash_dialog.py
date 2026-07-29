"""Value-only trash management dialog."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omr_grader.domain.models import DashboardIndexEntry


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
        self._entries: tuple[DashboardIndexEntry, ...] = ()
        self._write_enabled = True
        self._busy = False
        root = QVBoxLayout(self)
        title = QLabel("삭제한 시험")
        title.setObjectName("trashDialogTitle")
        root.addWidget(title)
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("trashList")
        self.list_widget.setAccessibleName("삭제한 시험 목록")
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        root.addWidget(self.list_widget)
        actions = QHBoxLayout()
        self.restore_button = QPushButton("복원")
        self.restore_button.setObjectName("trashRestoreButton")
        self.delete_button = QPushButton("영구 삭제")
        self.delete_button.setObjectName("trashPermanentDeleteButton")
        self.empty_button = QPushButton("휴지통 비우기")
        self.empty_button.setObjectName("trashEmptyButton")
        for button in (self.restore_button, self.delete_button, self.empty_button):
            actions.addWidget(button)
        root.addLayout(actions)
        self.restore_button.clicked.connect(lambda: self._request("restore", True))
        self.delete_button.clicked.connect(lambda: self._request("permanent_delete", True))
        self.empty_button.clicked.connect(lambda: self._request("empty", True))
        self.list_widget.itemSelectionChanged.connect(self._refresh)
        self._refresh()

    def set_entries(self, entries: tuple[DashboardIndexEntry, ...]) -> None:
        if not isinstance(entries, tuple) or not all(
            isinstance(entry, DashboardIndexEntry) for entry in entries
        ):
            raise TypeError("entries must be a tuple of DashboardIndexEntry values")
        self._entries = entries
        self.list_widget.clear()
        for entry in entries:
            item_text = f"{entry.exam_name} ({entry.exam_year or '-'}년)"
            self.list_widget.addItem(item_text)
            item = self.list_widget.item(self.list_widget.count() - 1)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, entry.session_id)
        self._refresh()

    def set_write_enabled(self, enabled: bool) -> None:
        self._write_enabled = bool(enabled)
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._refresh()

    def _selected(self) -> tuple[DashboardIndexEntry, ...]:
        ids = {
            data
            for item in self.list_widget.selectedItems()
            if isinstance((data := item.data(Qt.ItemDataRole.UserRole)), str)
        }
        return tuple(entry for entry in self._entries if entry.session_id in ids)

    def _request(self, action: str, confirm: bool) -> None:
        if not self._write_enabled or self._busy:
            return
        entries = self._entries if action == "empty" else self._selected()
        if not entries:
            return
        if confirm:
            message = (
                "선택한 시험을 복원하시겠습니까?"
                if action == "restore"
                else "선택한 시험을 영구 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다."
            )
            if (
                QMessageBox.question(
                    self,
                    "확인",
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

    def _refresh(self) -> None:
        selected = bool(self._selected())
        enabled = self._write_enabled and not self._busy
        self.restore_button.setEnabled(enabled and selected)
        self.delete_button.setEnabled(enabled and selected)
        self.empty_button.setEnabled(enabled and bool(self._entries))


__all__ = ["TrashDialog", "TrashRequest"]
