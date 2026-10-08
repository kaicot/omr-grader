"""Help window: table of contents on the left, the manual on the right, a search box on top."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from omr_grader.ui.help_content import SECTIONS


class HelpDialog(QDialog):
    """The manual; the window keeps no state beyond the shown document."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("helpDialog")
        self.setWindowTitle("OMR Grader 도움말 / 사용 설명서")
        self.setMinimumSize(760, 560)
        self.resize(1020, 720)
        self.setModal(False)
        self._section_starts: list[tuple[int, int]] = []
        self._syncing = False

        root = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("helpSearch")
        self.search_edit.setPlaceholderText("설명서에서 찾기 (예: 전원, 학번, 휴지통)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("설명서 검색")
        self.search_button = QPushButton("다음 찾기")
        self.search_button.setObjectName("helpSearchButton")
        self.search_status = QLabel()
        self.search_status.setObjectName("helpSearchStatus")
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.search_status)
        root.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.toc = QListWidget()
        self.toc.setObjectName("helpToc")
        self.toc.setAccessibleName("설명서 목차")
        for section in SECTIONS:
            self.toc.addItem(section.title)
            item = self.toc.item(self.toc.count() - 1)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, section.key)
        self.browser = QTextBrowser()
        self.browser.setObjectName("helpBrowser")
        self.browser.setOpenExternalLinks(True)
        self.browser.setOpenLinks(True)
        splitter.addWidget(self.toc)
        splitter.addWidget(self.browser)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 790])
        root.addWidget(splitter, 1)
        buttons = QDialogButtonBox(self)
        close = buttons.addButton("닫기", QDialogButtonBox.ButtonRole.RejectRole)
        close.setObjectName("helpCloseButton")
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)

        self.toc.currentRowChanged.connect(self._toc_chosen)
        self.search_edit.textChanged.connect(lambda _: self.find_next(from_start=True))
        self.search_edit.returnPressed.connect(self.find_next)
        self.search_button.clicked.connect(lambda: self.find_next())
        self.browser.verticalScrollBar().valueChanged.connect(self._sync_toc)

    def set_document(self, html: str, stylesheet: str) -> None:
        """Show the manual, keeping the reader's place when only the theme changed."""
        position = self.browser.verticalScrollBar().value()
        self.browser.document().setDefaultStyleSheet(stylesheet)
        self.browser.setHtml(html)
        self._section_starts = self._find_section_starts()
        self.browser.verticalScrollBar().setValue(position)

    def _find_section_starts(self) -> list[tuple[int, int]]:
        """Character positions of each section heading, in TOC order."""
        titles = {section.title: index for index, section in enumerate(SECTIONS)}
        starts: list[tuple[int, int]] = []
        block = self.browser.document().begin()
        while block.isValid():
            index = titles.get(block.text().strip())
            if index is not None and block.blockFormat().headingLevel() == 2:
                starts.append((block.position(), index))
            block = block.next()
        return sorted(starts)

    def show_section(self, key: str) -> None:
        for row in range(self.toc.count()):
            item = self.toc.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                self._syncing = True
                self.toc.setCurrentRow(row)
                self._syncing = False
                break
        self.browser.scrollToAnchor(key)

    def _toc_chosen(self, row: int) -> None:
        if self._syncing or not 0 <= row < len(SECTIONS):
            return
        self.browser.scrollToAnchor(SECTIONS[row].key)

    def _sync_toc(self) -> None:
        if not self._section_starts:
            return
        position = self.browser.cursorForPosition(self.browser.viewport().rect().topLeft()).position()
        row = 0
        for start, index in self._section_starts:
            if start <= position + 1:
                row = index
        if row != self.toc.currentRow():
            self._syncing = True
            self.toc.setCurrentRow(row)
            self._syncing = False

    def find_next(self, from_start: bool = False) -> bool:
        """Select the next match of the search text, wrapping to the top once."""
        text = self.search_edit.text().strip()
        if not text:
            self.search_status.setText("")
            return False
        if from_start:
            self.browser.moveCursor(QTextCursor.MoveOperation.Start)
        found = self.browser.find(text, QTextDocument.FindFlag(0))
        if not found:
            self.browser.moveCursor(QTextCursor.MoveOperation.Start)
            found = self.browser.find(text, QTextDocument.FindFlag(0))
        self.search_status.setText("" if found else "찾는 말이 없습니다")
        return found


__all__ = ["HelpDialog"]
