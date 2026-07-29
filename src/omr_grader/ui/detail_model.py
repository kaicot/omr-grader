"""Table model for the value-only detail result display."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from omr_grader.application.detail_presenter import DetailStudentDisplay

_INVALID_INDEX = QModelIndex()


class DetailTableModel(QAbstractTableModel):
    _fixed_headers = ("석차", "학번", "이름", "점수")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._students: tuple[DetailStudentDisplay, ...] = ()
        self._question_numbers: tuple[int, ...] = ()

    def set_students(self, students: tuple[DetailStudentDisplay, ...]) -> None:
        if not isinstance(students, tuple):
            raise TypeError("students must be tuple")
        numbers = tuple(sorted({a.question for student in students for a in student.answers}))
        self.beginResetModel()
        self._students, self._question_numbers = students, numbers
        self.endResetModel()

    def rowCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._students)

    def columnCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._fixed_headers) + len(self._question_numbers)

    def headerData(  # noqa: N802
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

    def data(  # noqa: N802
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.TextAlignmentRole,
        ):
            return None
        student = self._students[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignCenter)
        if col == 0:
            return "-" if student.rank is None else str(student.rank)
        if col == 1:
            return student.student_id
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
