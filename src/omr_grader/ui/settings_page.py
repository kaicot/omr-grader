"""Screen 4: controller-driven portable settings editor with no file access."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from omr_grader.application.dto import Settings, SettingsSaveResult

from .import_widgets import ImportKind, ImportSelection


@dataclass(frozen=True, slots=True)
class SettingsProfileCandidate:
    """A controller-validated profile value, safe for passive display only."""

    name: str
    valid: bool
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        if type(self.valid) is not bool:
            raise TypeError("valid must be bool")
        if self.diagnostic is not None and (
            not isinstance(self.diagnostic, str) or not self.diagnostic
        ):
            raise ValueError("diagnostic must be a non-empty string or None")
        if self.valid and self.diagnostic is not None:
            raise ValueError("valid candidates cannot have a diagnostic")


@dataclass(frozen=True, slots=True)
class SettingsPageRequest:
    """Immutable UI intent; the controller supplies the operation identifier."""

    settings: Settings
    expected_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.settings, Settings):
            raise TypeError("settings must be Settings")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")


class SettingsPage(QWidget):
    """Settings controls that emit values only and never inspect config or profile paths."""

    save_requested = Signal(object)
    profile_browse_requested = Signal()
    profile_import_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._candidates: tuple[SettingsProfileCandidate, ...] = ()
        self._saved_settings: Settings | None = None
        self._expected_revision: int | None = None
        self._configured_default: str | None = None
        self._write_enabled = True
        self._write_revoked = False
        self._settings_ready = False
        self._busy = False
        self._build_ui()
        self._refresh_gating()

    def _build_ui(self) -> None:
        self.setObjectName("settingsPage")
        self.setAccessibleName("환경 설정")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        title = QLabel("환경 설정", self)
        title.setObjectName("settingsPageTitle")
        title.setProperty("role", "page-title")
        title.setAccessibleName("환경 설정")
        root.addWidget(title)
        subtitle = QLabel("포터블 실행 폴더의 기본 인식 설정을 관리합니다.", self)
        subtitle.setObjectName("settingsPageSubtitle")
        root.addWidget(subtitle)

        path_card = QFrame(self)
        path_card.setObjectName("settingsPortablePathCard")
        path_form = QFormLayout(path_card)
        self.data_path_edit = QLineEdit(path_card)
        self.data_path_edit.setObjectName("portableDataPathEdit")
        self.data_path_edit.setAccessibleName("포터블 데이터 저장 경로")
        self.data_path_edit.setReadOnly(True)
        self.data_path_edit.setText("현재 실행 폴더 내 ./OMR_Grader/ 에 자동 저장됩니다.")
        path_form.addRow("데이터 저장 경로", self.data_path_edit)
        root.addWidget(path_card)

        settings_card = QFrame(self)
        settings_card.setObjectName("settingsOptionsCard")
        form = QFormLayout(settings_card)
        self.profile_combo = QComboBox(settings_card)
        self.profile_combo.setObjectName("settingsProfileCombo")
        self.profile_combo.setAccessibleName("기본 OMR 프로필")
        self.profile_import_button = QPushButton("OMR 프로필 불러오기", settings_card)
        self.profile_import_button.setObjectName("settingsProfileImportButton")
        self.profile_import_button.setAccessibleName("외부 OMR 프로필 불러오기")
        profile_row = QWidget(settings_card)
        profile_layout = QHBoxLayout(profile_row)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.addWidget(self.profile_combo, 1)
        profile_layout.addWidget(self.profile_import_button)
        self.profile_diagnostic_label = QLabel("검증된 프로필을 선택하세요.", settings_card)
        self.profile_diagnostic_label.setObjectName("settingsProfileDiagnostic")
        self.profile_diagnostic_label.setAccessibleName("기본 OMR 프로필 진단")
        self.profile_diagnostic_label.setWordWrap(True)

        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal, settings_card)
        self.sensitivity_slider.setObjectName("settingsSensitivitySlider")
        self.sensitivity_slider.setAccessibleName("기본 스캐너 인식 감도")
        self.sensitivity_slider.setRange(1, 10)
        self.sensitivity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sensitivity_slider.setTickInterval(1)
        self.sensitivity_label = QLabel("인식 수준 3 / 10", settings_card)
        self.sensitivity_label.setObjectName("settingsSensitivityValue")
        sensitivity_row = QWidget(settings_card)
        sensitivity_layout = QHBoxLayout(sensitivity_row)
        sensitivity_layout.setContentsMargins(0, 0, 0, 0)
        sensitivity_layout.addWidget(self.sensitivity_slider, 1)
        sensitivity_layout.addWidget(self.sensitivity_label)

        self.multiprocessing_checkbox = QCheckBox("병렬 처리(멀티프로세싱) 사용", settings_card)
        self.multiprocessing_checkbox.setObjectName("multiprocessingCheckbox")
        self.multiprocessing_checkbox.setAccessibleName("병렬 처리 멀티프로세싱 사용")
        form.addRow("기본 OMR 프로필", profile_row)
        form.addRow("프로필 상태", self.profile_diagnostic_label)
        form.addRow("기본 인식 감도", sensitivity_row)
        form.addRow("성능 설정", self.multiprocessing_checkbox)
        root.addWidget(settings_card)

        self.status_label = QLabel("저장된 설정을 불러오는 중입니다.", self)
        self.status_label.setObjectName("settingsStatusLabel")
        self.status_label.setAccessibleName("설정 저장 상태")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        actions = QHBoxLayout()
        actions.addStretch()
        self.save_button = QPushButton("설정 저장", self)
        self.save_button.setObjectName("settingsSaveButton")
        self.save_button.setAccessibleName("환경 설정 저장")
        self.save_button.setDefault(True)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        root.addStretch()
        QWidget.setTabOrder(self.profile_combo, self.profile_import_button)
        QWidget.setTabOrder(self.profile_import_button, self.sensitivity_slider)
        QWidget.setTabOrder(self.sensitivity_slider, self.multiprocessing_checkbox)
        QWidget.setTabOrder(self.multiprocessing_checkbox, self.save_button)

        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.sensitivity_slider.valueChanged.connect(self._sensitivity_changed)
        self.multiprocessing_checkbox.toggled.connect(self._candidate_changed)
        self.profile_import_button.clicked.connect(self._request_profile_browse)
        self.save_button.clicked.connect(self._emit_save_request)

    def set_data_path_display(self, text: str) -> None:
        """Set controller-provided portable path text; this widget never resolves paths."""
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        self.data_path_edit.setText(text)

    def set_settings(self, settings: Settings, expected_revision: int) -> None:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be Settings")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        self._saved_settings = settings
        self._settings_ready = True
        self._expected_revision = expected_revision
        self._configured_default = settings.default_profile or None
        self.sensitivity_slider.setValue(settings.default_sensitivity)
        self.multiprocessing_checkbox.setChecked(settings.use_multiprocessing)
        self._render_candidates(self._configured_default)
        self.status_label.setText("저장된 설정입니다.")
        self._refresh_gating()
    def set_settings_unavailable(self, diagnostic: str) -> None:
        if not isinstance(diagnostic, str) or not diagnostic:
            raise ValueError("diagnostic must be a non-empty string")
        self._saved_settings = None
        self._expected_revision = None
        self._settings_ready = False
        self.status_label.setText(diagnostic)
        self._refresh_gating()


    def set_profile_candidates(self, candidates: Iterable[SettingsProfileCandidate]) -> None:
        values = tuple(candidates)
        if any(not isinstance(candidate, SettingsProfileCandidate) for candidate in values):
            raise TypeError("candidates must contain SettingsProfileCandidate values")
        current = self._selected_profile_name()
        self._candidates = values
        self._render_candidates(current)
        self._candidate_changed()


    def set_imported_profile(
        self,
        candidate: SettingsProfileCandidate,
        candidates: Iterable[SettingsProfileCandidate] | None = None,
    ) -> None:
        """Refresh a successful import and select it without treating it as saved."""
        if not isinstance(candidate, SettingsProfileCandidate):
            raise TypeError("candidate must be SettingsProfileCandidate")
        values = self._candidates if candidates is None else tuple(candidates)
        if any(not isinstance(value, SettingsProfileCandidate) for value in values):
            raise TypeError("candidates must contain SettingsProfileCandidate values")
        if candidate not in values:
            raise ValueError("candidates must include the imported candidate")
        self._candidates = values
        self._render_candidates(candidate.name)
        self._candidate_changed()

    def request_profile_import(self, selection: ImportSelection) -> None:
        """Forward the shared, immutable import selection after controller file picking."""
        if not isinstance(selection, ImportSelection) or selection.kind is not ImportKind.PROFILE:
            raise TypeError("selection must be a profile ImportSelection")
        if self._can_edit():
            self.profile_import_requested.emit(selection)

    def set_write_enabled(self, enabled: bool, reason: str | None = None) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise ValueError("reason must be a non-empty string or None")
        if enabled and self._write_revoked:
            raise RuntimeError("write authority cannot be re-enabled after revocation")
        self._write_enabled = enabled
        if not enabled:
            self._write_revoked = True
            self.status_label.setText(reason or "실행 폴더에 쓸 수 없어 설정을 저장할 수 없습니다.")
        self._refresh_gating()

    def set_busy(self, busy: bool) -> None:
        if type(busy) is not bool:
            raise TypeError("busy must be bool")
        self._busy = busy
        if busy:
            self.status_label.setText("설정을 저장하고 있습니다.")
        self._refresh_gating()

    def set_saved(self, result: SettingsSaveResult) -> None:
        if not isinstance(result, SettingsSaveResult) or not result.committed:
            raise TypeError("result must be a committed SettingsSaveResult")
        candidate = self._current_settings()
        if candidate is None:
            raise RuntimeError("cannot mark an invalid candidate as saved")
        self._saved_settings = candidate
        self._configured_default = candidate.default_profile
        self._expected_revision = result.revision
        self._busy = False
        self.status_label.setText("설정이 저장되었습니다.")
        self._refresh_gating()

    def set_save_error(self, error: object | None = None, message: str | None = None) -> None:
        if message is not None and (not isinstance(message, str) or not message):
            raise ValueError("message must be a non-empty string or None")
        self._busy = False
        self.status_label.setText(
            message or (str(error) if error is not None else "설정을 저장하지 못했습니다.")
        )
        self._refresh_gating()

    def _render_candidates(self, selected_name: str | None) -> None:
        diagnostic_name = selected_name or self._configured_default
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("기본 OMR 프로필을 선택하세요", None)
        if diagnostic_name and all(
            candidate.name != diagnostic_name for candidate in self._candidates
        ):
            self.profile_combo.addItem(f"{diagnostic_name} (기본 프로필을 찾을 수 없음)", None)
            model = self.profile_combo.model()
            if isinstance(model, QStandardItemModel):
                item = model.item(self.profile_combo.count() - 1)
                if item is not None:
                    item.setEnabled(False)
        for candidate in self._candidates:
            label = (
                candidate.name
                if candidate.valid
                else f"{candidate.name} (사용 불가: {candidate.diagnostic})"
            )
            self.profile_combo.addItem(label, candidate.name)
            if not candidate.valid:
                model = self.profile_combo.model()
                if isinstance(model, QStandardItemModel):
                    item = model.item(self.profile_combo.count() - 1)
                    if item is not None:
                        item.setEnabled(False)
        selected_index = self.profile_combo.findData(selected_name)
        self.profile_combo.setCurrentIndex(selected_index if selected_index > 0 else 0)
        self.profile_combo.blockSignals(False)
        self._update_profile_diagnostic(
            selected_name if selected_index > 0 else self._configured_default
        )

    def _selected_profile_name(self) -> str | None:
        value = self.profile_combo.currentData()
        return value if isinstance(value, str) else None

    def _selected_candidate(self) -> SettingsProfileCandidate | None:
        selected = self._selected_profile_name()
        return next((item for item in self._candidates if item.name == selected), None)

    def _update_profile_diagnostic(self, configured_name: str | None = None) -> None:
        candidate = self._selected_candidate()
        if candidate is not None and candidate.valid:
            self.profile_diagnostic_label.setText(f"선택됨: {candidate.name}")
        elif candidate is not None:
            self.profile_diagnostic_label.setText(
                candidate.diagnostic or "사용할 수 없는 프로필입니다."
            )
        elif configured_name:
            self.profile_diagnostic_label.setText(
                f"기본 프로필을 찾을 수 없습니다: {configured_name}"
            )
        else:
            self.profile_diagnostic_label.setText("검증된 프로필을 선택하세요.")

    def _profile_changed(self, *_: object) -> None:
        self._update_profile_diagnostic()
        self._candidate_changed()

    def _sensitivity_changed(self, value: int) -> None:
        self.sensitivity_label.setText(f"인식 수준 {value} / 10")
        self._candidate_changed()

    def _candidate_changed(self, *_: object) -> None:
        if self._saved_settings is not None and self._current_settings() != self._saved_settings:
            self.status_label.setText("저장되지 않은 변경 사항이 있습니다.")
        self._refresh_gating()

    def _current_settings(self) -> Settings | None:
        candidate = self._selected_candidate()
        if candidate is not None:
            if not candidate.valid:
                return None
            default_profile = candidate.name
        elif self._configured_default:
            return None
        else:
            default_profile = ""
        return Settings(
            default_profile=default_profile,
            default_sensitivity=self.sensitivity_slider.value(),
            use_multiprocessing=self.multiprocessing_checkbox.isChecked(),
        )

    def _can_edit(self) -> bool:
        return self._settings_ready and self._write_enabled and not self._busy

    def _can_save(self) -> bool:
        return (
            self._can_edit()
            and self._expected_revision is not None
            and self._current_settings() is not None
        )

    def _refresh_gating(self) -> None:
        editable = self._can_edit()
        self.profile_combo.setEnabled(editable)
        self.profile_import_button.setEnabled(editable)
        self.sensitivity_slider.setEnabled(editable)
        self.multiprocessing_checkbox.setEnabled(editable)
        self.save_button.setEnabled(self._can_save())

    def _request_profile_browse(self) -> None:
        if self._can_edit():
            self.profile_browse_requested.emit()

    def _emit_save_request(self) -> None:
        settings = self._current_settings()
        if not self._can_save() or settings is None or self._expected_revision is None:
            return
        self.save_requested.emit(SettingsPageRequest(settings, self._expected_revision))


__all__ = ["SettingsPage", "SettingsPageRequest", "SettingsProfileCandidate"]
