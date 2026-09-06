from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import MappingProxyType

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer

from omr_grader.application.dto import ImportResponseCommand, ResponseBookRequest, Settings
from omr_grader.application.grading_presenter import GradingPageRequest
from omr_grader.application.response_import_use_case import ResponseImportUseCase
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.enums import CreationKind, ExamTerm, OperationKind, SessionState
from omr_grader.domain.errors import ErrorInfo, Ok
from omr_grader.domain.models import (
    DashboardIndexEntry,
    IdentityRecord,
    ManifestSummary,
    SessionManifest,
    SessionRecord,
)
from omr_grader.infrastructure.dashboard_repository import DashboardListing, DashboardRepository
from omr_grader.infrastructure.grading_runtime import ResponseImportCommitCoordinator
from omr_grader.infrastructure.session_store import SessionStore
from omr_grader.ui.app_controller import AppController, ServicePorts, _dashboard_worker_value
from omr_grader.ui.dashboard_page import DashboardGlobalRequest
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.worker_bridge import WorkerBridge, WorkerError, _value_only
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME


def _entry() -> DashboardIndexEntry:
    return DashboardIndexEntry(
        "warning-session",
        3,
        "generation-3",
        "a" * 64,
        "exam",
        "경고와 함께 표시되는 시험",
        2026,
        ExamTerm.SECOND,
        SessionState.GRADED,
        "2026-09-06T01:00:00.000000Z",
        1,
        "1",
        "1",
        "1",
        0,
    )


@pytest.mark.parametrize("location", ("listing", "outer-result", "both"))
def test_dashboard_startup_and_refresh_keep_entries_and_warning_context(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    location: str,
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    shown: list[str] = []
    shown_threads: list[QThread] = []
    show_diagnostic = window.show_diagnostic

    def show(message: str) -> None:
        shown.append(message)
        shown_threads.append(QThread.currentThread())
        show_diagnostic(message)

    monkeypatch.setattr(window, "show_diagnostic", show)
    entry = _entry()
    warning = ErrorInfo(
        "DASHBOARD_INDEX_STALE",
        "warning.dashboard_index_stale",
        "dashboard_index.json",
        {"reason": "실제 재구축 경고", "revision": 3, "retry": True, "extra": None},
        True,
        "PermissionError",
    )
    load_threads: list[QThread] = []
    loaded: list[DashboardListing] = []

    def load():
        load_threads.append(QThread.currentThread())
        listing = DashboardListing((entry,), (warning,) if location != "outer-result" else ())
        loaded.append(listing)
        return Ok(listing, (warning,) if location != "listing" else ())

    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(
            dashboard_load=load,
            settings_load=lambda: Ok(SettingsState(Settings("", 5, False), 1)),
        ),
        write_enabled=True,
    )
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert window.dashboard_page.model.entry_at(0) == entry
    assert shown == ["실제 재구축 경고"]
    assert window.status_label.text() == shown[-1]
    assert all(thread is QCoreApplication.instance().thread() for thread in shown_threads)
    assert all(thread is not QCoreApplication.instance().thread() for thread in load_threads)
    assert "UI_NON_VALUE_PAYLOAD" not in caplog.text
    # The service diagnostic remains a normal ErrorInfo; accepting mutable
    # service objects at the signal boundary is not part of this fix.
    assert not _value_only(warning)
    assert warning.context["revision"] == 3

    entry = replace(entry, exam_name="새로 고침된 시험", revision=4)
    warning = ErrorInfo(
        "DASHBOARD_INDEX_STALE",
        "warning.dashboard_index_stale",
        context={"reason": "새로 고침 경고"},
    )
    controller._reload_dashboard()
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert window.dashboard_page.model.rowCount() == 1
    assert window.dashboard_page.model.entry_at(0) == entry
    assert shown == ["실제 재구축 경고", "새로 고침 경고"]
    assert len(loaded) == 2
    controller.close()


def test_warning_context_is_copied_before_queued_delivery(qtbot) -> None:
    context = {"reason": "original", "count": 2, "retry": True, "optional": None}
    diagnostic = ErrorInfo("QA_WARNING", "warning.qa_warning", context=context)
    bridge = WorkerBridge()
    received = []
    errors = []
    bridge.succeeded.connect(received.append)
    bridge.failed.connect(errors.append)

    def load(_cancel, _progress):
        result = _dashboard_worker_value(Ok(DashboardListing((_entry(),), (diagnostic,))))
        context["reason"] = "changed after conversion"
        return result

    bridge.start(load)
    qtbot.waitUntil(lambda: not bridge.active)
    assert not errors
    assert len(received) == 1 and _value_only(received[0])
    assert dict(received[0].warnings[0].context) == {
        "reason": "original",
        "count": 2,
        "retry": True,
        "optional": None,
    }
    assert received[0].warnings[0].context is not context
    assert not _value_only(diagnostic)


@pytest.mark.parametrize("kind", ("dict", "proxy", "list", "path", "qobject", "handle"))
def test_dashboard_warning_adapter_rejects_live_or_mutable_context(qtbot, kind: str) -> None:
    unsafe = {
        "dict": {},
        "proxy": MappingProxyType({}),
        "list": [],
        "path": Path("live-file"),
        "qobject": QObject(),
        "handle": BytesIO(b"live"),
    }[kind]
    diagnostic = ErrorInfo("QA_WARNING", "warning.qa_warning")
    # ErrorInfo validates on construction but its dictionary can later mutate.
    diagnostic.context["unsafe"] = unsafe
    assert not _value_only(unsafe)
    bridge = WorkerBridge()
    successes = []
    errors = []
    bridge.succeeded.connect(successes.append)
    bridge.failed.connect(errors.append)
    bridge.start(
        lambda _cancel, _progress: _dashboard_worker_value(
            Ok(DashboardListing((_entry(),), (diagnostic,)))
        )
    )
    qtbot.waitUntil(lambda: not bridge.active)
    assert not successes
    assert len(errors) == 1 and isinstance(errors[0], WorkerError)
    assert _value_only(errors[0])


def add_unprojectable_session(store: SessionStore) -> None:
    """Create a store-valid synthetic sibling that the dashboard quarantines."""
    stamp = "2026-09-06T00:00:00.000000Z"
    session_id = "warning-fixture-sibling"
    manifest = SessionManifest(
        1,
        session_id,
        1,
        "warning-fixture-generation",
        None,
        None,
        None,
        "warning-fixture-create",
        OperationKind.CREATE,
        "2.1.1",
        stamp,
        SessionState.CREATED,
        (),
        None,
        "b" * 64,
        "b" * 64,
        None,
        None,
        (),
        ManifestSummary(0, 0, 0, None),
    )
    created = store.create_initial_generation(
        identity=IdentityRecord(1, session_id, stamp, CreationKind.SCAN),
        manifest=manifest,
        session=SessionRecord(
            1,
            session_id,
            1,
            SessionState.CREATED,
            "Synthetic missing dashboard inputs",
            2026,
            ExamTerm.SECOND,
            stamp,
            None,
            stamp,
        ),
        display_name="synthetic-dashboard-warning",
    )
    assert isinstance(created, Ok), created


def test_real_repository_quarantine_warning_keeps_healthy_dashboard_row(
    tmp_path: Path,
    qtbot,
    caplog,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(RESPONSE_HEADERS)
    sheet.append([1, "synthetic.png", "00000001", "합성 학생", "1", *("" for _ in range(99)), ""])
    source = tmp_path / "responses.xlsx"
    workbook.save(source)
    workbook.close()
    store = SessionStore(tmp_path / "Data")
    importer = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
    validation = importer.validate_response_book(
        ResponseBookRequest(
            str(source),
            RESPONSE_SHEET_NAME,
            "Healthy warning fixture",
            2026,
            ExamTerm.SECOND,
        )
    )
    assert isinstance(validation, Ok)
    imported = importer.import_response_book(
        ImportResponseCommand(
            validation.value.validation_token,
            "healthy-warning-fixture",
            "import-fixture",
            0,
        )
    )
    assert isinstance(imported, Ok)
    add_unprojectable_session(store)
    repository = DashboardRepository(
        store.root / "dashboard_index.json",
        store.discover_active_committed_leases,
    )
    listings = []

    def load():
        result = repository.list_active()
        assert isinstance(result, Ok), result
        listings.append(result.value)
        return result

    window = MainWindow()
    qtbot.addWidget(window)
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(dashboard_load=load),
        write_enabled=True,
    )
    window.show()
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    for phase in range(2):
        if phase:
            controller._reload_dashboard()
            qtbot.waitUntil(lambda: controller._active_bridge is None)
        assert window.dashboard_page.model.rowCount() == 1
        assert window.dashboard_page.model.entry_at(0).session_id == "healthy-warning-fixture"
        assert listings[-1].warnings[0].code == "DASHBOARD_SESSION_QUARANTINED"
        assert window.status_label.text() == listings[-1].warnings[0].context["reason"]
        assert "UI_NON_VALUE_PAYLOAD" not in caplog.text
    assert len(listings) == 2
    controller.close()


def test_results_navigation_accepts_warning_transport_and_displays_entries(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    warning = ErrorInfo(
        "DASHBOARD_INDEX_STALE",
        "warning.dashboard_index_stale",
        context={"reason": "결과 보기 경고"},
    )
    listing = DashboardListing((_entry(),), (warning,))
    navigated = []

    def navigate(request):
        navigated.append(request)
        window.navigate_to(MainWindow.EXAM_PAGE)

    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(dashboard_load=lambda: Ok(listing), result_navigation=navigate),
        write_enabled=True,
    )
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    request = GradingPageRequest(
        "warning-session",
        3,
        "responses.xlsx",
        "key.xlsx",
        "정답표",
        "result-nav",
        "result",
        False,
    )
    controller._navigate_results(request)
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert navigated == [request]
    assert window.pages.currentIndex() == MainWindow.EXAM_PAGE
    assert window.dashboard_page.model.entry_at(0) == listing.entries[0]
    assert window.status_label.text() == "결과 보기 경고"
    controller.close()


def test_trash_load_keeps_rows_and_presents_warning(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    warning = ErrorInfo("TRASH_WARNING", "warning.trash_warning", context={"reason": "휴지통 경고"})
    listing = DashboardListing((_entry(),), (warning,))
    dialogs = []
    create_dialog = window.dashboard_page.create_trash_dialog

    def create(entries):
        dialog = create_dialog(entries)
        dialogs.append(dialog)
        QTimer.singleShot(0, dialog.accept)
        return dialog

    monkeypatch.setattr(window.dashboard_page, "create_trash_dialog", create)
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(dashboard_trash_load=lambda: Ok(listing)),
        write_enabled=True,
    )
    controller._handle_dashboard_request(DashboardGlobalRequest("trash"))
    qtbot.waitUntil(lambda: controller._active_bridge is None)
    assert len(dialogs) == 1
    assert dialogs[0].list_widget.count() == 1
    assert _entry().exam_name in dialogs[0].list_widget.item(0).text()
    assert window.status_label.text() == "휴지통 경고"
    controller.close()
