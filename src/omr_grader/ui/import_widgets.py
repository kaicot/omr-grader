"""Value-only file import controls used by the scan workflow.

The widgets deliberately do not inspect paths or open dropped files.  They only
validate the Qt drop payload and its declared filename suffix; controllers hand
validated selections to background-capable application services.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class ImportKind(StrEnum):
    SOURCE = "source"
    FOLDER = "folder"
    PDF = "pdf"
    ROSTER = "roster"
    PROFILE = "profile"
    ANSWER_KEY = "answer_key"


@dataclass(frozen=True, slots=True)
class ImportSelection:
    """A path selection passed across the UI boundary without file access."""

    kind: ImportKind
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ImportKind):
            raise TypeError("kind must be an ImportKind")
        if (
            not isinstance(self.paths, tuple)
            or not self.paths
            or any(not isinstance(path, str) or not path for path in self.paths)
        ):
            raise ValueError("paths must be a non-empty immutable tuple of strings")


class ImportDropWidget(QFrame):
    """Clickable, drop-enabled selector that performs only structural checks."""

    selection_changed = Signal(object)
    browse_requested = Signal(object)
    rejected = Signal(str)

    _TEXT = {
        ImportKind.SOURCE: (
            "이미지(JPG, PNG) 폴더나 PDF 파일을 이곳에 끌어놓거나 위 버튼으로 선택해주세요."
        ),
        ImportKind.FOLDER: "스캔된 이미지 폴더를 이곳에 드래그하거나 클릭하여 선택하세요.",
        ImportKind.PDF: "스캔된 PDF 파일을 이곳에 드래그하거나 클릭하여 선택하세요.",
        ImportKind.ROSTER: "응시 학생 명단 엑셀 파일을 이곳에 드래그하거나 클릭하여 선택하세요.",
        ImportKind.PROFILE: "OMR 프로필 파일을 이곳에 드래그하거나 클릭하여 불러오세요.",
        ImportKind.ANSWER_KEY: "정답표 엑셀 파일을 이곳에 끌어놓거나 클릭하여 선택하세요.",
    }

    def __init__(self, kind: ImportKind = ImportKind.FOLDER, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = ImportKind(kind)
        self._selection: ImportSelection | None = None
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("importDropWidget")
        self.setAccessibleName("스캔 파일 선택")
        self.setAccessibleDescription("파일 또는 폴더를 드래그하거나 클릭하여 선택합니다.")
        self.setProperty("importState", "empty")
        self.setProperty("dragActive", False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        self._title = QLabel("파일 또는 폴더 선택", self)
        self._title.setObjectName("importDropTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail = QLabel(self)
        self._detail.setObjectName("importDropDetail")
        self._detail.setWordWrap(True)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)
        layout.addWidget(self._detail)
        self.set_kind(self._kind)

    @property
    def kind(self) -> ImportKind:
        return self._kind

    @property
    def selection(self) -> ImportSelection | None:
        return self._selection

    def set_kind(self, kind: ImportKind) -> None:
        self._kind = ImportKind(kind)
        self._selection = None
        self._title.setText(
            "스캔 파일 또는 폴더 선택"
            if self._kind is ImportKind.SOURCE
            else "이미지 폴더 선택"
            if self._kind is ImportKind.FOLDER
            else "PDF 파일 선택"
            if self._kind is ImportKind.PDF
            else "명단 파일 선택"
            if self._kind is ImportKind.ROSTER
            else "정답표 파일 선택"
            if self._kind is ImportKind.ANSWER_KEY
            else "OMR 프로필 선택"
        )
        self._detail.setText(self._TEXT[self._kind])
        self.setAccessibleName(
            "이미지 폴더 또는 PDF 파일 선택"
            if self._kind is ImportKind.SOURCE
            else "이미지 폴더 선택"
            if self._kind is ImportKind.FOLDER
            else "PDF 파일 선택"
            if self._kind is ImportKind.PDF
            else "응시 학생 명단 엑셀 선택"
            if self._kind is ImportKind.ROSTER
            else "정답표 엑셀 선택"
            if self._kind is ImportKind.ANSWER_KEY
            else "OMR 프로필 파일 선택"
        )
        self.setProperty("importState", "empty")
        self.setProperty("pickerState", "ready")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_selection(self, paths: tuple[str, ...] | list[str]) -> bool:
        values = tuple(paths)
        if not self._paths_are_structurally_valid(values):
            self._reject_for_kind()
            return False
        selected_kind = (
            ImportKind.PDF
            if self._kind is ImportKind.SOURCE and values[0].casefold().endswith(".pdf")
            else ImportKind.FOLDER
            if self._kind is ImportKind.SOURCE
            else self._kind
        )
        self._selection = ImportSelection(selected_kind, values)
        count = len(values)
        noun = "개 항목" if self._kind is ImportKind.FOLDER else "개 파일"
        self._detail.setText(f"선택됨: {values[0]} ({count}{noun})")
        self.setProperty("importState", "selected")
        self.setProperty("pickerState", "ready")
        self.style().unpolish(self)
        self.style().polish(self)
        self.selection_changed.emit(self._selection)
        return True

    def clear(self) -> None:
        self._selection = None
        self._detail.setText(self._TEXT[self._kind])
        self.setProperty("importState", "empty")
        self.setProperty("pickerState", "ready")

    def set_picker_cancelled(self) -> None:
        """Record a controller-reported picker cancellation without changing input."""
        self.setProperty("pickerState", "cancelled")
        self._detail.setText(
            "선택이 취소되었습니다. 기존 선택을 유지합니다."
            if self._selection is not None
            else "선택이 취소되었습니다."
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.browse_requested.emit(self._kind)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.isEnabled() and self._mime_is_acceptable(event.mimeData()):
            self._set_drag_active(True)
            event.acceptProposedAction()
        else:
            self._set_drag_active(False)
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self.isEnabled() and self._mime_is_acceptable(event.mimeData()):
            event.acceptProposedAction()
        else:
            self._set_drag_active(False)
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        if not self.isEnabled():
            event.ignore()
            return
        mime = event.mimeData()
        if not self._mime_is_acceptable(mime):
            self._reject_for_kind()
            event.ignore()
            return
        paths = tuple(url.toLocalFile() for url in mime.urls())
        if self.set_selection(paths):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def _mime_is_acceptable(self, mime: QMimeData) -> bool:
        if not mime.hasUrls():
            return False
        urls = mime.urls()
        if not urls or any(not url.isLocalFile() for url in urls):
            return False
        return self._paths_are_structurally_valid(tuple(url.toLocalFile() for url in urls))

    def _paths_are_structurally_valid(self, paths: tuple[str, ...]) -> bool:
        if not paths or any(not isinstance(path, str) or not path for path in paths):
            return False
        lowered = tuple(path.casefold() for path in paths)
        if self._kind in (ImportKind.SOURCE, ImportKind.FOLDER):
            # Qt has no trustworthy directory MIME marker.  Accept one local URL
            # as a folder candidate and leave existence/type validation to a worker.
            return len(paths) == 1
        if self._kind is ImportKind.PDF:
            return all(path.endswith(".pdf") for path in lowered)
        if self._kind is ImportKind.ROSTER:
            return len(paths) == 1 and lowered[0].endswith((".xlsx", ".xlsm"))
        if self._kind is ImportKind.ANSWER_KEY:
            return len(paths) == 1 and lowered[0].endswith(".xlsx")
        return len(paths) == 1 and lowered[0].endswith(".omrtemplate")

    def _reject_for_kind(self) -> None:
        messages = {
            ImportKind.SOURCE: "이미지 폴더 하나 또는 PDF 파일 하나만 선택할 수 있습니다.",
            ImportKind.FOLDER: "이미지 폴더 하나만 선택할 수 있습니다.",
            ImportKind.PDF: "PDF 파일만 선택할 수 있습니다.",
            ImportKind.ROSTER: "명단 엑셀 파일(.xlsx 또는 .xlsm)만 선택할 수 있습니다.",
            ImportKind.PROFILE: "OMR 프로필 파일(.omrtemplate)만 선택할 수 있습니다.",
            ImportKind.ANSWER_KEY: "정답표 엑셀 파일(.xlsx) 하나만 선택할 수 있습니다.",
        }
        self.rejected.emit(messages[self._kind])
