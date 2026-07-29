from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from omr_grader.application.dto import Settings, SettingsSaveResult
from omr_grader.ui.settings_page import (
    SettingsPage,
    SettingsPageRequest,
    SettingsProfileCandidate,
)

VALID = SettingsProfileCandidate("기본.omrtemplate", True)
OTHER = SettingsProfileCandidate("다른.omrtemplate", True)
INVALID = SettingsProfileCandidate("손상.omrtemplate", False, "프로필 형식이 올바르지 않습니다.")


def _ready_page(qtbot) -> SettingsPage:
    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_profile_candidates((VALID, INVALID, OTHER))
    page.set_settings(Settings("기본.omrtemplate", 3, True), 7)
    page.show()
    return page


def test_save_emits_immutable_settings_candidate_and_expected_revision(qtbot) -> None:
    page = _ready_page(qtbot)
    page.profile_combo.setCurrentIndex(page.profile_combo.findData("다른.omrtemplate"))
    page.sensitivity_slider.setValue(9)
    page.multiprocessing_checkbox.setChecked(False)

    with qtbot.waitSignal(page.save_requested) as signal:
        QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    request = signal.args[0]
    assert isinstance(request, SettingsPageRequest)
    assert request.expected_revision == 7
    assert request.settings == Settings("다른.omrtemplate", 9, False)
    with pytest.raises((AttributeError, TypeError)):
        request.expected_revision = 8


def test_invalid_and_missing_default_profiles_are_visible_without_replacement(qtbot) -> None:
    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_profile_candidates((VALID, INVALID))
    page.set_settings(Settings("없음.omrtemplate", 3, True), 1)

    invalid_index = page.profile_combo.findData("손상.omrtemplate")
    assert not page.profile_combo.model().item(invalid_index).isEnabled()
    assert "사용 불가" in page.profile_combo.itemText(invalid_index)
    assert "없음.omrtemplate" in page.profile_diagnostic_label.text()
    assert page.profile_combo.currentIndex() == 0
    assert not page.save_button.isEnabled()


def test_unsaved_and_saved_status_tracks_current_candidate(qtbot) -> None:
    page = _ready_page(qtbot)
    page.sensitivity_slider.setValue(4)
    assert "저장되지 않은" in page.status_label.text()
    page.set_saved(SettingsSaveResult(True, 8, "settings-save"))
    assert page.status_label.text() == "설정이 저장되었습니다."
    page.sensitivity_slider.setValue(5)
    assert "저장되지 않은" in page.status_label.text()


def test_path_is_read_only_and_write_or_busy_blocks_mutations(qtbot) -> None:
    page = _ready_page(qtbot)
    assert page.data_path_edit.isReadOnly()
    page.set_write_enabled(False, "읽기 전용 실행 폴더")
    assert not page.save_button.isEnabled()
    assert not page.profile_combo.isEnabled()
    assert not page.sensitivity_slider.isEnabled()
    with pytest.raises(RuntimeError, match="cannot be re-enabled"):
        page.set_write_enabled(True)

    busy_page = _ready_page(qtbot)
    busy_page.set_busy(True)
    assert not busy_page.save_button.isEnabled()
    assert not busy_page.profile_import_button.isEnabled()
    busy_page.set_busy(False)
    assert busy_page.save_button.isEnabled()


def test_keyboard_order_accessibility_and_range_validation(qtbot) -> None:
    page = _ready_page(qtbot)
    assert page.accessibleName() == "환경 설정"
    assert page.profile_combo.accessibleName() == "기본 OMR 프로필"
    assert page.sensitivity_slider.minimum() == 1
    assert page.sensitivity_slider.maximum() == 10
    page.profile_combo.setFocus()
    QTest.keyClick(page.profile_combo, Qt.Key.Key_Tab)
    qtbot.waitUntil(page.profile_import_button.hasFocus)
    QTest.keyClick(page.profile_import_button, Qt.Key.Key_Tab)
    qtbot.waitUntil(page.sensitivity_slider.hasFocus)
    with pytest.raises(ValueError):
        page.set_settings(Settings("기본.omrtemplate", 3, True), 0)
    with pytest.raises(TypeError):
        page.set_write_enabled(1)
