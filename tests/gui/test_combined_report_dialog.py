from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from omr_grader.application.dto import Settings
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.enums import ExamTerm, SessionState
from omr_grader.domain.errors import Ok
from omr_grader.domain.models import DashboardIndexEntry
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.combined_report_dialog import CombinedReportChoice, CombinedReportDialog
from omr_grader.ui.dashboard_model import COLUMN_SELECTION, DashboardSelection
from omr_grader.ui.dashboard_page import DashboardPage, DashboardRequest
from omr_grader.ui.main_window import MainWindow


def _entry(session_id: str, name: str) -> DashboardIndexEntry:
    return DashboardIndexEntry(
        session_id,
        1,
        f"generation-{session_id}",
        "a" * 64,
        f"261007_120000_{name}",
        name,
        None,
        ExamTerm.UNSPECIFIED,
        SessionState.GRADED,
        "2026-10-07T03:00:00.000000Z",
        12,
        "70",
        "90",
        "50",
        0,
    )


P1, P2 = _entry("session-1", "졸업고사p1"), _entry("session-2", "졸업고사p2")


def test_parts_start_in_name_order_and_can_be_reordered(qtbot, monkeypatch) -> None:
    dialog = CombinedReportDialog((P2, P1))
    qtbot.addWidget(dialog)
    assert [entry.session_id for entry in dialog.ordered_entries] == ["session-1", "session-2"]
    assert dialog.part_list.item(0).text().startswith("파트1")

    dialog.part_list.setCurrentRow(1)
    dialog.up_button.click()

    assert [entry.session_id for entry in dialog.ordered_entries] == ["session-2", "session-1"]
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("D:/out/합산", "")
    )
    dialog.set_subject_path("D:/in/과목구성.xlsx")
    dialog.create_button.click()

    assert dialog.choice == CombinedReportChoice(
        ("session-2", "session-1"), "D:/in/과목구성.xlsx", "D:/out/합산.xlsx"
    )
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_a_single_exam_cannot_be_combined(qtbot) -> None:
    dialog = CombinedReportDialog((P1,))
    qtbot.addWidget(dialog)
    assert not dialog.create_button.isEnabled()


def test_the_dashboard_offers_combining_once_two_exams_are_checked(qtbot) -> None:
    page = DashboardPage()
    qtbot.addWidget(page)
    page.set_entries((P1, P2))
    requests: list[object] = []
    page.request_emitted.connect(requests.append)
    assert not page.combine_button.isEnabled()

    for row in range(2):
        page.model.setData(
            page.model.index(row, COLUMN_SELECTION),
            Qt.CheckState.Checked,
            Qt.ItemDataRole.CheckStateRole,
        )
    page.combine_button.click()

    assert len(requests) == 1
    assert requests[0].action == "combine"
    assert set(requests[0].selection.session_ids) == {"session-1", "session-2"}


@dataclass(frozen=True, slots=True)
class _Summary:
    path: str
    students: int
    complete: int
    needs_review: int
    unreadable: int


class _AcceptingDialog:
    """Stands in for the dialog: answers as if the user chose and saved."""

    def __init__(self, entries, parent) -> None:
        self.entries = entries
        self.choice = CombinedReportChoice(
            tuple(entry.session_id for entry in entries), None, "D:/out/합산.xlsx"
        )
        from PySide6.QtCore import QObject, Signal

        class _Signals(QObject):
            sample_requested = Signal(str)

        self._signals = _Signals()
        self.sample_requested = self._signals.sample_requested

    def exec(self) -> QDialog.DialogCode:
        return QDialog.DialogCode.Accepted

    def deleteLater(self) -> None:  # noqa: N802
        pass


def test_the_controller_builds_the_report_and_says_where_it_went(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    calls: list[tuple[object, ...]] = []

    def export(session_ids, subject_path, destination):
        calls.append((session_ids, subject_path, destination))
        return Ok(_Summary(destination, 12, 11, 1, 0))

    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            settings_load=lambda: Ok(SettingsState(Settings("", 3, False), 1)),
            combined_export=export,
        ),
        write_enabled=True,
    )
    controller._combined_dialog_factory = _AcceptingDialog
    window.dashboard_page.set_entries((P1, P2))

    controller._handle_dashboard_request(
        DashboardRequest("combine", DashboardSelection(("session-1", "session-2"), (1, 1)))
    )

    qtbot.waitUntil(lambda: window.findChild(QMessageBox, "actionResultDialog") is not None)
    assert calls == [(("session-1", "session-2"), None, "D:/out/합산.xlsx")]
    text = window.findChild(QMessageBox, "actionResultDialog").text()
    assert "학생 12명 · 확인 필요 1명" in text
    controller.close()

