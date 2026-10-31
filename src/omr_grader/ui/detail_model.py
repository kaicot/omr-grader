"""Table model for the value-only detail result display."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor

from omr_grader.application.detail_presenter import DetailStudentDisplay

_INVALID_INDEX = QModelIndex()
# Rows whose student ID needs a look (unreadable or not on the roster), and fixed rows.
_CHECK_BACKGROUND = QColor("#FFEDD5")
_CHECK_TEXT = QColor("#7C2D12")
# Green, not blue: a fixed row must not look like the selected row.
_FIXED_BACKGROUND = QColor("#DCFCE7")
_FIXED_TEXT = QColor("#14532D")


def needs_id_check(student: DetailStudentDisplay, roster_ids: frozenset[str]) -> bool:
    """An ID nobody can match: unread, or missing from the exam's roster."""
    if student.fixed_from is not None:
        return False
    return not student.student_id or bool(roster_ids and student.student_id not in roster_ids)


class DetailTableModel(QAbstractTableModel):
    _fixed_headers = ("석차", "학번", "이름", "점수")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._students: tuple[DetailStudentDisplay, ...] = ()
        self._question_numbers: tuple[int, ...] = ()
        self._roster_ids: frozenset[str] = frozenset()

    def set_students(
        self,
        students: tuple[DetailStudentDisplay, ...],
        roster_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(students, tuple):
            raise TypeError("students must be tuple")
        numbers = tuple(sorted({a.question for student in students for a in student.answers}))
        self.beginResetModel()
        self._students, self._question_numbers = students, numbers
        self._roster_ids = roster_ids
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._students)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._fixed_headers) + len(self._question_numbers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return (
                self._fixed_headers[section]
                if section < 4
                else f"Q{self._question_numbers[section - 4]}"
            )
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.TextAlignmentRole,
            Qt.ItemDataRole.BackgroundRole,
            Qt.ItemDataRole.ForegroundRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        student = self._students[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignCenter)
        check = needs_id_check(student, self._roster_ids)
        fixed = student.fixed_from is not None
        if role == Qt.ItemDataRole.BackgroundRole:
            return _CHECK_BACKGROUND if check else _FIXED_BACKGROUND if fixed else None
        if role == Qt.ItemDataRole.ForegroundRole:
            return _CHECK_TEXT if check else _FIXED_TEXT if fixed else None
        if role == Qt.ItemDataRole.ToolTipRole:
            if check:
                return "학번을 확인하세요: 읽지 못했거나 명단에 없는 학번입니다."
            if fixed:
                return f"고친 학번입니다. 원래 {student.fixed_from or '판독 불가'}"
            return None
        if col == 0:
            return "-" if student.rank is None else str(student.rank)
        if col == 1:
            if fixed:
                return f"{student.student_id} (수정)"
            return student.student_id or "읽지 못함"
        if col == 2:
            return student.name
        if col == 3:
            return student.score
        answer = next(
            (a for a in student.answers if a.question == self._question_numbers[col - 4]), None
        )
        if answer is None or answer.correct is None:
            return "-"
        return "O" if answer.correct else "X"

    def student_at(self, row: int) -> DetailStudentDisplay | None:
        return self._students[row] if 0 <= row < len(self._students) else None
