from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from omr_grader.application.dto import Settings, SettingsSaveResult
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.settings_page import SettingsPage, SettingsProfileCandidate

BASE = SettingsProfileCandidate("기본.omrtemplate", True)
IMPORTED = SettingsProfileCandidate("가져온.omrtemplate", True)


def test_profile_browse_and_shared_import_intent_are_controller_only(qtbot) -> None:
    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_profile_candidates((BASE,))
    page.set_settings(Settings("기본.omrtemplate", 3, True), 2)
    page.show()

    with qtbot.waitSignal(page.profile_browse_requested):
        QTest.mouseClick(page.profile_import_button, Qt.MouseButton.LeftButton)
    selection = ImportSelection(ImportKind.PROFILE, ("C:/외부/가져온.omrtemplate",))
    with qtbot.waitSignal(page.profile_import_requested) as imported:
        page.request_profile_import(selection)
    assert imported.args[0] is selection


def test_import_refresh_selects_only_unsaved_candidate(qtbot) -> None:
    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_profile_candidates((BASE,))
    page.set_settings(Settings("기본.omrtemplate", 3, True), 2)

    page.set_imported_profile(IMPORTED, (BASE, IMPORTED))

    assert page.profile_combo.currentData() == "가져온.omrtemplate"
    assert "저장되지 않은" in page.status_label.text()
    assert page.save_button.isEnabled()
    page.set_saved(SettingsSaveResult(True, 3, "profile-import-save"))
    assert page.profile_combo.currentData() == "가져온.omrtemplate"
    assert page.status_label.text() == "설정이 저장되었습니다."


def test_import_intent_is_gated_while_read_only_or_busy(qtbot) -> None:
    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_profile_candidates((BASE,))
    page.set_settings(Settings("기본.omrtemplate", 3, True), 2)
    selection = ImportSelection(ImportKind.PROFILE, ("C:/외부/가져온.omrtemplate",))
    received = []
    page.profile_import_requested.connect(received.append)
    page.set_write_enabled(False)
    page.request_profile_import(selection)
    busy_page = SettingsPage()
    qtbot.addWidget(busy_page)
    busy_page.set_profile_candidates((BASE,))
    busy_page.set_settings(Settings("기본.omrtemplate", 3, True), 2)
    busy_page.profile_import_requested.connect(received.append)
    busy_page.set_busy(True)
    busy_page.request_profile_import(selection)
    assert received == []
