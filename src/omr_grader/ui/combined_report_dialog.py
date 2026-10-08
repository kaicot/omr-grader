"""Combined score report: order the parts, optionally pick a subject workbook, choose where to save."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omr_grader.domain.models import DashboardIndexEntry


@dataclass(frozen=True, slots=True)
class CombinedReportChoice:
    """What the user chose: parts in order, an optional subject workbook, the output file."""

    session_ids: tuple[str, ...]
    subject_config_path: str | None
    destination: str


class CombinedReportDialog(QDialog):
    """Collect a combined-report request; the controller runs it."""

    sample_requested = Signal(str)

    def __init__(
        self, entries: tuple[DashboardIndexEntry, ...], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("combinedReportDialog")
        self.setWindowTitle("합산 성적표")
        self.setMinimumWidth(560)
        # Parts start in exam-name order, which puts p1 before p2.
        self._entries = tuple(sorted(entries, key=lambda entry: entry.exam_name))
        self._subject_path: str | None = None
        self.choice: CombinedReportChoice | None = None

        root = QVBoxLayout(self)
        title = QLabel("고른 시험을 파트 순서대로 합칩니다.")
        title.setObjectName("combinedReportTitle")
        root.addWidget(title)
        self.part_list = QListWidget()
        self.part_list.setObjectName("combinedPartList")
        self.part_list.setAccessibleName("합칠 시험과 파트 순서")
        root.addWidget(self.part_list)
        order = QHBoxLayout()
        self.up_button = QPushButton("위로")
        self.down_button = QPushButton("아래로")
        order.addWidget(self.up_button)
        order.addWidget(self.down_button)
        order.addStretch()
        root.addLayout(order)

        subject_title = QLabel(
            "과목 구성 (선택): 여러 과목을 따로 집계하거나 합격을 판정할 때만 불러옵니다."
        )
        subject_title.setWordWrap(True)
        root.addWidget(subject_title)
        self.subject_label = QLabel("과목 구성 없음: 파트별 점수, 총점, 통합 석차만 만듭니다.")
        self.subject_label.setObjectName("combinedSubjectLabel")
        self.subject_label.setWordWrap(True)
        root.addWidget(self.subject_label)
        subject_row = QHBoxLayout()
        self.subject_button = QPushButton("과목 구성 불러오기")
        self.subject_clear_button = QPushButton("지우기")
        self.sample_button = QPushButton("샘플 내려받기")
        subject_row.addWidget(self.subject_button)
        subject_row.addWidget(self.subject_clear_button)
        subject_row.addStretch()
        subject_row.addWidget(self.sample_button)
        root.addLayout(subject_row)

        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("취소")
        self.create_button = QPushButton("합산 성적표 만들기")
        self.create_button.setObjectName("primaryActionButton")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.create_button)
        root.addLayout(actions)

        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.subject_button.clicked.connect(self._pick_subject)
        self.subject_clear_button.clicked.connect(lambda: self.set_subject_path(None))
        self.sample_button.clicked.connect(self._save_sample)
        self.cancel_button.clicked.connect(self.reject)
        self.create_button.clicked.connect(self._create)
        self.part_list.currentRowChanged.connect(lambda _: self._refresh())
        self._fill()

    @property
    def ordered_entries(self) -> tuple[DashboardIndexEntry, ...]:
        return self._entries

    def set_subject_path(self, path: str | None) -> None:
        self._subject_path = path
        self.subject_label.setText(
            "과목 구성 없음: 파트별 점수, 총점, 통합 석차만 만듭니다."
            if path is None
            else f"과목 구성: {Path(path).name}"
        )
        self._refresh()

    def _fill(self, current: int = 0) -> None:
        self.part_list.clear()
        for number, entry in enumerate(self._entries, 1):
            self.part_list.addItem(
                f"파트{number}   {entry.display_folder}   ({entry.participant_count}명)"
            )
        if self._entries:
            self.part_list.setCurrentRow(current)
        self._refresh()

    def _move(self, step: int) -> None:
        row = self.part_list.currentRow()
        target = row + step
        if not 0 <= row < len(self._entries) or not 0 <= target < len(self._entries):
            return
        entries = list(self._entries)
        entries[row], entries[target] = entries[target], entries[row]
        self._entries = tuple(entries)
        self._fill(target)

    def _refresh(self) -> None:
        row = self.part_list.currentRow()
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < len(self._entries) - 1)
        self.subject_clear_button.setEnabled(self._subject_path is not None)
        self.create_button.setEnabled(len(self._entries) >= 2)

    def _pick_subject(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "과목 구성 엑셀 선택", "", "Excel 통합 문서 (*.xlsx)"
        )
        if path:
            self.set_subject_path(path)

    def _save_sample(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "과목 구성 샘플 저장", "과목구성_샘플.xlsx", "Excel 통합 문서 (*.xlsx)"
        )
        if path:
            self.sample_requested.emit(path)

    def default_filename(self) -> str:
        first = self._entries[0].display_folder if self._entries else "시험"
        return f"합산성적표_{first}.xlsx"

    def _create(self) -> None:
        if len(self._entries) < 2:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "합산 성적표 저장", self.default_filename(), "Excel 통합 문서 (*.xlsx)"
        )
        if not destination:
            return
        if not destination.lower().endswith(".xlsx"):
            destination += ".xlsx"
        self.choice = CombinedReportChoice(
            tuple(entry.session_id for entry in self._entries),
            self._subject_path,
            destination,
        )
        self.accept()


__all__ = ["CombinedReportChoice", "CombinedReportDialog"]
