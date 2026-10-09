"""Update notice and older-install import, driven through the real controller."""

from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QMessageBox

from omr_grader import __version__
from omr_grader.application.dto import Settings
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.infrastructure.data_import import ImportProgress, ImportSummary
from omr_grader.infrastructure.update_check import ReleaseInfo, UpdatePreferences, parse_version
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.main_window import MainWindow

PAGE = "https://github.com/kaicot/omr-grader/releases/tag/v9.0.0"


def _newer() -> str:
    major, minor, patch = parse_version(__version__) or (0, 0, 0)
    return f"{major}.{minor}.{patch + 1}"


class _Prefs:
    def __init__(self, prefs: UpdatePreferences) -> None:
        self.value = prefs
        self.saved: list[UpdatePreferences] = []

    def load(self) -> UpdatePreferences:
        return self.value

    def save(self, prefs: UpdatePreferences) -> None:
        self.value = prefs
        self.saved.append(prefs)


def _controller(window, prefs, fetch=None, **extra) -> AppController:
    return AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            settings_load=lambda: Ok(SettingsState(Settings("", 5, False), 1)),
            update_fetch=fetch,
            update_prefs_load=prefs.load,
            update_prefs_save=prefs.save,
            **extra,
        ),
        write_enabled=True,
    )


def test_a_newer_release_shows_the_banner_and_skipping_remembers_it(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    prefs = _Prefs(UpdatePreferences())
    release = ReleaseInfo(_newer(), PAGE, None)
    controller = _controller(window, prefs, lambda: Ok(release))

    qtbot.waitUntil(window.update_banner.isVisible, timeout=6000)
    assert f"v{release.version}" in window.update_label.text()
    assert prefs.value.last_checked is not None

    window.update_skip_button.click()

    assert not window.update_banner.isVisible()
    assert prefs.value.skipped_version == release.version
    controller.close()


def test_a_skipped_or_current_release_stays_quiet(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    release = ReleaseInfo(_newer(), PAGE, None)
    prefs = _Prefs(UpdatePreferences(skipped_version=release.version))
    calls: list[int] = []

    def fetch():
        calls.append(1)
        return Ok(release)

    controller = _controller(window, prefs, fetch)

    qtbot.waitUntil(lambda: prefs.saved != [], timeout=6000)
    assert calls == [1]
    assert not window.update_banner.isVisible()
    controller.close()


def test_checking_by_hand_reports_offline_and_up_to_date(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    # Checked recently, so nothing runs at start-up.
    prefs = _Prefs(UpdatePreferences(True, "2999-01-01T00:00:00Z"))
    answers = [
        Err((ErrorInfo("UPDATE_CHECK_FAILED", "error.update_check_failed", None, context={"reason": "오프라인"}),)),
        Ok(ReleaseInfo(__version__, PAGE, None)),
    ]
    controller = _controller(window, prefs, lambda: answers.pop(0))
    status = window.settings_page.update_status_label

    window.settings_page.update_now_button.click()
    qtbot.waitUntil(lambda: "확인하지 못했습니다" in status.text(), timeout=6000)
    assert "오프라인" in status.text()

    window.settings_page.update_now_button.click()
    qtbot.waitUntil(lambda: "최신 버전입니다" in status.text(), timeout=6000)
    assert not window.update_banner.isVisible()
    controller.close()


def test_turning_checks_off_is_saved_and_no_check_runs_without_a_fetcher(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    prefs = _Prefs(UpdatePreferences())
    controller = _controller(window, prefs, lambda: Ok(ReleaseInfo(_newer(), PAGE, None)))

    window.settings_page.update_check_box.setChecked(False)

    assert prefs.value.enabled is False
    controller.close()

    offline = MainWindow()
    qtbot.addWidget(offline)
    plain = _controller(offline, _Prefs(UpdatePreferences()))
    assert not offline.settings_page.update_now_button.isEnabled()
    plain.close()


def test_an_empty_dashboard_offers_the_import_and_reports_what_came_over(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    asked: list[int] = []

    def question(*args, **kwargs):
        asked.append(1)
        return QMessageBox.StandardButton.Yes

    # 4.2.1 no longer asks on first start; the button on the empty dashboard replaces it.
    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "D:/old/OMR Grader")
    calls: list[str] = []
    loads: list[int] = []

    def importer(folder, report):
        calls.append(folder)
        report(ImportProgress(0, 2))
        return Ok(ImportSummary(2, 0, ("261001_090000_퀴즈",), 1, 1, None))

    def load():
        from omr_grader.infrastructure.dashboard_repository import DashboardListing

        loads.append(1)
        return Ok(DashboardListing(()))

    controller = _controller(
        window,
        _Prefs(UpdatePreferences(enabled=False)),
        data_import=importer,
        first_run=True,
        dashboard_load=load,
    )

    qtbot.waitUntil(lambda: controller._active_bridge is None, timeout=6000)
    page = window.dashboard_page
    assert page.empty_state.isVisibleTo(page)
    assert calls == []
    before = len(loads)
    page.import_previous_button.click()

    qtbot.waitUntil(lambda: calls == ["D:/old/OMR Grader"], timeout=6000)
    qtbot.waitUntil(
        lambda: "시험 2개를 가져왔습니다" in window.settings_page.update_status_label.text(),
        timeout=6000,
    )
    text = window.settings_page.update_status_label.text()
    assert "확인이 필요한 시험: 261001_090000_퀴즈" in text
    assert "예전 휴지통의 시험 1개는 휴지통으로" in text
    assert "예전 폴더는 그대로" in text
    # The exam list is read again after the import.
    qtbot.waitUntil(lambda: len(loads) > before, timeout=6000)
    assert asked == []
    # The busy text is gone once the import has ended.
    assert window.settings_page.status_label.text() != "이전 버전 자료를 가져오고 있습니다."
    controller.close()
