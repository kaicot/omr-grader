"""Immutable dashboard index presentation model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from unicodedata import normalize

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.models import DashboardIndexEntry

_INVALID_INDEX = QModelIndex()

COLUMN_SELECTION = 0
COLUMN_EXAM_NAME = 1
COLUMN_GRADED_AT = 2
COLUMN_PARTICIPANTS = 3
COLUMN_AVERAGE = 4
COLUMN_HIGH_LOW = 5
COLUMN_MANAGEMENT = 6
HEADERS = ("선택", "시험명", "채점일시", "응시인원", "평균점수", "최고/최저점", "관리")


def korean_search_key(value: str) -> str:
    """Return a deterministic, whitespace-insensitive Korean search key."""
    return "".join(normalize("NFKC", value).casefold().split())


def _term_label(term: ExamTerm) -> str:
    return {
        ExamTerm.FIRST: "1학기",
        ExamTerm.SECOND: "2학기",
        ExamTerm.SUMMER: "여름학기",
        ExamTerm.WINTER: "겨울학기",
        ExamTerm.OTHER: "기타",
        ExamTerm.UNSPECIFIED: "미지정",
    }[term]


def _timestamp(value: str | None) -> str:
    if value is None:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


@dataclass(frozen=True, slots=True)
class DashboardSelection:
    """Value-only selection passed from the view to its controller."""

    session_ids: tuple[str, ...]
    revisions: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.session_ids) is not tuple or type(self.revisions) is not tuple:
            raise TypeError("session_ids and revisions must be tuples")
        if len(self.session_ids) != len(self.revisions):
            raise ValueError("session_ids and revisions must have equal cardinality")
        if not self.session_ids:
            raise ValueError("session_ids must not be empty")
        if any(type(session_id) is not str for session_id in self.session_ids):
            raise TypeError("session_ids must contain str values")
        if any(not session_id for session_id in self.session_ids):
            raise ValueError("session_ids must not contain empty values")
        if len(set(self.session_ids)) != len(self.session_ids):
            raise ValueError("session_ids must be unique")
        if any(type(revision) is not int for revision in self.revisions):
            raise TypeError("revisions must contain int values")
        if any(revision < 0 for revision in self.revisions):
            raise ValueError("revisions must be nonnegative")

class DashboardTableModel(QAbstractTableModel):
    """A filtered, stable-sorted table over immutable dashboard entries."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: tuple[DashboardIndexEntry, ...] = ()
        self._visible: tuple[DashboardIndexEntry, ...] = ()
        self._checked_ids: frozenset[str] = frozenset()
        self._search = ""
        self._year: int | None = None
        self._term: ExamTerm | None = None
        self._sort_column = COLUMN_GRADED_AT
        self._sort_order = Qt.SortOrder.DescendingOrder

    def rowCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return None

    def data(  # noqa: N802
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if not index.isValid() or not 0 <= index.row() < len(self._visible):
            return None
        entry = self._visible[index.row()]
        column = index.column()
        if column == COLUMN_SELECTION and role == Qt.ItemDataRole.CheckStateRole:
            return (
                Qt.CheckState.Checked
                if entry.session_id in self._checked_ids
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                "",
                entry.exam_name,
                _timestamp(entry.graded_at),
                f"{entry.participant_count}명",
                "-" if entry.average_score is None else f"{entry.average_score}점",
                "-"
                if entry.highest_score is None or entry.lowest_score is None
                else f"{entry.highest_score} / {entry.lowest_score}점",
                "",
            )
            return values[column]
        if role == Qt.ItemDataRole.ToolTipRole:
            values = (
                "클릭하여 시험을 선택하거나 선택 해제합니다.",
                entry.exam_name,
                _timestamp(entry.graded_at),
                f"{entry.participant_count}명",
                "-" if entry.average_score is None else f"{entry.average_score}점",
                "-"
                if entry.highest_score is None or entry.lowest_score is None
                else f"{entry.highest_score} / {entry.lowest_score}점",
                f"{entry.exam_name} 상세 보기 또는 삭제",
            )
            return values[column]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{HEADERS[column]}: {self.data(index, Qt.ItemDataRole.DisplayRole)}"
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == COLUMN_SELECTION:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(  # noqa: N802
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            not index.isValid()
            or index.column() != COLUMN_SELECTION
            or role != Qt.ItemDataRole.CheckStateRole
        ):
            return False
        session_id = self._visible[index.row()].session_id
        if isinstance(value, Qt.CheckState):
            state = value
        elif isinstance(value, int):
            try:
                state = Qt.CheckState(value)
            except ValueError:
                return False
        else:
            return False
        checked = set(self._checked_ids)
        if state == Qt.CheckState.Checked:
            checked.add(session_id)
        else:
            checked.discard(session_id)
        self._checked_ids = frozenset(checked)
        model_index = self.index(index.row(), index.column())
        self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def set_entries(self, entries: tuple[DashboardIndexEntry, ...]) -> None:
        if not isinstance(entries, tuple) or not all(
            isinstance(item, DashboardIndexEntry) for item in entries
        ):
            raise TypeError("entries must be a tuple of DashboardIndexEntry values")
        self._entries = entries
        self._checked_ids = frozenset(
            item.session_id for item in entries if item.session_id in self._checked_ids
        )
        self._rebuild()

    def set_filters(
        self, search: str = "", year: int | None = None, term: ExamTerm | str | None = None
    ) -> None:
        if not isinstance(search, str) or (year is not None and type(year) is not int):
            raise TypeError("search must be str and year must be int or None")
        self._search, self._year = korean_search_key(search), year
        self._term = None if term is None or term == "" else ExamTerm(term)
        self._rebuild()

    def entry_at(self, row: int) -> DashboardIndexEntry | None:
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def selection(self) -> DashboardSelection | None:
        selected = tuple(item for item in self._entries if item.session_id in self._checked_ids)
        if not selected:
            return None
        return DashboardSelection(
            tuple(item.session_id for item in selected), tuple(item.revision for item in selected)
        )

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if 0 <= column < len(HEADERS):
            self._sort_column, self._sort_order = column, order
            self._rebuild()

    def _rebuild(self) -> None:
        shown = [item for item in self._entries if self._matches(item)]
        shown.sort(key=self._sort_key, reverse=self._sort_order == Qt.SortOrder.DescendingOrder)
        self.beginResetModel()
        self._visible = tuple(shown)
        self.endResetModel()

    def _matches(self, item: DashboardIndexEntry) -> bool:
        return (
            (not self._search or self._search in korean_search_key(item.exam_name))
            and (
                self._year is None
                or (
                    item.graded_at is not None
                    and item.graded_at[:4].isdigit()
                    and int(item.graded_at[:4]) == self._year
                )
            )
            and (self._term is None or item.exam_term == self._term)
        )

    def _sort_key(self, item: DashboardIndexEntry) -> Any:
        if self._sort_column == COLUMN_EXAM_NAME:
            return korean_search_key(item.exam_name)
        if self._sort_column == COLUMN_GRADED_AT:
            return item.graded_at or ""
        if self._sort_column == COLUMN_PARTICIPANTS:
            return item.participant_count
        if self._sort_column == COLUMN_AVERAGE:
            return Decimal(item.average_score) if item.average_score is not None else Decimal("-1")
        return item.session_id


__all__ = ["DashboardSelection", "DashboardTableModel", "HEADERS", "korean_search_key"]
