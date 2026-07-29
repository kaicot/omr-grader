"""Screen 3-1: passive, controller-driven OMR inspection and correction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from omr_grader.application.detail_presenter import (
    DetailAnswerEdit,
    DetailEdit,
    DetailIdEdit,
    DetailLoadRequest,
    DetailLoadResult,
    DetailPageDisplay,
    DetailPageRequest,
    DetailPreviewResult,
    DetailSaveResult,
    DetailStudentDisplay,
    NormalizedCell,
)
from omr_grader.ui.detail_model import DetailTableModel
from omr_grader.ui.omr_graphics_view import OmrGraphicsView


@dataclass(frozen=True, slots=True)
class _SaveSnapshot:
    request: DetailPageRequest
    edit_generation: int



class DetailPage(QWidget):
    """A passive UI: controller owns loading, previews, writes, and navigation."""

    back_requested = Signal(object)
    save_requested = Signal(object)
    discard_requested = Signal(object)
    close_requested = Signal(object)
    unsaved_changes_requested = Signal(object)
    work_item_load_requested = Signal(object)
    preview_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._display: DetailPageDisplay | None = None
        self._selected: DetailStudentDisplay | None = None
        self._answer_original: dict[tuple[str, int], int | None] = {}
        self._id_original: dict[tuple[str, int], int | None] = {}
        self._edits: dict[tuple[str, str, int], DetailEdit] = {}
        self._lazy_authorized_work_items: set[str] = set()
        self._load_correlations: dict[str, str] = {}
        self._preview_correlation_id: str | None = None
        self._save_snapshot: _SaveSnapshot | None = None
        self._save_in_progress = False
        self._edit_generation = 0
        self._write_enabled = True
        self._build_ui()

    @property
    def is_dirty(self) -> bool:
        return bool(self._edits)

    @property
    def pending_edits(self) -> tuple[DetailEdit, ...]:
        return tuple(self._edits[key] for key in sorted(self._edits))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        title_row = QHBoxLayout()
        self.back_button = QPushButton("← 대시보드로 돌아가기")
        self.back_button.setObjectName("detailBackButton")
        self.back_button.setAccessibleName("대시보드로 돌아가기")
        self.back_button.clicked.connect(self.request_back)
        self.title_label = QLabel("상세 결과")
        self.title_label.setProperty("role", "page-title")
        self.save_button = QPushButton("수정사항 저장")
        self.save_button.setObjectName("detailSaveButton")
        self.save_button.setAccessibleName("수정사항 저장")
        self.save_button.clicked.connect(self.request_save)
        title_row.addWidget(self.back_button)
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        title_row.addWidget(self.save_button)
        root.addLayout(title_row)
        self.summary_label = QLabel("요약: 표시할 결과가 없습니다.")
        self.summary_label.setAccessibleName("시험 요약")
        root.addWidget(self.summary_label)
        self.conflict_label = QLabel()
        self.conflict_label.setObjectName("detailConflictLabel")
        self.conflict_label.setAccessibleName("학번 중복 또는 충돌")
        root.addWidget(self.conflict_label)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("detailSplitter")
        self.splitter.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.table = QTableView()
        self.table.setObjectName("detailStudentTable")
        self.table.setAccessibleName("학생별 성적표")
        self.model = DetailTableModel(self.table)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.selectionModel().currentRowChanged.connect(self._selected_row_changed)
        left_layout.addWidget(self.table)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        controls = QHBoxLayout()
        self.zoom_in_button = QPushButton("확대")
        self.zoom_out_button = QPushButton("축소")
        self.fit_button = QPushButton("화면에 맞춤")
        for button, name in (
            (self.zoom_in_button, "이미지 확대"),
            (self.zoom_out_button, "이미지 축소"),
            (self.fit_button, "이미지를 화면에 맞춤"),
        ):
            button.setAccessibleName(name)
            controls.addWidget(button)
        controls.addStretch()
        right_layout.addLayout(controls)
        self.graphics_view = OmrGraphicsView()
        self.graphics_view.setMinimumSize(320, 240)
        self.graphics_view.cell_activated.connect(self._activate_cell)
        right_layout.addWidget(self.graphics_view)
        self.no_image_label = QLabel("선택한 학생의 OMR 원본 이미지가 없습니다.")
        self.no_image_label.setObjectName("detailNoImageLabel")
        self.no_image_label.setAccessibleName("OMR 원본 이미지 없음")
        self.no_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.no_image_label)
        id_row = QHBoxLayout()
        id_row.addWidget(QLabel("학번 수정:"))
        self.id_inputs: list[QLineEdit] = []
        for position in range(8):
            field = QLineEdit()
            field.setMaxLength(1)
            field.setFixedWidth(28)
            field.setAccessibleName(f"학번 {position + 1}번째 자리")
            field.setInputMask("9;_")
            field.editingFinished.connect(lambda position=position: self._edit_id_digit(position))
            self.id_inputs.append(field)
            id_row.addWidget(field)
        right_layout.addLayout(id_row)
        self.zoom_in_button.clicked.connect(self.graphics_view.zoom_in)
        self.zoom_out_button.clicked.connect(self.graphics_view.zoom_out)
        self.fit_button.clicked.connect(self.graphics_view.fit_image)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 6)
        self.splitter.setSizes([400, 600])
        root.addWidget(self.splitter, 1)
        self.setMinimumSize(720, 480)
        self._refresh_actions()

    def set_write_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        self._write_enabled = enabled
        self.graphics_view.set_editable(enabled and not self._save_in_progress)
        self._refresh_actions()

    def set_display(self, display: DetailPageDisplay | None) -> None:
        if display is not None and not isinstance(display, DetailPageDisplay):
            raise TypeError("display must be DetailPageDisplay or None")
        previous_display = self._display
        selected_id = None if self._selected is None else self._selected.work_item_id
        selected_raster = self._selected
        if (
            display is not None
            and previous_display is not None
            and display.session_id == previous_display.session_id
            and selected_raster is not None
            and selected_raster.image_bytes is not None
        ):
            display = replace(
                display,
                students=tuple(
                    replace(
                        student,
                        image_bytes=selected_raster.image_bytes,
                        cells=selected_raster.cells,
                    )
                    if (
                        student.work_item_id == selected_id
                        and student.image_bytes is None
                    )
                    else student
                    for student in display.students
                ),
            )
        self._display = display
        self._edits.clear()
        self._set_originals()
        self._lazy_authorized_work_items.clear()
        self._load_correlations.clear()
        self._preview_correlation_id = None
        self._save_snapshot = None
        self._save_in_progress = False
        if display is None:
            self.title_label.setText("상세 결과")
            self.summary_label.setText("요약: 표시할 결과가 없습니다.")
            self._restore_selection((), None)
        else:
            self.title_label.setText(f"{display.exam_name} 상세 결과")
            x = display.summary
            self.summary_label.setText(
                f"요약: 총원 {x.student_count}명 | 평균 {x.average_score}점 | "
                f"최고점 {x.high_score}점 | 최저점 {x.low_score}점"
            )
            self._restore_selection(display.students, selected_id)
        self._refresh_actions()

    def apply_loaded_work_item(self, result: DetailLoadResult) -> None:
        """Merge a correlated lazy result and retain only the selected raster."""
        if not isinstance(result, DetailLoadResult) or self._display is None:
            return
        student = result.student
        selected_id = None if self._selected is None else self._selected.work_item_id
        if (
            self._load_correlations.get(student.work_item_id) != result.correlation_id
            or selected_id != student.work_item_id
            or not self._matches_display(self._display, student.work_item_id)
        ):
            return
        self._load_correlations.pop(student.work_item_id, None)
        self._record_lazy_baselines(student)
        student = self._with_drafts(student)
        students = tuple(
            student
            if item.work_item_id == student.work_item_id
            else replace(item, image_bytes=None)
            for item in self._display.students
        )
        self._display = replace(self._display, students=students)
        self._restore_selection(students, student.work_item_id)

    def apply_preview(self, result: DetailPreviewResult) -> None:
        if (
            not isinstance(result, DetailPreviewResult)
            or self._display is None
            or result.correlation_id != self._preview_correlation_id
            or not self._matches_display(result.display)
        ):
            return
        selected_id = None if self._selected is None else self._selected.work_item_id
        previous = {student.work_item_id: student for student in self._display.students}
        students = tuple(
            self._with_drafts(
                replace(
                    student,
                    image_bytes=previous[student.work_item_id].image_bytes,
                    cells=previous[student.work_item_id].cells,
                )
            )
            if student.work_item_id in previous
            else student
            for student in result.display.students
        )
        self._display = replace(result.display, students=students)
        self._restore_selection(students, selected_id)

    def _set_originals(self) -> None:
        """Listing and preview projections cannot authorize correction before-values."""
        self._answer_original.clear()
        self._id_original.clear()
    def _record_lazy_baselines(self, student: DetailStudentDisplay) -> None:
        self._lazy_authorized_work_items.add(student.work_item_id)
        for answer in student.answers:
            key = (student.work_item_id, answer.question)
            if ("answer", student.work_item_id, answer.question) not in self._edits:
                self._answer_original[key] = answer.answer
        for position, digit in enumerate(student.id_digits, 1):
            if ("id", student.work_item_id, position) not in self._edits:
                self._id_original[(student.work_item_id, position)] = digit
    def _with_drafts(self, student: DetailStudentDisplay) -> DetailStudentDisplay:
        answers = tuple(
            replace(answer, answer=edit.after)
            if isinstance(
                edit := self._edits.get(
                    ("answer", student.work_item_id, answer.question)
                ),
                DetailAnswerEdit,
            )
            else answer
            for answer in student.answers
        )
        digits = tuple(
            edit.after
            if isinstance(
                edit := self._edits.get(("id", student.work_item_id, position)),
                DetailIdEdit,
            )
            else digit
            for position, digit in enumerate(student.id_digits, 1)
        )
        return replace(student, answers=answers, id_digits=digits)

    def _matches_display(
        self, display: DetailPageDisplay, work_item_id: str | None = None
    ) -> bool:
        current = self._display
        return (
            current is not None
            and display.session_id == current.session_id
            and display.revision == current.revision
            and display.detail_handle == current.detail_handle
            and (
                work_item_id is None
                or any(student.work_item_id == work_item_id for student in display.students)
            )
        )
    def _restore_selection(
        self, students: tuple[DetailStudentDisplay, ...], selected_id: str | None
    ) -> None:
        self.model.set_students(students)
        row = next(
            (
                index
                for index, student in enumerate(students)
                if student.work_item_id == selected_id
            ),
            0,
        )
        if not students:
            self.table.clearSelection()
            self._show_student(None)
            return
        self.table.selectRow(row)
        self._selected_row_changed(self.model.index(row, 0), QModelIndex())

    def _selected_row_changed(self, current: QModelIndex, _: QModelIndex) -> None:
        student = self.model.student_at(current.row())
        self._load_correlations.clear()
        self._show_student(student)
        if (
            student is not None
            and student.work_item_id not in self._lazy_authorized_work_items
            and self._display is not None
            and student.work_item_id not in self._load_correlations
        ):
            correlation_id = uuid4().hex
            self._load_correlations[student.work_item_id] = correlation_id
            self.work_item_load_requested.emit(
                DetailLoadRequest(
                    self._display.session_id,
                    self._display.revision,
                    self._display.detail_handle,
                    student.work_item_id,
                    correlation_id,
                )
            )

    def _show_student(self, student: DetailStudentDisplay | None) -> None:
        self._selected = student
        if student is None or student.image_bytes is None:
            self.graphics_view.set_image(None)
            self.no_image_label.setVisible(True)
        else:
            self.graphics_view.set_image(student.image_bytes, student.cells)
            self.no_image_label.setVisible(False)
        self.conflict_label.setText(
            "" if student is None or student.id_conflict is None else student.id_conflict
        )
        digits = () if student is None else student.id_digits
        for position, field in enumerate(self.id_inputs):
            field.blockSignals(True)
            field.setText(
                "" if position >= len(digits) or digits[position] is None else str(digits[position])
            )
            field.blockSignals(False)
        self._refresh_actions()

    def _activate_cell(self, cell: NormalizedCell) -> None:
        if not self._write_enabled or self._save_in_progress or self._selected is None:
            return
        if cell.kind == "answer":
            if cell.question is None or cell.option is None:
                return
            work_item_id, question = self._selected.work_item_id, cell.question
            baseline_key = (work_item_id, question)
            if baseline_key not in self._answer_original:
                return
            before = self._answer_original[baseline_key]
            existing = self._edits.get(("answer", work_item_id, question))
            current = before if existing is None else existing.after
            after = None if current == cell.option else cell.option
            key = ("answer", work_item_id, question)
            if after == before:
                self._edits.pop(key, None)
            else:
                self._edits[key] = DetailAnswerEdit(work_item_id, question, before, after)
        elif cell.kind == "id" and type(cell.option) is int and 0 <= cell.option <= 9:
            positions = sorted(
                {candidate.left for candidate in self._selected.cells if candidate.kind == "id"}
            )
            if cell.left not in positions:
                return
            position = positions.index(cell.left) + 1
            if position > 8:
                return
            work_item_id = self._selected.work_item_id
            baseline_key = (work_item_id, position)
            if baseline_key not in self._id_original:
                return
            before = self._id_original[baseline_key]
            key = ("id", work_item_id, position)
            if cell.option == before:
                self._edits.pop(key, None)
            else:
                self._edits[key] = DetailIdEdit(work_item_id, position, before, cell.option)
        else:
            return
        self._edit_generation += 1
        self._request_preview()
        self._refresh_actions()

    def _edit_id_digit(self, zero_position: int) -> None:
        if not self._write_enabled or self._save_in_progress or self._selected is None:
            return
        text = self.id_inputs[zero_position].text()
        after = int(text) if text else None
        work_item_id, position = self._selected.work_item_id, zero_position + 1
        baseline_key = (work_item_id, position)
        if baseline_key not in self._id_original:
            return
        before = self._id_original[baseline_key]
        key = ("id", work_item_id, position)
        if after == before:
            self._edits.pop(key, None)
        else:
            self._edits[key] = DetailIdEdit(work_item_id, position, before, after)
        self._edit_generation += 1
        self._request_preview()
        self._refresh_actions()

    def _request_preview(self) -> None:
        if self._save_in_progress:
            return
        request = self._request("preview")
        if request is not None:
            self.preview_requested.emit(request)

    def request_save(self) -> None:
        if (
            not self._write_enabled
            or self._save_in_progress
            or self._display is None
            or not self._edits
        ):
            return
        request = self._request("save")
        if request is not None:
            self._save_snapshot = _SaveSnapshot(request, self._edit_generation)
            self._save_in_progress = True
            self._refresh_actions()
            self.save_requested.emit(request)

    def request_back(self) -> None:
        self._request_navigation("back")

    def request_close(self) -> None:
        self._request_navigation("close")

    def request_discard(self) -> None:
        request = self._request("discard")
        if request is not None:
            self.discard_requested.emit(request)

    def _request_navigation(self, intent: str) -> None:
        request = self._request(intent)
        if request is None:
            return
        if self.is_dirty:
            self.unsaved_changes_requested.emit(request)
        elif intent == "back":
            self.back_requested.emit(request)
        else:
            self.close_requested.emit(request)

    def save_completed(self, result: DetailSaveResult) -> None:
        """Accept only the immutable identity and edit-generation snapshot in flight."""
        snapshot = self._save_snapshot
        if (
            not isinstance(result, DetailSaveResult)
            or snapshot is None
            or self._display is None
            or result.correlation_id != snapshot.request.correlation_id
            or self._edit_generation != snapshot.edit_generation
            or self._display.session_id != snapshot.request.session_id
            or self._display.revision != snapshot.request.revision
            or self._display.detail_handle != snapshot.request.detail_handle
            or result.display.session_id != snapshot.request.session_id
            or result.display.revision <= snapshot.request.revision
        ):
            return
        self.set_display(result.display)

    def save_failed(self, correlation_id: str) -> None:
        """Release only the matching save attempt while retaining local edits."""
        snapshot = self._save_snapshot
        if snapshot is None or correlation_id != snapshot.request.correlation_id:
            return
        self._save_snapshot = None
        self._save_in_progress = False
        self._refresh_actions()

    def _request(self, intent: str) -> DetailPageRequest | None:
        if self._display is None:
            return None
        correlation_id = uuid4().hex
        if intent == "preview":
            self._preview_correlation_id = correlation_id
        return DetailPageRequest(
            self._display.session_id,
            self._display.revision,
            intent,
            self.pending_edits,
            self._display.detail_handle,
            correlation_id,
        )

    def _refresh_actions(self) -> None:
        self.save_button.setEnabled(
            self._write_enabled
            and not self._save_in_progress
            and self._display is not None
            and self.is_dirty
        )
        self.graphics_view.set_editable(self._write_enabled and not self._save_in_progress)
        ids_editable = (
            self._write_enabled
            and not self._save_in_progress
            and self._selected is not None
            and all(
                (self._selected.work_item_id, position) in self._id_original
                for position in range(1, 9)
            )
        )
        for field in getattr(self, "id_inputs", []):
            field.setEnabled(ids_editable)
