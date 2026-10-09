"""The trash dialog opened from the dashboard really reaches the trash service.

Until 4.1.2 the dialog opened while the loading worker was still winding down, so its buttons
started disabled and every restore, delete and empty request was silently dropped.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from omr_grader.application.dto import PermanentDeleteResult, Settings, TrashRestoreResult
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.enums import CleanupState, ExamTerm, IndexState, SessionState
from omr_grader.domain.errors import Ok
from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.infrastructure.dashboard_repository import DashboardListing
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.trash_dialog import TrashDialog


def _entry(session_id: str, name: str) -> DashboardIndexEntry:
    return DashboardIndexEntry(
        session_id,
        1,
        f"generation-{session_id}",
        "a" * 64,
        f"261008_120000_{name}",
        name,
        None,
        ExamTerm.UNSPECIFIED,
        SessionState.GRADED,
        "2026-10-08T03:00:00.000000Z",
        12,
        "70",
        "90",
        "50",
        0,
    )


ENTRIES = (_entry("session-a", "시험A"), _entry("session-b", "시험B"))


@pytest.mark.parametrize(
    ("button", "rows", "action", "session_ids", "status"),
    [
        ("restore_button", (1,), "trash_restore", ("session-b",), "휴지통에서 시험을 복원했습니다."),
        ("delete_button", (0, 1), "trash_delete", ("session-a", "session-b"), "시험을 영구 삭제했습니다."),
        ("empty_button", (), "trash_delete", ("session-a", "session-b"), "시험을 영구 삭제했습니다."),
    ],
)
def test_each_trash_button_reaches_the_service_and_reloads_the_dashboard(
    qtbot, monkeypatch, button, rows, action, session_ids, status
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    calls: list[tuple[str, tuple[str, ...]]] = []
    loads: list[int] = []

    def trash(request):
        calls.append((request.action, request.selection.session_ids))
        if request.action == "trash_restore":
            return Ok(TrashRestoreResult(True, "Data", IndexState.CURRENT, uuid4().hex))
        return Ok(
            PermanentDeleteResult(True, IndexState.CURRENT, CleanupState.COMPLETE, uuid4().hex)
        )

    def load():
        loads.append(1)
        return Ok(DashboardListing(()))

    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            settings_load=lambda: Ok(SettingsState(Settings("", 3, False), 1)),
            dashboard_load=load,
            dashboard_trash_load=lambda: Ok(DashboardListing(ENTRIES)),
            dashboard_trash=trash,
        ),
        write_enabled=True,
    )
    window.show()
    seen: dict[str, object] = {}

    def press() -> None:
        dialog = next(
            (
                widget
                for widget in QApplication.topLevelWidgets()
                if isinstance(widget, TrashDialog) and widget.isVisible()
            ),
            None,
        )
        if dialog is None:
            QTimer.singleShot(20, press)
            return
        seen["enabled_before"] = dialog.empty_button.isEnabled()
        dialog.select_rows(*rows)
        seen["enabled"] = getattr(dialog, button).isEnabled()
        getattr(dialog, button).click()

    # Let the start-up dashboard load finish first.
    qtbot.waitUntil(
        lambda: controller._active_bridge is None
        and window.dashboard_page.trash_button.isEnabled(),
        timeout=5000,
    )
    loads.clear()
    QTimer.singleShot(0, press)
    window.dashboard_page.trash_button.click()

    # Chosen exams reach the service one by one, so one failure cannot stop the others.
    qtbot.waitUntil(lambda: len(calls) == len(session_ids), timeout=5000)
    assert seen == {"enabled_before": True, "enabled": True}
    assert calls == [(action, (session_id,)) for session_id in session_ids]
    # The finished action reloads the exam list.
    qtbot.waitUntil(lambda: loads == [1] and controller._active_bridge is None, timeout=5000)
    # The status bar says what happened instead of staying on "처리 중".
    qtbot.waitUntil(lambda: window.status_label.text() == status, timeout=5000)
    controller.close()
