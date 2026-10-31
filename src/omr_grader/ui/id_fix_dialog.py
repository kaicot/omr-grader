"""'학번 고치기': set the student ID of one answer sheet whose ID bubbles were marked wrong.

The dialog shows the top-left of the sheet (handwritten name and ID above the marked ID
grid), so the teacher compares what the student wrote with what they marked, then types the
right ID or picks the student from the roster.  It returns a value; the controller commits.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from omr_grader.application.detail_presenter import DetailStudentDisplay, NormalizedCell

_STUDENT_ID = re.compile(r"[0-9]{8}")
_HIGHLIGHT = QColor("#EA580C")
_OK = "#15803D"
_WARN = "#C2410C"
# Without ID cells (unreadable ID or an imported row) the sheet's top-left part is shown.
_FALLBACK_SHARE = (0.45, 0.6)
_PREVIEW_HEIGHT = 470


@dataclass(frozen=True, slots=True)
class IdCheck:
    """What the dialog says about a typed ID; ``savable`` enables 저장."""

    savable: bool
    message: str
    warning: bool
    on_roster: bool


def check_student_id(
    text: str,
    *,
    current_id: str,
    roster: Mapping[str, str],
    others: Mapping[str, str],
) -> IdCheck:
    """Judge a typed ID against the roster and the exam's other sheets."""
    if _STUDENT_ID.fullmatch(text) is None:
        return IdCheck(False, "학번은 숫자 8자리로 입력하세요.", False, False)
    if text == current_id:
        return IdCheck(False, "지금 학번과 같습니다.", False, text in roster)
    parts: list[str] = []
    warning = False
    on_roster = text in roster
    if on_roster:
        parts.append(f"✓ 명단: {roster[text]} ({text})")
    elif roster:
        parts.append("명단에 없는 학번입니다. 저장하면 이름 없이 표시됩니다.")
        warning = True
    if text in others:
        parts.append(
            f"이 시험의 다른 답안지({others[text]})도 이 학번입니다."
            " 저장하면 둘 다 '중복확인필요'로 표시됩니다."
        )
        warning = True
    elif not warning:
        parts.append("이 시험에서 겹치는 학번 없음")
    if warning and on_roster:
        # The check mark promises an all-clear; a warning line must not start with it.
        parts[0] = parts[0].removeprefix("✓ ")
    return IdCheck(True, " · ".join(parts), warning, on_roster)


def _id_columns(cells: Sequence[NormalizedCell]) -> list[list[NormalizedCell]]:
    """ID cells grouped by column, left to right."""
    columns: list[list[NormalizedCell]] = []
    for cell in sorted((c for c in cells if c.kind == "id"), key=lambda c: c.left):
        if columns and abs(cell.left - columns[-1][0].left) < cell.width / 2:
            columns[-1].append(cell)
        else:
            columns.append([cell])
    return columns


class _SheetPreview(QLabel):
    """The top-left of the sheet; columns whose typed digit differs are framed in orange."""

    def __init__(self, image_bytes: bytes | None, cells: Sequence[NormalizedCell], read_id: str):
        super().__init__()
        self._image = QImage.fromData(image_bytes) if image_bytes else QImage()
        self._columns = _id_columns(cells)
        self._read_id = read_id
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("누르면 답안지 전체를 크게 봅니다")
        self.setObjectName("idFixSheetPreview")
        self.setStyleSheet("border: 1px solid #CBD5E1; background: white;")
        self.show_typed("")

    @property
    def has_image(self) -> bool:
        return not self._image.isNull()

    def _crop(self) -> QRectF:
        width, height = self._image.width(), self._image.height()
        if not self._columns:
            return QRectF(0, 0, width * _FALLBACK_SHARE[0], height * _FALLBACK_SHARE[1])
        cells = [cell for column in self._columns for cell in column]
        right = max(cell.left + cell.width for cell in cells)
        bottom = max(cell.top + cell.height for cell in cells)
        pad = max(cell.width for cell in cells)
        return QRectF(
            0,
            0,
            min(width, (right + pad) * width),
            min(height, (bottom + pad) * height),
        )

    def show_typed(self, typed: str) -> None:
        if not self.has_image:
            self.setText("답안지 그림이 없습니다\n(엑셀로 가져온 응답이거나 아직 불러오는 중)")
            return
        crop = self._crop()
        picture = QPixmap.fromImage(self._image.copy(crop.toRect()))
        if len(typed) == len(self._columns) == len(self._read_id) and typed.isdigit():
            painter = QPainter(picture)
            painter.setPen(QPen(_HIGHLIGHT, max(3, picture.width() // 160)))
            width, height = self._image.width(), self._image.height()
            for index, column in enumerate(self._columns):
                if typed[index] == self._read_id[index]:
                    continue
                left = min(cell.left for cell in column) * width
                top = min(cell.top for cell in column) * height
                right = max(cell.left + cell.width for cell in column) * width
                bottom = max(cell.top + cell.height for cell in column) * height
                margin = (right - left) * 0.25
                painter.drawRect(
                    QRectF(left - margin, top - margin, right - left + 2 * margin, bottom - top + 2 * margin)
                )
            painter.end()
        self.setPixmap(
            picture.scaledToHeight(_PREVIEW_HEIGHT, Qt.TransformationMode.SmoothTransformation)
        )

    def mousePressEvent(self, event: object) -> None:
        if not self.has_image:
            return
        viewer = QDialog(self)
        viewer.setWindowTitle("답안지 전체")
        layout = QVBoxLayout(viewer)
        area = QScrollArea()
        label = QLabel()
        label.setPixmap(QPixmap.fromImage(self._image))
        area.setWidget(label)
        layout.addWidget(area)
        viewer.resize(900, 900)
        viewer.exec()


class IdFixDialog(QDialog):
    """Returns the new ID in ``chosen_id``, or ``undo`` True to go back to the read ID."""

    def __init__(
        self,
        exam_name: str,
        student: DetailStudentDisplay,
        roster: Sequence[tuple[str, str]],
        others: Mapping[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("학번 고치기")
        self.setObjectName("idFixDialog")
        self.chosen_id: str | None = None
        self.undo = False
        self._student = student
        self._roster = dict(roster)
        self._others = dict(others)
        read_id = student.fixed_from if student.fixed_from is not None else student.student_id

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)
        title = QLabel("학번 고치기")
        title.setProperty("role", "page-title")
        outer.addWidget(title)
        outer.addWidget(QLabel(f"{exam_name} · 석차 {student.rank or '-'} · {student.name or '이름 없음'}"))

        body = QHBoxLayout()
        body.setSpacing(20)
        left = QVBoxLayout()
        self.preview = _SheetPreview(student.image_bytes, student.cells, read_id)
        left.addWidget(self.preview)
        caption = QLabel("답안지 왼쪽 위 (손글씨 성명·학번과 칠한 칸)\n그림을 누르면 답안지 전체를 크게 봅니다")
        caption.setProperty("role", "muted")
        left.addWidget(caption)
        left.addStretch(1)
        body.addLayout(left)

        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        form.addWidget(QLabel("지금 학번"), 0, 0)
        shown = QLabel(student.student_id or "읽지 못함")
        shown.setStyleSheet("font-size: 16px; font-weight: 700;")
        status_row = QHBoxLayout()
        status_row.addWidget(shown)
        if student.fixed_from is not None:
            status_row.addWidget(self._badge(f"고친 학번 · 원래 {student.fixed_from or '판독 불가'}", "#2563EB"))
        elif self._roster and student.student_id not in self._roster:
            status_row.addWidget(self._badge("명단에 없음", "#EA580C"))
        status_row.addStretch(1)
        form.addLayout(status_row, 0, 1)

        form.addWidget(QLabel("명단에서 찾기"), 1, 0)
        self.search = QComboBox()
        self.search.setObjectName("idFixRosterSearch")
        self.search.setEditable(True)
        self.search.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._choices = [f"{name} · {student_id}" for student_id, name in roster if name]
        self.search.addItems(self._choices)
        self.search.setCurrentIndex(-1)
        line = self.search.lineEdit()
        if line is not None:
            line.setPlaceholderText("이름을 입력하세요" if roster else "명단 없이 인식한 시험입니다")
        completer = QCompleter(self._choices, self.search)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.search.setCompleter(completer)
        self.search.setEnabled(bool(roster))
        self.search.currentTextChanged.connect(self._picked)
        form.addWidget(self.search, 1, 1)

        form.addWidget(QLabel("새 학번"), 2, 0)
        self.id_edit = QLineEdit()
        self.id_edit.setObjectName("idFixNewId")
        self.id_edit.setMaxLength(8)
        self.id_edit.setPlaceholderText("숫자 8자리")
        self.id_edit.setStyleSheet("font-size: 16px; font-weight: 700; padding: 4px;")
        self.id_edit.textChanged.connect(self._typed)
        form.addWidget(self.id_edit, 2, 1)
        self.check_label = QLabel()
        self.check_label.setObjectName("idFixCheck")
        self.check_label.setWordWrap(True)
        form.addWidget(self.check_label, 3, 1)

        right = QVBoxLayout()
        right.addLayout(form)
        note = QLabel(
            "저장하면 결과 엑셀과 채점 이미지가 새 학번으로 다시 만들어지고, 비고에\n"
            "'학번 수정(원래 …)'이 남습니다. 칠한 학번은 기록으로 남아 '원래대로'로\n"
            "되돌릴 수 있습니다. 합산 성적표는 고친 학번으로 학생을 맞춥니다."
        )
        note.setStyleSheet("background: #F1F5F9; border-radius: 6px; padding: 10px; color: #334155;")
        right.addWidget(note)
        right.addStretch(1)
        body.addLayout(right, 1)
        outer.addLayout(body)

        buttons = QHBoxLayout()
        self.undo_button = QPushButton(
            f"원래대로 ({student.fixed_from or '판독 불가'})"
            if student.fixed_from is not None
            else "원래대로"
        )
        self.undo_button.setObjectName("idFixUndo")
        self.undo_button.setVisible(student.fixed_from is not None)
        self.undo_button.clicked.connect(self._undo)
        buttons.addWidget(self.undo_button)
        buttons.addStretch(1)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("저장")
        self.save_button.setObjectName("idFixSave")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)
        self._typed("")

    @staticmethod
    def _badge(text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: white; background: {color}; border-radius: 8px; padding: 2px 8px;"
            " font-weight: 600;"
        )
        return label

    def _picked(self, text: str) -> None:
        if text in self._choices:
            self.id_edit.setText(text.rsplit(" · ", 1)[1])

    def _check(self) -> IdCheck:
        return check_student_id(
            self.id_edit.text().strip(),
            current_id=self._student.student_id,
            roster=self._roster,
            others=self._others,
        )

    def _typed(self, text: str) -> None:
        check = self._check()
        self.check_label.setText(check.message if text else "")
        self.check_label.setStyleSheet(
            f"color: {_WARN if check.warning or not check.savable else _OK}; font-weight: 600;"
        )
        self.save_button.setEnabled(check.savable)
        self.preview.show_typed(text.strip())

    def _save(self) -> None:
        check = self._check()
        if not check.savable:
            return
        if self._roster and not check.on_roster:
            answer = QMessageBox.question(
                self,
                "명단에 없는 학번",
                "이 학번은 명단에 없습니다. 그래도 저장할까요?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.chosen_id = self.id_edit.text().strip()
        self.accept()

    def _undo(self) -> None:
        self.undo = True
        self.accept()


__all__ = ["IdCheck", "IdFixDialog", "check_student_id"]
