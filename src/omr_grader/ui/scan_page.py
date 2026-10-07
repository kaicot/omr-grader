"""Screen 1: collect scan inputs and emit immutable recognition requests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .import_widgets import ImportDropWidget, ImportKind, ImportSelection

_PROGRESS_PHASES = ("prepare", "recognize", "save")


@dataclass(frozen=True, slots=True)
class ValidatedProfileState:
    """Controller-validated profile metadata rendered without opening its path."""

    name: str
    path: str
    dimensions: tuple[int, int] | None
    grid_summary: str
    validation_errors: tuple[str, ...] = ()
    is_default: bool = False
    duplicate_outcome: str | None = None
    validated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be a non-empty string")
        if self.dimensions is not None and (
            not isinstance(self.dimensions, tuple)
            or len(self.dimensions) != 2
            or any(type(value) is not int or value <= 0 for value in self.dimensions)
        ):
            raise ValueError("dimensions must be two positive integers or None")
        if not isinstance(self.grid_summary, str):
            raise TypeError("grid_summary must be a string")
        if not isinstance(self.validation_errors, tuple) or any(
            not isinstance(error, str) or not error for error in self.validation_errors
        ):
            raise ValueError("validation_errors must be an immutable tuple of non-empty strings")
        if type(self.is_default) is not bool or type(self.validated) is not bool:
            raise TypeError("is_default and validated must be booleans")
        if self.duplicate_outcome is not None and not isinstance(self.duplicate_outcome, str):
            raise TypeError("duplicate_outcome must be a string or None")
        if self.validated and self.validation_errors:
            raise ValueError("validated profiles cannot contain validation errors")


@dataclass(frozen=True, slots=True)
class ScanPageRequest:
    """UI-only recognition request; all fields are immutable value objects."""

    exam_name: str
    profile: ValidatedProfileState
    roster_path: str | None
    source: ImportSelection
    sensitivity: int
    session_id: str | None

    @property
    def profile_path(self) -> str:
        """Compatibility value for the application command boundary."""
        return self.profile.path


class _ProfileCard(QFrame):
    """Section 4 card: a profile file dropped anywhere on it reaches the hidden drop widget.

    The drop widget keeps the structural validation and the selection signal, so a drop on the
    card behaves exactly like a drop on the old profile drop zone.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setProperty("dragActive", False)
        self.drop_widget = ImportDropWidget(ImportKind.PROFILE, self)
        self.drop_widget.setObjectName("profileImportWidget")
        self.drop_widget.hide()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self.drop_widget.dragEnterEvent(event)
        self._follow_drop_widget()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self.drop_widget.dragMoveEvent(event)
        self._follow_drop_widget()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.drop_widget.dragLeaveEvent(event)
        self._follow_drop_widget()

    def dropEvent(self, event: QDropEvent) -> None:
        self.drop_widget.dropEvent(event)
        self._follow_drop_widget()

    def _follow_drop_widget(self) -> None:
        active = bool(self.drop_widget.property("dragActive"))
        if self.property("dragActive") != active:
            self.setProperty("dragActive", active)
            self.style().unpolish(self)
            self.style().polish(self)


def _compact_drop_zone(widget: ImportDropWidget) -> None:
    """Thinner padding for this page's drop zones, so all five sections fit unscrolled."""
    layout = widget.layout()
    if layout is not None:
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(2)


class ScanPage(QWidget):
    """Input screen with no filesystem, OCR, or workbook work on the UI thread."""

    recognition_requested = Signal(object)
    fresh_response_requested = Signal()
    cancel_requested = Signal(object)
    help_requested = Signal()
    reset_requested = Signal()
    sample_roster_requested = Signal()
    source_browse_requested = Signal(object)
    source_changed = Signal(object)
    roster_browse_requested = Signal(object)
    profile_browse_requested = Signal()
    profile_import_requested = Signal(object)
    profile_drop_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._profiles: tuple[ValidatedProfileState, ...] = ()
        self._source: ImportSelection | None = None
        self._roster_path: str | None = None
        self._session_id: str | None = None
        self._write_enabled = True
        self._busy = False
        self._profile_importing = False
        self._form_detecting = False
        self._operation_id: str | None = None
        self._cancellable = True
        # The running operation's own clock: started by the first busy or progress call and
        # redrawn every second so the status line keeps moving between worker events.
        self._busy_started: float | None = None
        self._busy_text = ""
        self._progress_phase: str | None = None
        self._progress_counts = (0, 0, 0)
        self._progress_eta: float | None = None
        self._progress_event_at = 0.0
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self._refresh_progress)
        self._build_ui()
        self._update_gating()
        self._default_sensitivity = 5
        self._default_profile_name: str | None = None

    def _build_ui(self) -> None:
        self.setObjectName("scanPage")
        self.setAccessibleName("OMR 스캔")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(14)

        top = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("OMR 시험지 인식", self)
        title.setObjectName("scanPageTitle")
        title.setAccessibleName("OMR 시험지 인식")
        subtitle = QLabel(
            "시험명 → 명단(선택) → 스캔 선택 → 인식 프로필 자동 지정 → 인식 실행", self
        )
        subtitle.setObjectName("scanPageSubtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        top.addLayout(heading, 1)
        self.fresh_response_button = QPushButton("응답 엑셀로 시작", self)
        self.fresh_response_button.setObjectName("freshResponseButton")
        self.fresh_response_button.setAccessibleName("응답 엑셀로 새 세션 시작")
        self.fresh_response_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.fresh_response_button.installEventFilter(self)
        self.reset_button = QPushButton("초기화 / 재설정", self)
        self.reset_button.setObjectName("scanResetButton")
        self.reset_button.setAccessibleName("입력 초기화 및 재설정")
        top.addWidget(self.fresh_response_button)
        top.addWidget(self.reset_button)
        root.addLayout(top)

        # The five input sections scroll on their own, so the progress area and the action row
        # below stay on screen however small the window is.
        self.sections_scroll_area = QScrollArea(self)
        self.sections_scroll_area.setObjectName("scanSectionsScrollArea")
        self.sections_scroll_area.setAccessibleName("입력 항목 영역")
        self.sections_scroll_area.setWidgetResizable(True)
        self.sections_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        # No tab stop of its own: the window scrolls it to whichever control takes the focus.
        self.sections_scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Its size hint stays out of the page's preferred height: with word-wrapped labels the
        # page is height-for-width, and the window's own scroll area would otherwise keep the
        # page as tall as all five sections and push the action row off a short window.
        self.sections_scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )
        self.sections_scroll_area.setMinimumHeight(120)
        sections = QWidget()
        sections.setObjectName("scanSections")
        self.sections_scroll_area.setWidget(sections)
        sections_layout = QVBoxLayout(sections)
        sections_layout.setContentsMargins(0, 0, 0, 0)
        sections_layout.setSpacing(10)

        exam_card = QFrame(sections)
        exam_card.setObjectName("scanExamCard")
        exam_layout = QVBoxLayout(exam_card)
        exam_title = QLabel("1. 시험명 *", exam_card)
        exam_title.setObjectName("scanSectionLabel")
        exam_layout.addWidget(exam_title)
        self.exam_name_edit = QLineEdit(exam_card)
        self.exam_name_edit.setObjectName("examNameEdit")
        self.exam_name_edit.setAccessibleName("시험명")
        self.exam_name_edit.setPlaceholderText("예: 26-2 생리학 중간고사")
        exam_layout.addWidget(self.exam_name_edit)
        sections_layout.addWidget(exam_card)

        roster_card = QFrame(sections)
        roster_card.setObjectName("scanRosterCard")
        roster_layout = QVBoxLayout(roster_card)
        roster_header = QHBoxLayout()
        roster_title = QLabel("2. 응시 학생 명단 (선택)", roster_card)
        roster_title.setObjectName("scanSectionLabel")
        self.sample_roster_button = QPushButton("샘플 명단 내려받기", roster_card)
        self.sample_roster_button.setObjectName("sampleRosterButton")
        self.sample_roster_button.setAccessibleName("샘플 응시 학생 명단 내려받기")
        self.roster_status = QLabel("명단이 없으면 이름은 ‘미등록’으로 표시됩니다.", roster_card)
        self.roster_status.setObjectName("rosterStatus")
        roster_header.addWidget(roster_title)
        roster_header.addSpacing(12)
        roster_header.addWidget(self.roster_status)
        roster_header.addStretch()
        roster_header.addWidget(self.sample_roster_button)
        roster_layout.addLayout(roster_header)
        self.roster_widget = ImportDropWidget(ImportKind.ROSTER, roster_card)
        self.roster_widget.setObjectName("rosterImportWidget")
        _compact_drop_zone(self.roster_widget)
        roster_layout.addWidget(self.roster_widget)
        sections_layout.addWidget(roster_card)

        source_card = QFrame(sections)
        source_card.setObjectName("scanSourceCard")
        source_layout = QVBoxLayout(source_card)
        source_header = QHBoxLayout()
        source_title = QLabel("3. 스캔 파일/폴더 선택 *", source_card)
        source_title.setObjectName("scanSectionLabel")
        source_header.addWidget(source_title)
        source_header.addStretch()
        self.source_folder_button = QPushButton("이미지 폴더 찾기", source_card)
        self.source_folder_button.setObjectName("sourceFolderButton")
        self.source_pdf_button = QPushButton("PDF 파일 찾기", source_card)
        self.source_pdf_button.setObjectName("sourcePdfButton")
        source_header.addWidget(self.source_folder_button)
        source_header.addWidget(self.source_pdf_button)
        source_layout.addLayout(source_header)
        self.source_widget = ImportDropWidget(ImportKind.SOURCE, source_card)
        self.source_widget.setObjectName("scanSourceImportWidget")
        _compact_drop_zone(self.source_widget)
        source_layout.addWidget(self.source_widget)
        sections_layout.addWidget(source_card)

        profile_card = _ProfileCard(sections)
        profile_card.setObjectName("scanProfileCard")
        profile_layout = QVBoxLayout(profile_card)
        profile_title = QLabel("4. 인식 프로필 (스캔을 고르면 자동 지정)", profile_card)
        profile_title.setObjectName("scanSectionLabel")
        profile_layout.addWidget(profile_title)
        self.form_status_label = QLabel(profile_card)
        self.form_status_label.setObjectName("formStatusLabel")
        self.form_status_label.setAccessibleName("답안지 양식 자동 인식 상태")
        self.form_status_label.setWordWrap(True)
        self.form_status_label.setTextFormat(Qt.TextFormat.PlainText)
        profile_layout.addWidget(self.form_status_label)
        self.form_progress_bar = QProgressBar(profile_card)
        self.form_progress_bar.setObjectName("formProgressBar")
        self.form_progress_bar.setAccessibleName("답안지 양식 확인 진행률")
        self.form_progress_bar.setTextVisible(False)
        self.form_progress_bar.hide()
        profile_layout.addWidget(self.form_progress_bar)
        profile_row = QHBoxLayout()
        self.profile_combo = QComboBox(profile_card)
        self.profile_combo.setObjectName("profileCombo")
        self.profile_combo.setAccessibleName("OMR 프로필 선택")
        self.profile_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.profile_combo.setMinimumContentsLength(20)
        self.profile_combo.addItem("OMR 프로필을 선택하세요", None)
        self.profile_combo.setProperty("attention", False)
        self.profile_import_button = QPushButton("다른 프로필 불러오기", profile_card)
        self.profile_import_button.setObjectName("profileImportButton")
        self.profile_import_button.setAccessibleName("외부 OMR 프로필 불러오기")
        self.profile_import_button.setProperty("attention", False)
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(self.profile_import_button)
        profile_layout.addLayout(profile_row)
        self.profile_summary = QLabel("검증된 OMR 프로필을 선택하거나 불러오세요.", profile_card)
        self.profile_summary.setObjectName("profileSummary")
        self.profile_summary.setAccessibleName("선택한 OMR 프로필 검증 정보")
        self.profile_summary.setWordWrap(True)
        profile_layout.addWidget(self.profile_summary)
        # Hidden: only the card shows the drop target, but this widget validates the file.
        self.profile_widget = profile_card.drop_widget
        sections_layout.addWidget(profile_card)

        settings_card = QFrame(sections)
        settings_card.setObjectName("scanSensitivityCard")
        settings = QHBoxLayout(settings_card)
        settings_title = QLabel("5. 고급 인식 설정", settings_card)
        settings_title.setObjectName("scanSectionLabel")
        settings.addWidget(settings_title)
        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal, settings_card)
        self.sensitivity_slider.setObjectName("sensitivitySlider")
        self.sensitivity_slider.setAccessibleName("스캐너 명암 및 인식 감도")
        self.sensitivity_slider.setRange(1, 10)
        self.sensitivity_slider.setValue(5)
        self.sensitivity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sensitivity_slider.setTickInterval(1)
        settings.addWidget(self.sensitivity_slider, 1)
        self.sensitivity_label = QLabel("인식 수준 5 / 10", settings_card)
        self.sensitivity_label.setObjectName("sensitivityValueLabel")
        self.sensitivity_help = QLabel("낮음  ←  스캐너 명암/인식 감도  →  높음", settings_card)
        self.sensitivity_help.setObjectName("sensitivityHelpLabel")
        settings.addWidget(self.sensitivity_label)
        settings.addWidget(self.sensitivity_help)
        sections_layout.addWidget(settings_card)
        sections_layout.addStretch(1)
        root.addWidget(self.sections_scroll_area, 1)

        progress = QVBoxLayout()
        progress.setSpacing(6)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setObjectName("scanProgressBar")
        self.progress_bar.setAccessibleName("OMR 인식 진행률")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFormat("%v / %m (%p%)")
        self.progress_bar.hide()
        self.progress_label = QLabel("입력 항목을 모두 선택하면 인식을 시작할 수 있습니다.", self)
        self.progress_label.setObjectName("scanProgressLabel")
        self.progress_label.setAccessibleName("현재 인식 상태")
        self.progress_label.setWordWrap(True)
        progress.addWidget(self.progress_bar)
        progress.addWidget(self.progress_label)
        root.addLayout(progress)

        actions = QHBoxLayout()
        self.run_hint_label = QLabel(self)
        self.run_hint_label.setObjectName("scanRunHint")
        self.run_hint_label.setAccessibleName("인식 실행 준비 상태")
        self.run_hint_label.setWordWrap(True)
        self.cancel_button = QPushButton("인식 취소", self)
        self.cancel_button.setObjectName("scanCancelButton")
        self.cancel_button.setAccessibleName("진행 중인 OMR 인식 취소")
        self.run_button = QPushButton("OMR 인식 실행", self)
        self.run_button.setObjectName("scanRunButton")
        self.run_button.setAccessibleName("OMR 시험지 인식 및 응답결과 생성")
        actions.addWidget(self.run_hint_label, 1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.run_button)
        root.addLayout(actions)
        self.session_footer = QLabel("현재 세션: 새 인식 작업", self)
        self.session_footer.setObjectName("scanSessionFooter")
        self.session_footer.setAccessibleName("현재 세션 및 준비 상태")
        root.addWidget(self.session_footer)

        self.reset_button.clicked.connect(self._reset_inputs)
        self.reset_button.clicked.connect(self.reset_requested)
        self.sample_roster_button.clicked.connect(self.sample_roster_requested)
        self.profile_import_button.clicked.connect(self.profile_browse_requested)
        self.profile_widget.browse_requested.connect(self._profile_browse_requested)
        self.profile_widget.selection_changed.connect(self._profile_dropped)
        self.source_widget.selection_changed.connect(self._source_selected)
        self.source_widget.browse_requested.connect(
            lambda _: self.source_browse_requested.emit(ImportKind.PDF)
        )
        self.source_folder_button.clicked.connect(
            lambda: self.source_browse_requested.emit(ImportKind.FOLDER)
        )
        self.source_pdf_button.clicked.connect(
            lambda: self.source_browse_requested.emit(ImportKind.PDF)
        )
        self.roster_widget.selection_changed.connect(self._roster_selected)
        self.roster_widget.browse_requested.connect(self.roster_browse_requested)
        self.sensitivity_slider.valueChanged.connect(self._set_sensitivity_label)
        self.exam_name_edit.textChanged.connect(self._update_gating)
        self._manual_profile_choices = 0
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.profile_combo.activated.connect(self._profile_activated)
        self.run_button.clicked.connect(self._emit_recognition_request)
        self.fresh_response_button.clicked.connect(self._emit_fresh_response_request)
        self.cancel_button.clicked.connect(self._emit_cancel_request)
        self._show_form_hint()

    def set_profiles(self, profiles: Iterable[ValidatedProfileState]) -> None:
        """Render controller-validated profile values without inspecting any path."""
        values = tuple(profiles)
        if any(not isinstance(profile, ValidatedProfileState) for profile in values):
            raise TypeError("profiles must contain ValidatedProfileState values")
        self._profiles = values
        current = self._selected_profile()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("OMR 프로필을 선택하세요", None)
        for profile in values:
            self.profile_combo.addItem(profile.name, profile)
        index = self.profile_combo.findData(current) if current is not None else 0
        if index <= 0:
            index = next(
                (
                    position
                    for position, profile in enumerate(values, start=1)
                    if profile.path == self._default_profile_name and profile.validated
                ),
                next(
                    (
                        position
                        for position, profile in enumerate(values, start=1)
                        if profile.is_default and profile.validated
                    ),
                    0,
                ),
            )
        self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self._profile_changed()

    def set_defaults(self, default_profile: str, sensitivity: int) -> None:
        """Apply the committed settings snapshot to future/new scan forms only."""
        if not isinstance(default_profile, str):
            raise TypeError("default_profile must be a string")
        if type(sensitivity) is not int or not 1 <= sensitivity <= 10:
            raise ValueError("sensitivity must be an integer from 1 through 10")
        self._default_profile_name = default_profile or None
        self._default_sensitivity = sensitivity
        if not self._busy:
            self.sensitivity_slider.setValue(sensitivity)
            if self._default_profile_name is not None:
                for index in range(1, self.profile_combo.count()):
                    profile = self.profile_combo.itemData(index)
                    if (
                        isinstance(profile, ValidatedProfileState)
                        and profile.path == self._default_profile_name
                        and profile.validated
                    ):
                        self.profile_combo.setCurrentIndex(index)
                        break

    def set_roster(self, roster_path: str | None, count: int | None = None) -> None:
        if roster_path is not None and (not isinstance(roster_path, str) or not roster_path):
            raise ValueError("roster_path must be a non-empty string or None")
        self._roster_path = roster_path
        if roster_path is None:
            self.roster_widget.clear()
            self.roster_status.setText("명단이 없으면 이름은 ‘미등록’으로 표시됩니다.")
        else:
            if self.roster_widget.set_selection((roster_path,)):
                suffix = f" ({count}명)" if isinstance(count, int) and count >= 0 else ""
                self.roster_status.setText(f"명단 연결됨{suffix}")
                self.roster_status.setToolTip(roster_path)
            else:
                self._roster_path = None
                self.roster_status.setText("명단 파일 형식을 확인하세요.")
        self._update_gating()

    def set_source(self, source: ImportSelection | None) -> None:
        if source is not None and (
            not isinstance(source, ImportSelection)
            or source.kind not in (ImportKind.FOLDER, ImportKind.PDF)
        ):
            raise TypeError("source must be a folder or PDF ImportSelection, or None")
        self._source = source
        if source is None:
            self.source_widget.clear()
        else:
            if not self.source_widget.set_selection(source.paths):
                self._source = None
        if self._source is None:
            # Whatever the form check said described the scans that are gone now.
            self._show_form_hint()
        self._update_gating()

    def set_source_picker_cancelled(self) -> None:
        self.source_widget.set_picker_cancelled()

    def set_roster_picker_cancelled(self) -> None:
        self.roster_widget.set_picker_cancelled()

    def set_profile_picker_cancelled(self) -> None:
        self.profile_widget.set_picker_cancelled()

    def set_profile_importing(self, source_path: str) -> None:
        if not isinstance(source_path, str) or not source_path:
            raise ValueError("source_path must be a non-empty string")
        self._profile_importing = True
        self.profile_summary.setText(
            "프로필명: 확인 중  |  상태: 불러오는 중  |  정상유무: 확인 중"
        )
        self.profile_summary.setToolTip(source_path)
        self.progress_label.setText("OMR 프로필을 검증하고 안전하게 저장하는 중입니다.")
        self._update_gating()

    def select_profile(self, path: str) -> bool:
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        for index in range(1, self.profile_combo.count()):
            profile = self.profile_combo.itemData(index)
            if isinstance(profile, ValidatedProfileState) and profile.path == path:
                self._profile_importing = False
                self.profile_combo.setCurrentIndex(index)
                self._set_form_attention(False)
                self.progress_label.setText(
                    f"'{profile.name}'을(를) 인식 프로필로 지정했습니다."
                )
                self._update_gating()
                return True
        self.set_profile_import_error("불러온 OMR 프로필을 목록에서 찾을 수 없습니다.")
        return False

    def set_profile_import_error(self, message: str) -> None:
        if not isinstance(message, str) or not message:
            raise ValueError("message must be a non-empty string")
        self._profile_importing = False
        self.profile_summary.setText(
            "프로필명: -  |  상태: 불러오기 실패  |  정상유무: 비정상"
        )
        self.profile_summary.setToolTip(message)
        self.progress_label.setText(message)
        self._update_gating()

    def current_source(self) -> ImportSelection | None:
        """The scan source the next recognition would read."""
        return self._source

    def set_form_detecting(self) -> None:
        """Show that the answer-sheet form is being identified; scanning waits for it."""
        self._begin_form_detection()
        self.form_progress_bar.setRange(0, 0)
        self._set_form_status("답안지 양식을 확인하는 중입니다…", "", "⏳")
        self._update_gating()

    def set_form_detection_progress(self, pages_done: int, pages_total: int) -> None:
        """Show how many pages the form check has read; after the last one it tidies up."""
        if type(pages_done) is not int or type(pages_total) is not int:
            raise TypeError("pages_done and pages_total must be integers")
        if pages_done < 0 or pages_total < 0:
            raise ValueError("pages_done and pages_total must be non-negative")
        self._begin_form_detection()
        if pages_done < pages_total:
            self.form_progress_bar.setRange(0, pages_total)
            self.form_progress_bar.setValue(pages_done)
            self._set_form_status(
                f"답안지 양식을 확인하는 중 ({pages_done} / {pages_total}쪽)", "", "⏳"
            )
        else:
            self.form_progress_bar.setRange(0, 0)
            self._set_form_status("확인한 쪽으로 양식을 정리하는 중…", "", "⏳")
        self._update_gating()

    def set_form_detected(self, text: str, *, warning: bool = False) -> None:
        """Show the detected form; `warning` marks a result that needs a second look."""
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        if type(warning) is not bool:
            raise TypeError("warning must be a bool")
        self._end_form_detection()
        self._set_form_status(text, "warning" if warning else "success", "⚠" if warning else "✓")
        self._update_gating()

    def set_form_detection_failed(self, message: str) -> None:
        """Show why no form was set and point at the controls for choosing a profile by hand."""
        if not isinstance(message, str) or not message:
            raise ValueError("message must be a non-empty string")
        self._end_form_detection()
        self._set_form_status(message, "error", "✗")
        self._set_form_attention(True)
        self._update_gating()

    def _begin_form_detection(self) -> None:
        self._form_detecting = True
        self._set_form_attention(False)
        self.form_progress_bar.show()

    def _end_form_detection(self) -> None:
        self._form_detecting = False
        self._set_form_attention(False)
        self.form_progress_bar.hide()

    def _show_form_hint(self) -> None:
        self._end_form_detection()
        self._set_form_status("스캔을 선택하면 인식 프로필이 자동으로 지정됩니다.", "hint")

    def _set_form_status(self, text: str, role: str, symbol: str = "") -> None:
        self.form_status_label.setText(f"{symbol} {text}" if symbol else text)
        self._set_role(self.form_status_label, role)

    def _set_form_attention(self, attention: bool) -> None:
        """Highlight the manual profile controls while no profile could be set for the scans."""
        for widget in (self.profile_combo, self.profile_import_button):
            if widget.property("attention") != attention:
                widget.setProperty("attention", attention)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    @staticmethod
    def _set_role(widget: QWidget, role: str) -> None:
        if widget.property("role") != role:
            widget.setProperty("role", role)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def set_session(self, session_id: str | None, label: str | None = None) -> None:
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError("session_id must be a non-empty string or None")
        self._session_id = session_id
        self.session_footer.setText(f"현재 세션: {label or session_id or '새 인식 작업'}")

    def set_write_enabled(self, enabled: bool, reason: str | None = None) -> None:
        self._write_enabled = bool(enabled)
        if not self._write_enabled:
            self.progress_label.setText(
                reason or "실행 폴더에 쓸 수 없어 새 인식 작업을 시작할 수 없습니다."
            )
        self._update_gating()

    def set_busy(
        self, busy: bool, operation_id: str | None = None, *, cancellable: bool = True
    ) -> None:
        self._busy = bool(busy)
        self._operation_id = operation_id if self._busy else None
        self._cancellable = bool(cancellable) if self._busy else True
        self.progress_bar.setVisible(self._busy)
        if self._busy:
            self._progress_phase = None
            self._busy_text = (
                "OMR 인식을 준비하고 있습니다. 취소할 수 있습니다."
                if self._cancellable
                else "응답 결과를 안전하게 가져오고 있습니다."
            )
            self.progress_bar.setRange(0, 0)
            self._start_progress_clock()
            self._refresh_progress()
        else:
            self._stop_progress_clock()
        self._update_gating()

    def set_progress(
        self,
        completed: int,
        total: int,
        failed: int = 0,
        *,
        elapsed_seconds: float | int | None = None,
        eta_seconds: float | int | None = None,
        phase: str = "recognize",
        **_: object,
    ) -> None:
        """Show one worker progress event; the elapsed time comes from this page's own clock."""
        if total < 0 or completed < 0 or failed < 0:
            raise ValueError("progress values must be non-negative")
        if completed + failed > total:
            raise ValueError("completed and failed counts cannot exceed total")
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int | float) or value < 0)
            for value in (elapsed_seconds, eta_seconds)
        ):
            raise ValueError("elapsed_seconds and eta_seconds must be non-negative numbers or None")
        if phase not in _PROGRESS_PHASES:
            raise ValueError("phase must be one of prepare, recognize or save")
        self._busy = True
        self._start_progress_clock()
        self._progress_phase = phase
        self._progress_counts = (completed, total, failed)
        self._progress_eta = None if eta_seconds is None else float(eta_seconds)
        self._progress_event_at = monotonic()
        self.progress_bar.show()
        if phase == "save":
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setFormat("%v / %m (%p%)")
            self.progress_bar.setValue(completed if phase == "prepare" else completed + failed)
        self._refresh_progress()
        self._update_gating()

    def set_result(
        self,
        result: object | None = None,
        message: str = "OMR 인식과 응답결과 생성이 완료되었습니다.",
    ) -> None:
        self._busy = False
        self._operation_id = None
        self._stop_progress_clock()
        self.progress_bar.hide()
        self.progress_label.setText(message)
        self._update_gating()

    def set_error(self, error: object | None = None, message: str | None = None) -> None:
        preserve_progress = not self.progress_bar.isHidden() and self.progress_bar.maximum() > 0
        completed = self.progress_bar.value()
        total = self.progress_bar.maximum()
        self._busy = False
        self._operation_id = None
        self._stop_progress_clock()
        text = message or (str(error) if error else "OMR 인식 중 오류가 발생했습니다.")
        if preserve_progress:
            percent = round(completed * 100 / total)
            text += f"\n마지막 진행 {completed} / {total} ({percent}%)"
            self.progress_bar.show()
        else:
            self.progress_bar.hide()
        self.progress_label.setText(text)
        self._update_gating()

    def set_cancelled(self, message: str = "OMR 인식이 취소되었습니다.") -> None:
        """Finish cancellation and restore all editable inputs."""
        self._busy = False
        self._operation_id = None
        self._stop_progress_clock()
        self.progress_bar.hide()
        self.progress_label.setText(message)
        self._update_gating()

    def _start_progress_clock(self) -> None:
        if self._busy_started is None:
            self._busy_started = monotonic()
        if not self._progress_timer.isActive():
            self._progress_timer.start()

    def _stop_progress_clock(self) -> None:
        self._progress_timer.stop()
        self._busy_started = None
        self._progress_phase = None

    def _refresh_progress(self) -> None:
        """Redraw the running operation's status line from the page's own clock."""
        started = self._busy_started
        if started is None:
            return
        now = monotonic()
        elapsed = self._format_duration(now - started)
        completed, total, failed = self._progress_counts
        processed = completed + failed
        if self._progress_phase is None:
            text = self._busy_text
            if now - started >= 1:
                text += f" (경과 {elapsed})"
        elif self._progress_phase == "prepare":
            text = f"스캔 파일을 준비하는 중 ({completed} / {total}쪽) · 경과 {elapsed}"
        elif self._progress_phase == "recognize":
            text = f"답안지를 판독하는 중 ({processed} / {total}쪽) · 경과 {elapsed}"
            if self._progress_eta is not None and 0 < processed < total:
                # The estimate only arrives with events, so it counts down in between.
                remaining = self._progress_eta - (now - self._progress_event_at)
                if remaining >= 1:
                    text += f" · 남은 시간 약 {self._format_duration(remaining)}"
            if processed:
                text += f" (성공 {completed}, 확인 필요 {failed})"
        else:
            text = f"인식 결과를 저장하는 중… · 경과 {elapsed}"
        self.progress_label.setText(text)

    def _source_selected(self, selection: ImportSelection) -> None:
        if self._busy or selection.kind not in (ImportKind.FOLDER, ImportKind.PDF):
            return
        self._source = selection
        self._update_gating()
        # set_source() also arrives here (the drop widget emits selection_changed), so this
        # one emit covers drops, browsing and programmatic selection exactly once.
        self.source_changed.emit(selection)

    def _roster_selected(self, selection: ImportSelection) -> None:
        if self._busy or selection.kind is not ImportKind.ROSTER:
            return
        self._roster_path = selection.paths[0]
        self.roster_status.setText("명단 선택됨")
        self.roster_status.setToolTip(self._roster_path)

    def _profile_browse_requested(self, _: ImportKind) -> None:
        if not self._busy:
            self.profile_browse_requested.emit()

    def _profile_dropped(self, selection: ImportSelection) -> None:
        if self._busy or selection.kind is not ImportKind.PROFILE:
            return
        self.profile_drop_requested.emit(selection)
        self.profile_import_requested.emit(selection)

    def _selected_profile(self) -> ValidatedProfileState | None:
        value = self.profile_combo.currentData()
        return value if isinstance(value, ValidatedProfileState) else None

    def _profile_activated(self, *_: object) -> None:
        self._manual_profile_choices += 1
        self._set_form_attention(False)

    @property
    def manual_profile_choices(self) -> int:
        """How many times the user has picked a profile in the list by hand."""
        return self._manual_profile_choices

    def _profile_changed(self, *_: object) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.profile_summary.setText("검증된 OMR 프로필을 선택하거나 불러오세요.")
            self.profile_summary.setToolTip("")
        else:
            dimensions = (
                f"기준 크기 {profile.dimensions[0]} × {profile.dimensions[1]}"
                if profile.dimensions is not None
                else "기준 크기 정보 없음"
            )
            default = " · 기본 프로필" if profile.is_default else ""
            duplicate = (
                f" · 중복 처리: {profile.duplicate_outcome}"
                if profile.duplicate_outcome is not None
                else ""
            )
            normal = profile.validated and not profile.validation_errors
            status = (
                "검증 완료"
                if normal
                else "검증 실패"
                if profile.validation_errors
                else "검증되지 않음"
            )
            self.profile_summary.setText(
                f"프로필명: {profile.name}  |  상태: {status}  |  "
                f"정상유무: {'정상' if normal else '비정상'}"
            )
            errors = (
                f" · 오류: {' / '.join(profile.validation_errors)}"
                if profile.validation_errors
                else ""
            )
            self.profile_summary.setToolTip(
                f"{dimensions} · {profile.grid_summary}{default}{duplicate}{errors}"
            )
        self._update_gating()

    def _set_sensitivity_label(self, value: int) -> None:
        self.sensitivity_label.setText(f"인식 수준 {value} / 10")

    def _reset_inputs(self) -> None:
        if self._busy or not self._write_enabled:
            return
        self.exam_name_edit.clear()
        self.profile_combo.setCurrentIndex(0)
        self._source = None
        self._roster_path = None
        self.source_widget.clear()
        self.roster_widget.clear()
        self.profile_widget.clear()
        self.sensitivity_slider.setValue(self._default_sensitivity)
        if self._default_profile_name is not None:
            for index in range(1, self.profile_combo.count()):
                profile = self.profile_combo.itemData(index)
                if (
                    isinstance(profile, ValidatedProfileState)
                    and profile.path == self._default_profile_name
                    and profile.validated
                ):
                    self.profile_combo.setCurrentIndex(index)
                    break
        self.roster_status.setText("명단이 없으면 이름은 ‘미등록’으로 표시됩니다.")
        self.progress_label.setText("입력 항목을 모두 선택하면 인식을 시작할 수 있습니다.")
        self._show_form_hint()
        self._update_gating()

    def _run_blocker(self) -> str | None:
        """The hint for the first unmet run condition, or None when a run may start."""
        if not self._write_enabled:
            return "실행 폴더에 쓸 수 없어 인식을 시작할 수 없습니다."
        if self._busy:
            return ""
        has_name = bool(self.exam_name_edit.text().strip())
        has_source = self._source is not None and self._source.kind in (
            ImportKind.FOLDER,
            ImportKind.PDF,
        )
        if not has_name and not has_source:
            return "시험명과 스캔을 고르면 실행할 수 있습니다."
        if not has_name:
            return "시험명을 입력하면 실행할 수 있습니다."
        if not has_source:
            return "스캔 파일이나 폴더를 고르면 실행할 수 있습니다."
        if self._form_detecting:
            return "인식 프로필을 확인하는 중입니다."
        if self._profile_importing:
            return "프로필을 불러오는 중입니다."
        profile = self._selected_profile()
        if profile is None or not profile.validated or profile.validation_errors:
            return "인식 프로필을 고르면 실행할 수 있습니다."
        return None

    def _can_run(self) -> bool:
        return self._run_blocker() is None

    @staticmethod
    def _format_duration(seconds: float | int) -> str:
        total_seconds = max(0, int(seconds))
        if total_seconds < 60:
            return f"{total_seconds}초"
        minutes, remainder = divmod(total_seconds, 60)
        if minutes < 60:
            return f"{minutes}분 {remainder}초"
        return f"{minutes // 60}시간 {minutes % 60}분"

    def _update_gating(self, *_: object) -> None:
        blocker = self._run_blocker()
        self.run_button.setEnabled(blocker is None)
        self.run_hint_label.setText(
            "준비되었습니다. 'OMR 인식 실행'을 누르세요." if blocker is None else blocker
        )
        self._set_role(
            self.run_hint_label,
            "success" if blocker is None else "hint" if self._write_enabled else "error",
        )
        self.fresh_response_button.setEnabled(self._write_enabled and not self._busy)
        # Once results are being saved the exam is created either way, so a cancel
        # there would only claim a cancellation that did not happen.
        self.cancel_button.setEnabled(
            self._busy and self._cancellable and self._progress_phase != "save"
        )
        editable = self._write_enabled and not self._busy
        for widget in (
            self.exam_name_edit,
            self.profile_combo,
            self.profile_import_button,
            self.profile_widget,
            self.source_folder_button,
            self.source_pdf_button,
            self.source_widget,
            self.roster_widget,
            self.sensitivity_slider,
            self.sample_roster_button,
            self.reset_button,
        ):
            widget.setEnabled(editable)
        for widget in (self.profile_combo, self.profile_import_button, self.profile_widget):
            widget.setEnabled(editable and not self._profile_importing)

    def _emit_recognition_request(self) -> None:
        profile = self._selected_profile()
        if not self._can_run() or self._source is None or profile is None:
            return
        self.recognition_requested.emit(
            ScanPageRequest(
                exam_name=self.exam_name_edit.text().strip(),
                profile=profile,
                roster_path=self._roster_path,
                source=self._source,
                sensitivity=self.sensitivity_slider.value(),
                session_id=self._session_id,
            )
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.fresh_response_button
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            self.fresh_response_button.click()
            return True
        return super().eventFilter(watched, event)

    def _emit_fresh_response_request(self) -> None:
        if self._write_enabled and not self._busy:
            self.fresh_response_requested.emit()

    def _emit_cancel_request(self) -> None:
        if self._busy and self._cancellable:
            self.cancel_requested.emit(self._operation_id)
