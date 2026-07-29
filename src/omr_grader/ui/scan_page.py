"""Screen 1: collect scan inputs and emit immutable recognition requests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .import_widgets import ImportDropWidget, ImportKind, ImportSelection


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


class ScanPage(QWidget):
    """Input screen with no filesystem, OCR, or workbook work on the UI thread."""

    recognition_requested = Signal(object)
    fresh_response_requested = Signal()
    cancel_requested = Signal(object)
    help_requested = Signal()
    reset_requested = Signal()
    sample_roster_requested = Signal()
    source_browse_requested = Signal(object)
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
        self._operation_id: str | None = None
        self._cancellable = True
        self._build_ui()
        self._update_gating()
        self._default_sensitivity = 3
        self._default_profile_name: str | None = None

    def _build_ui(self) -> None:
        self.setObjectName("scanPage")
        self.setAccessibleName("OMR 스캔")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(18)

        top = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("OMR 시험지 인식", self)
        title.setObjectName("scanPageTitle")
        title.setAccessibleName("OMR 시험지 인식")
        subtitle = QLabel("시험 정보와 스캔 파일을 선택하면 응답결과를 생성합니다.", self)
        subtitle.setObjectName("scanPageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        top.addLayout(heading)
        top.addStretch()
        self.fresh_response_button = QPushButton("응답 엑셀로 시작", self)
        self.fresh_response_button.setObjectName("freshResponseButton")
        self.fresh_response_button.setAccessibleName("응답 엑셀로 새 세션 시작")
        self.fresh_response_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.fresh_response_button.installEventFilter(self)
        self.help_button = QPushButton("도움말 / 사용 설명서", self)
        self.help_button.setObjectName("scanHelpButton")
        self.help_button.setAccessibleName("도움말 및 사용 설명서")
        self.reset_button = QPushButton("초기화 / 재설정", self)
        self.reset_button.setObjectName("scanResetButton")
        self.reset_button.setAccessibleName("입력 초기화 및 재설정")
        top.addWidget(self.fresh_response_button)
        top.addWidget(self.help_button)
        top.addWidget(self.reset_button)
        root.addLayout(top)

        exam_card = QFrame(self)
        exam_card.setObjectName("scanExamCard")
        exam_form = QFormLayout(exam_card)
        self.exam_name_edit = QLineEdit(exam_card)
        self.exam_name_edit.setObjectName("examNameEdit")
        self.exam_name_edit.setAccessibleName("시험명")
        self.exam_name_edit.setPlaceholderText("예: 26-2 생리학 중간고사")
        self.profile_combo = QComboBox(exam_card)
        self.profile_combo.setObjectName("profileCombo")
        self.profile_combo.setAccessibleName("OMR 프로필 선택")
        self.profile_combo.addItem("OMR 프로필을 선택하세요", None)
        self.profile_import_button = QPushButton("OMR 프로필 불러오기", exam_card)
        self.profile_import_button.setObjectName("profileImportButton")
        self.profile_import_button.setAccessibleName("외부 OMR 프로필 불러오기")
        profile_row = QWidget(exam_card)
        profile_layout = QHBoxLayout(profile_row)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.addWidget(self.profile_combo, 1)
        profile_layout.addWidget(self.profile_import_button)
        self.profile_widget = ImportDropWidget(ImportKind.PROFILE, exam_card)
        self.profile_widget.setObjectName("profileImportWidget")
        self.profile_summary = QLabel("검증된 OMR 프로필을 선택하거나 불러오세요.", exam_card)
        self.profile_summary.setObjectName("profileSummary")
        self.profile_summary.setAccessibleName("선택한 OMR 프로필 검증 정보")
        self.profile_summary.setWordWrap(True)
        exam_form.addRow("1. 시험명 입력 *", self.exam_name_edit)
        exam_form.addRow("인식 프로필 *", profile_row)
        exam_form.addRow("프로필 끌어놓기", self.profile_widget)
        exam_form.addRow("프로필 정보", self.profile_summary)
        root.addWidget(exam_card)

        roster_card = QFrame(self)
        roster_card.setObjectName("scanRosterCard")
        roster_layout = QHBoxLayout(roster_card)
        roster_text = QVBoxLayout()
        roster_title = QLabel("2. 응시 학생 명단 업로드 (선택 사항)", roster_card)
        roster_title.setObjectName("rosterTitle")
        self.roster_status = QLabel("명단이 없으면 이름은 ‘미등록’으로 표시됩니다.", roster_card)
        self.roster_status.setObjectName("rosterStatus")
        roster_text.addWidget(roster_title)
        roster_text.addWidget(self.roster_status)
        roster_layout.addLayout(roster_text, 1)
        self.sample_roster_button = QPushButton("샘플 명단 내려받기", roster_card)
        self.sample_roster_button.setObjectName("sampleRosterButton")
        self.sample_roster_button.setAccessibleName("샘플 응시 학생 명단 내려받기")
        self.roster_widget = ImportDropWidget(ImportKind.ROSTER, roster_card)
        self.roster_widget.setObjectName("rosterImportWidget")
        self.roster_widget.setFixedWidth(270)
        roster_layout.addWidget(self.sample_roster_button)
        roster_layout.addWidget(self.roster_widget)
        root.addWidget(roster_card)

        source_card = QFrame(self)
        source_card.setObjectName("scanSourceCard")
        source_layout = QVBoxLayout(source_card)
        source_layout.addWidget(QLabel("3. 스캔 파일/폴더 선택 *", source_card))
        mode_row = QHBoxLayout()
        self.folder_radio = QRadioButton("이미지 폴더 선택 (JPG, PNG)", source_card)
        self.folder_radio.setObjectName("folderModeRadio")
        self.folder_radio.setAccessibleName("이미지 폴더 선택")
        self.pdf_radio = QRadioButton("PDF 파일 선택", source_card)
        self.pdf_radio.setObjectName("pdfModeRadio")
        self.pdf_radio.setAccessibleName("PDF 파일 선택")
        self.folder_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.folder_radio)
        self._mode_group.addButton(self.pdf_radio)
        mode_row.addWidget(self.folder_radio)
        mode_row.addWidget(self.pdf_radio)
        mode_row.addStretch()
        source_layout.addLayout(mode_row)
        self.source_widget = ImportDropWidget(ImportKind.FOLDER, source_card)
        self.source_widget.setObjectName("scanSourceImportWidget")
        source_layout.addWidget(self.source_widget)
        root.addWidget(source_card)

        settings_card = QFrame(self)
        settings_card.setObjectName("scanSensitivityCard")
        settings = QHBoxLayout(settings_card)
        settings.addWidget(QLabel("4. 고급 인식 설정", settings_card))
        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal, settings_card)
        self.sensitivity_slider.setObjectName("sensitivitySlider")
        self.sensitivity_slider.setAccessibleName("스캐너 명암 및 인식 감도")
        self.sensitivity_slider.setRange(1, 10)
        self.sensitivity_slider.setValue(3)
        self.sensitivity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sensitivity_slider.setTickInterval(1)
        settings.addWidget(self.sensitivity_slider, 1)
        self.sensitivity_label = QLabel("인식 수준 3 / 10", settings_card)
        self.sensitivity_label.setObjectName("sensitivityValueLabel")
        self.sensitivity_help = QLabel("낮음  ←  스캐너 명암/인식 감도  →  높음", settings_card)
        self.sensitivity_help.setObjectName("sensitivityHelpLabel")
        settings.addWidget(self.sensitivity_label)
        settings.addWidget(self.sensitivity_help)
        root.addWidget(settings_card)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setObjectName("scanProgressBar")
        self.progress_bar.setAccessibleName("OMR 인식 진행률")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        self.progress_label = QLabel("입력 항목을 모두 선택하면 인식을 시작할 수 있습니다.", self)
        self.progress_label.setObjectName("scanProgressLabel")
        self.progress_label.setAccessibleName("현재 인식 상태")
        root.addWidget(self.progress_bar)
        root.addWidget(self.progress_label)
        self.session_footer = QLabel("현재 세션: 새 인식 작업", self)
        self.session_footer.setObjectName("scanSessionFooter")
        self.session_footer.setAccessibleName("현재 세션 및 준비 상태")
        root.addWidget(self.session_footer)

        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("인식 취소", self)
        self.cancel_button.setObjectName("scanCancelButton")
        self.cancel_button.setAccessibleName("진행 중인 OMR 인식 취소")
        self.run_button = QPushButton("OMR 인식 실행", self)
        self.run_button.setObjectName("scanRunButton")
        self.run_button.setAccessibleName("OMR 시험지 인식 및 응답결과 생성")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.run_button)
        root.addLayout(actions)

        self.help_button.clicked.connect(self.help_requested)
        self.reset_button.clicked.connect(self._reset_inputs)
        self.reset_button.clicked.connect(self.reset_requested)
        self.sample_roster_button.clicked.connect(self.sample_roster_requested)
        self.profile_import_button.clicked.connect(self.profile_browse_requested)
        self.profile_widget.browse_requested.connect(self._profile_browse_requested)
        self.profile_widget.selection_changed.connect(self._profile_dropped)
        self.folder_radio.toggled.connect(self._set_source_mode)
        self.source_widget.selection_changed.connect(self._source_selected)
        self.source_widget.browse_requested.connect(self.source_browse_requested)
        self.roster_widget.selection_changed.connect(self._roster_selected)
        self.roster_widget.browse_requested.connect(self.roster_browse_requested)
        self.sensitivity_slider.valueChanged.connect(self._set_sensitivity_label)
        self.exam_name_edit.textChanged.connect(self._update_gating)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.run_button.clicked.connect(self._emit_recognition_request)
        self.fresh_response_button.clicked.connect(self._emit_fresh_response_request)
        self.cancel_button.clicked.connect(self._emit_cancel_request)

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
                self.roster_status.setText(f"명단 연결됨: {roster_path}{suffix}")
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
            if source.kind is not self.source_widget.kind:
                self.folder_radio.setChecked(source.kind is ImportKind.FOLDER)
                self.pdf_radio.setChecked(source.kind is ImportKind.PDF)
            if not self.source_widget.set_selection(source.paths):
                self._source = None
        self._update_gating()

    def set_source_picker_cancelled(self) -> None:
        self.source_widget.set_picker_cancelled()

    def set_roster_picker_cancelled(self) -> None:
        self.roster_widget.set_picker_cancelled()

    def set_profile_picker_cancelled(self) -> None:
        self.profile_widget.set_picker_cancelled()

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
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(
                "OMR 인식을 준비하고 있습니다. 취소할 수 있습니다."
                if self._cancellable
                else "응답 결과를 안전하게 가져오고 있습니다."
            )
        self._update_gating()

    def set_progress(
        self,
        completed: int,
        total: int,
        failed: int = 0,
        *,
        elapsed_seconds: float | int | None = None,
        eta_seconds: float | int | None = None,
        **_: object,
    ) -> None:
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
        self._busy = True
        self.progress_bar.show()
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(completed + failed)
        timing = []
        if elapsed_seconds is not None:
            timing.append(f"경과 {self._format_duration(elapsed_seconds)}")
        if eta_seconds is not None:
            timing.append(f"예상 남은 시간 {self._format_duration(eta_seconds)}")
        suffix = f" · {' · '.join(timing)}" if timing else ""
        progress_text = (
            f"현재 처리 중: {completed + failed} / {total} "
            f"(성공 {completed}, 확인 필요 {failed}){suffix}"
        )
        self.progress_label.setText(progress_text)
        self._update_gating()

    def set_result(
        self,
        result: object | None = None,
        message: str = "OMR 인식과 응답결과 생성이 완료되었습니다.",
    ) -> None:
        self._busy = False
        self._operation_id = None
        self.progress_bar.hide()
        self.progress_label.setText(message)
        self._update_gating()

    def set_error(self, error: object | None = None, message: str | None = None) -> None:
        self._busy = False
        self._operation_id = None
        self.progress_bar.hide()
        text = message or (str(error) if error else "OMR 인식 중 오류가 발생했습니다.")
        self.progress_label.setText(text)
        self._update_gating()

    def set_cancelled(self, message: str = "OMR 인식이 취소되었습니다.") -> None:
        """Finish cancellation and restore all editable inputs."""
        self._busy = False
        self._operation_id = None
        self.progress_bar.hide()
        self.progress_label.setText(message)
        self._update_gating()

    def _set_source_mode(self, folder_mode: bool) -> None:
        if self._busy:
            return
        kind = ImportKind.FOLDER if folder_mode else ImportKind.PDF
        self._source = None
        self.source_widget.set_kind(kind)
        self._update_gating()

    def _source_selected(self, selection: ImportSelection) -> None:
        if self._busy or selection.kind is not self.source_widget.kind:
            return
        self._source = selection
        self._update_gating()

    def _roster_selected(self, selection: ImportSelection) -> None:
        if self._busy or selection.kind is not ImportKind.ROSTER:
            return
        self._roster_path = selection.paths[0]
        self.roster_status.setText(f"명단 선택됨: {self._roster_path}")

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

    def _profile_changed(self, *_: object) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.profile_summary.setText("검증된 OMR 프로필을 선택하거나 불러오세요.")
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
            validation = (
                "검증 완료"
                if profile.validated
                else f"검증 실패: {' / '.join(profile.validation_errors) or '검증되지 않음'}"
            )
            self.profile_summary.setText(
                f"{profile.name}\n{dimensions} · {profile.grid_summary}\n"
                f"{validation}{default}{duplicate}"
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
        self._update_gating()

    def _can_run(self) -> bool:
        profile = self._selected_profile()
        return (
            self._write_enabled
            and not self._busy
            and bool(self.exam_name_edit.text().strip())
            and profile is not None
            and profile.validated
            and not profile.validation_errors
            and self._source is not None
            and self._source.kind is self.source_widget.kind
        )

    @staticmethod
    def _format_duration(seconds: float | int) -> str:
        total_seconds = int(seconds)
        minutes, remainder = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{remainder:02d}"

    def _update_gating(self, *_: object) -> None:
        self.run_button.setEnabled(self._can_run())
        self.fresh_response_button.setEnabled(self._write_enabled and not self._busy)
        self.cancel_button.setEnabled(self._busy and self._cancellable)
        editable = self._write_enabled and not self._busy
        for widget in (
            self.exam_name_edit,
            self.profile_combo,
            self.profile_import_button,
            self.profile_widget,
            self.folder_radio,
            self.pdf_radio,
            self.source_widget,
            self.roster_widget,
            self.sensitivity_slider,
            self.sample_roster_button,
            self.reset_button,
        ):
            widget.setEnabled(editable)

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
