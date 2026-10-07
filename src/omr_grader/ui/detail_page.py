"""Screen 3-1: passive, controller-driven, view-only OMR result inspection."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from omr_grader.application.detail_presenter import (
    DetailEdit,
    DetailLoadRequest,
    DetailLoadResult,
    DetailPageDisplay,
    DetailPageRequest,
    DetailPreviewResult,
    DetailSaveResult,
    DetailStudentDisplay,
)
from omr_grader.domain.score_average import display_average
from omr_grader.ui.detail_model import DetailTableModel
from omr_grader.ui.omr_graphics_view import OmrGraphicsView


class DetailPage(QWidget):
    """A passive, view-only UI: the controller owns loading and navigation.

    Corrections are not offered yet, so the page is never dirty and never requests a
    preview or a save. The save, discard, preview, and unsaved-changes signals stay
    declared for the controller's bindings but are never emitted.
    """

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
        self._loaded_work_items: set[str] = set()
        self._load_correlations: dict[str, str] = {}
        self._build_ui()

    @property
    def is_dirty(self) -> bool:
        return False

    @property
    def pending_edits(self) -> tuple[DetailEdit, ...]:
        return ()

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
        title_row.addWidget(self.back_button)
        title_row.addWidget(self.title_label)
        title_row.addStretch()
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
        right_layout.addWidget(self.graphics_view)
        self.no_image_label = QLabel("엑셀로 채점된 결과로 OMR 이미지가 없습니다")
        self.no_image_label.setObjectName("detailNoImageLabel")
        self.no_image_label.setAccessibleName("OMR 원본 이미지 없음")
        self.no_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.no_image_label)
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

    def set_write_enabled(self, enabled: bool) -> None:
        """Validate the controller-owned authority; a view-only page has nothing to gate."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")

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
        self._loaded_work_items.clear()
        self._load_correlations.clear()
        if display is None:
            self.title_label.setText("상세 결과")
            self.summary_label.setText("요약: 표시할 결과가 없습니다.")
            self._restore_selection((), None)
        else:
            self.title_label.setText(f"{display.exam_name} 상세 결과")
            x = display.summary
            self.summary_label.setText(
                f"요약: 총원 {x.student_count}명 | 평균 {display_average(x.average_score)}점 | "
                f"최고점 {x.high_score}점 | 최저점 {x.low_score}점"
            )
            self._restore_selection(display.students, selected_id)

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
        # Only this student's image is kept, so every other student loads again.
        self._loaded_work_items = {student.work_item_id}
        students = tuple(
            student
            if item.work_item_id == student.work_item_id
            else replace(item, image_bytes=None)
            for item in self._display.students
        )
        self._display = replace(self._display, students=students)
        self._restore_selection(students, student.work_item_id)

    def apply_preview(self, result: DetailPreviewResult) -> None:
        """Ignore a preview: this page never requests one, so none can be current."""

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
            and student.work_item_id not in self._loaded_work_items
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

    def request_back(self) -> None:
        self._request_navigation("back")

    def request_close(self) -> None:
        self._request_navigation("close")

    def _request_navigation(self, intent: str) -> None:
        """Leave straight away: a view-only page never holds unsaved changes."""
        request = self._request(intent)
        if request is None:
            return
        if intent == "back":
            self.back_requested.emit(request)
        else:
            self.close_requested.emit(request)

    def save_completed(self, result: DetailSaveResult) -> None:
        """Ignore a save result: this page never requests a save, so none can be current."""

    def save_failed(self, correlation_id: str) -> None:
        """Ignore a save failure: this page never requests a save, so none can be current."""

    def _request(self, intent: str) -> DetailPageRequest | None:
        if self._display is None:
            return None
        return DetailPageRequest(
            self._display.session_id,
            self._display.revision,
            intent,
            self.pending_edits,
            self._display.detail_handle,
            uuid4().hex,
        )
