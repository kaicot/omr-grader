"""전체 백업 · 전체 복구 · 백업 알림 · 공간 확인 on the screens (stubbed services)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

import omr_grader.ui.app_controller as app_controller
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.infrastructure.backup_prefs import BackupPreferences
from omr_grader.infrastructure.dashboard_repository import DashboardListing
from omr_grader.infrastructure.full_backup import (
    FullBackupProgress,
    FullBackupSummary,
    FullRestoreSummary,
)
from omr_grader.ui.app_controller import AppController
from omr_grader.ui.dashboard_page import DashboardGlobalRequest, DashboardPage
from tests.gui.test_app_controller import (
    FakeScanService,
    _committed_scan_result,
    _dashboard_entry,
    _ready_ports,
    _request,
    _window,
)


class _Prefs:
    def __init__(self, prefs: BackupPreferences) -> None:
        self.value = prefs
        self.saved: list[BackupPreferences] = []

    def load(self) -> BackupPreferences:
        return self.value

    def save(self, prefs: BackupPreferences) -> None:
        self.value = prefs
        self.saved.append(prefs)


def _capture(monkeypatch: pytest.MonkeyPatch, window, name: str) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(window, name, lambda title, text: shown.append((title, text)))
    return shown


def test_the_dashboard_offers_a_full_backup_and_a_folder_restore(qtbot, monkeypatch) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.set_write_enabled(True)
    emitted: list[object] = []
    page.request_emitted.connect(emitted.append)

    page.backup_all_button.click()
    monkeypatch.setattr(page, "_ask_restore_kind", lambda: "folder")
    page.restore_button.click()
    monkeypatch.setattr(page, "_ask_restore_kind", lambda: None)
    page.restore_button.click()

    assert [request.action for request in emitted] == ["backup_all", "restore_all"]
    assert all(isinstance(request, DashboardGlobalRequest) for request in emitted)
    page.set_space_warning("남은 공간이 500 MB뿐입니다.")
    assert page.space_label.isVisibleTo(page)
    page.set_space_warning(None)
    assert not page.space_label.isVisibleTo(page)
    # A full backup only reads the exams: a read-only install may still make one.
    page.set_write_enabled(False)
    assert page.backup_all_button.isEnabled() and not page.restore_button.isEnabled()


def test_a_full_backup_records_its_time_and_hides_the_reminder(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    window, scan, grading = _window(qtbot)
    prefs = _Prefs(BackupPreferences())
    calls: list[str] = []

    def backup(folder: str, report) -> Ok:
        calls.append(folder)
        report(FullBackupProgress(1, 2))
        return Ok(FullBackupSummary(str(tmp_path / "OMR-Grader-backup-x"), 2, 2, (), 3, (("s-a", 4), ("s-b", 2))))

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    monkeypatch.setattr(app_controller, "same_drive", lambda left, right: False)
    shown = _capture(monkeypatch, window, "show_info_dialog")
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            full_backup=backup,
            backup_prefs_load=prefs.load,
            backup_prefs_save=prefs.save,
            data_dir=tmp_path,
        ),
        write_enabled=True,
    )
    window.show_backup_reminder("전체 백업을 아직 한 번도 하지 않았습니다.")

    window.backup_now_button.click()

    qtbot.waitUntil(lambda: bool(shown), timeout=5000)
    assert calls == [str(tmp_path)]
    assert shown[0][0] == "전체 백업"
    assert "시험 2개 중 2개를 백업했습니다." in shown[0][1]
    assert prefs.value.last_full_backup_at is not None
    assert dict(prefs.value.revisions) == {"s-a": 4, "s-b": 2}
    assert not window.backup_banner.isVisibleTo(window)
    controller.close()


def test_the_same_drive_is_backed_up_only_after_a_yes(qtbot, monkeypatch, tmp_path: Path) -> None:
    window, scan, grading = _window(qtbot)
    calls: list[str] = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    monkeypatch.setattr(app_controller, "same_drive", lambda left, right: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(full_backup=lambda folder, report: calls.append(folder), data_dir=tmp_path),
        write_enabled=True,
    )

    window.backup_now_button.click()

    assert calls == []
    controller.close()


def test_a_full_restore_reports_what_came_back(qtbot, monkeypatch, tmp_path: Path) -> None:
    window, scan, grading = _window(qtbot)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    shown = _capture(monkeypatch, window, "show_info_dialog")
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(
            full_restore=lambda folder, report: Ok(
                FullRestoreSummary(3, 1, ("시험C (백업 파일이 손상되었습니다)",), 2, None)
            )
        ),
        write_enabled=True,
    )

    window.dashboard_page.request_emitted.emit(DashboardGlobalRequest("restore_all"))

    qtbot.waitUntil(lambda: bool(shown), timeout=5000)
    text = shown[0][1]
    assert "시험 3개를 복구했습니다." in text
    assert "이미 있는 시험 1개(휴지통에 있는 것 포함)는 건너뛰었습니다." in text
    assert "환경 설정은 지금 쓰는 설정을 그대로 두었습니다." in text
    assert "시험C" in text
    controller.close()


def test_a_failed_full_backup_says_so_in_a_dialog(qtbot, monkeypatch, tmp_path: Path) -> None:
    window, scan, grading = _window(qtbot)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    monkeypatch.setattr(app_controller, "same_drive", lambda left, right: False)
    errors = _capture(monkeypatch, window, "show_error_dialog")
    failure = Err(
        (
            ErrorInfo(
                "FULL_BACKUP_SPACE",
                "error.full_backup_space",
                context={"reason": "백업할 드라이브에 공간이 모자랍니다."},
            ),
        )
    )
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(full_backup=lambda folder, report: failure, data_dir=tmp_path),
        write_enabled=True,
    )

    window.backup_now_button.click()

    qtbot.waitUntil(lambda: bool(errors), timeout=5000)
    assert errors[0][0] == "전체 백업" and "공간이 모자랍니다" in errors[0][1]
    controller.close()


def test_the_reminder_appears_once_after_the_exam_list_loads(qtbot, monkeypatch) -> None:
    window, scan, grading = _window(qtbot)
    entry = _dashboard_entry()
    old = type(entry)(**{**{f: getattr(entry, f) for f in entry.__dataclass_fields__}, "display_folder": "260101_090000_시험A"})
    prefs = _Prefs(BackupPreferences())
    controller = AppController(
        window,
        scan,
        grading,
        _ready_ports(backup_prefs_load=prefs.load, backup_prefs_save=prefs.save),
        write_enabled=True,
    )

    controller._finish_dashboard_load(DashboardListing((old,), ()))
    assert window.backup_banner.isVisibleTo(window)
    assert "한 번도" in window.backup_label.text()
    window.backup_later_button.click()
    controller._finish_dashboard_load(DashboardListing((old,), ()))
    assert not window.backup_banner.isVisibleTo(window)

    window.settings_page.backup_reminder_box.setChecked(False)
    assert prefs.value.enabled is False
    controller.close()


def test_a_scan_that_will_not_fit_on_the_drive_does_not_start(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    window, scan, grading = _window(qtbot)
    service = FakeScanService(_committed_scan_result)
    errors = _capture(monkeypatch, window, "show_error_dialog")
    monkeypatch.setattr(
        app_controller, "shortfall", lambda path, needed: "필요한 공간 약 1.2 GB / 남은 공간 800 MB"
    )
    controller = AppController(
        window, scan, grading, _ready_ports(scan=service, data_dir=tmp_path), write_enabled=True
    )

    controller.start_scan(_request())

    assert service.calls == []
    assert errors and errors[0][0] == "공간 부족" and "1.2 GB" in errors[0][1]
    controller.close()


def test_a_nearly_full_drive_is_shown_above_the_exam_list(qtbot, monkeypatch, tmp_path: Path) -> None:
    window, scan, grading = _window(qtbot)
    monkeypatch.setattr(app_controller, "free_bytes", lambda path: 300 * 1024 * 1024)
    controller = AppController(
        window, scan, grading, _ready_ports(data_dir=tmp_path), write_enabled=True
    )

    controller._finish_dashboard_load(DashboardListing((), ()))

    assert "300 MB" in window.dashboard_page.space_label.text()
    controller.close()
