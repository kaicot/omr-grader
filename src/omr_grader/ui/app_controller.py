"""Typed presentation controller joining Qt pages to application service ports."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from threading import Event
from typing import Protocol
from uuid import uuid4

from PySide6.QtCore import QObject

from omr_grader.application.detail_presenter import (
    DetailLoadRequest,
    DetailLoadResult,
    DetailPageDisplay,
    DetailPageRequest,
    DetailPreviewResult,
    DetailSaveResult,
)
from omr_grader.application.dto import (
    AnswerKeyRequest,
    BackupExportResult,
    BackupRestoreResult,
    CancelOperationCommand,
    CollisionPolicy,
    CombinedReportResult,
    CommitGenerationResult,
    ImportResponseCommand,
    PermanentDeleteResult,
    ProfileImportRequest,
    ProfileImportResult,
    RegradeCommand,
    ScanCommand,
    ScanProgress,
    ScanSource,
    SessionCreateResult,
    Settings,
    SettingsSaveCommand,
    SettingsSaveResult,
    SoftDeleteResult,
    TrashRestoreResult,
)
from omr_grader.application.grading_presenter import (
    AnswerKeyValidationDisplay,
    ConnectedSessionDisplay,
    GradingPageRequest,
    GradingPresenter,
    GradingProgressDisplay,
)
from omr_grader.application.ports import (
    AnswerKeyUseCase,
    GradingUseCase,
    ResponseImportUseCase,
    ScanUseCase,
)
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.dashboard_repository import DashboardListing
from omr_grader.infrastructure.profile_store import ProfileCatalogItem
from omr_grader.resources.messages import MESSAGE_CATALOG, get_message
from omr_grader.ui.dashboard_model import DashboardSelection
from omr_grader.ui.dashboard_page import DashboardPage, DashboardRequest
from omr_grader.ui.detail_page import DetailPage
from omr_grader.ui.grading_page import GradingPage
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPage, ScanPageRequest, ValidatedProfileState
from omr_grader.ui.settings_page import (
    SettingsPage,
    SettingsPageRequest,
    SettingsProfileCandidate,
)
from omr_grader.ui.worker_bridge import CancelHook, Operation, WorkerBridge, WorkerError

ProfilePicker = Callable[[], ImportSelection | None]
SelectionPicker = Callable[[ImportKind], ImportSelection | None]
AnswerKeyPicker = Callable[[GradingPageRequest], tuple[str, str] | None]
ResponsePicker = Callable[[GradingPageRequest], tuple[str, str] | None]
ResponseSelectionImport = Callable[
    [GradingPageRequest, tuple[str, str]], Result[ConnectedSessionDisplay]
]
FreshResponsePicker = Callable[["FreshResponseIntent"], tuple[str, str] | None]
FreshResponseSelectionImport = Callable[
    ["FreshResponseIntent", tuple[str, str]], Result[ConnectedSessionDisplay]
]
IntentHandler = Callable[[GradingPageRequest], None]


class WriteCommand(Protocol):
    @property
    def operation_id(self) -> str: ...


ScanContextOperation = Callable[[ScanCommand, Event, Callable[[object], None]], object]
GradingContextOperation = Callable[[RegradeCommand, Event, Callable[[object], None]], object]


@dataclass(frozen=True, slots=True)
class FreshResponseIntent:
    """Value-only request to create a new generation-one response session."""

    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("operation_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ServicePorts:
    """Explicit controller dependencies; page widgets never perform adapter I/O."""

    scan: ScanUseCase | None = None
    response_import: ResponseImportUseCase | None = None
    grading: GradingUseCase | None = None
    answer_key: AnswerKeyUseCase | None = None
    scan_context: ScanContextOperation | None = None
    grading_context: GradingContextOperation | None = None
    cancel_operation: Callable[[CancelOperationCommand], Result[None]] | None = None
    profile_picker: ProfilePicker | None = None
    source_picker: SelectionPicker | None = None
    roster_picker: SelectionPicker | None = None
    answer_key_picker: AnswerKeyPicker | None = None
    other_response_picker: ResponsePicker | None = None
    import_response_selection: ResponseSelectionImport | None = None
    fresh_response_picker: FreshResponsePicker | None = None
    import_fresh_response_selection: FreshResponseSelectionImport | None = None
    sample_roster: Callable[[], None] | None = None
    sample_answer_key: IntentHandler | None = None
    result_navigation: IntentHandler | None = None
    profile_catalog: Callable[[], Result[tuple[ProfileCatalogItem, ...]]] | None = None
    profile_import: Callable[[ProfileImportRequest], Result[ProfileImportResult]] | None = None
    session_display: (
        Callable[[SessionCreateResult | CommitGenerationResult], Result[ConnectedSessionDisplay]]
        | None
    ) = None
    dashboard_load: Callable[[], Result[DashboardListing]] | None = None
    dashboard_detail: Callable[[DashboardSelection], Result[DetailPageDisplay]] | None = None
    dashboard_delete: Callable[[DashboardSelection], Result[SoftDeleteResult]] | None = None
    dashboard_backup: Callable[[DashboardSelection], Result[BackupExportResult]] | None = None
    dashboard_restore: Callable[[], Result[BackupRestoreResult]] | None = None
    dashboard_combined: Callable[[DashboardSelection], Result[CombinedReportResult]] | None = None
    dashboard_trash: (
        Callable[[DashboardRequest], Result[TrashRestoreResult | PermanentDeleteResult]] | None
    ) = None
    dashboard_trash_load: Callable[[], Result[DashboardListing]] | None = None
    detail_load: Callable[[DetailLoadRequest], Result[DetailLoadResult]] | None = None
    detail_preview: Callable[[DetailPageRequest], Result[DetailPreviewResult]] | None = None
    detail_save: Callable[[DetailPageRequest], Result[DetailSaveResult]] | None = None
    detail_close: Callable[[DetailPageRequest], Result[None]] | None = None
    settings_load: Callable[[], Result[SettingsState]] | None = None
    settings_save: Callable[[SettingsSaveCommand], Result[SettingsSaveResult]] | None = None


def _error_text(error: object) -> str:
    if isinstance(error, ErrorInfo):
        message_key = error.message_key
        context = error.context
    elif isinstance(error, WorkerError):
        message_key = error.message_key
        context = dict(error.context)
    else:
        return "작업을 완료하지 못했습니다. 입력과 실행 환경을 확인하세요."
    message = MESSAGE_CATALOG.get(message_key)
    if message is not None:
        return message
    reason = context.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return "작업을 완료하지 못했습니다. 입력과 실행 환경을 확인하세요."


class AppController(QObject):
    """Single owner of write authority and all worker-to-view transitions."""

    def __init__(
        self,
        main_window: MainWindow,
        scan_page: ScanPage,
        grading_page: GradingPage,
        services: ServicePorts,
        *,
        write_enabled: bool,
        diagnostic: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.main_window, self.scan_page, self.grading_page = main_window, scan_page, grading_page
        self.dashboard_page: DashboardPage = main_window.dashboard_page
        self.detail_page: DetailPage = main_window.detail_page
        self.settings_page: SettingsPage = main_window.settings_page
        self.services = services
        self._write_capability = write_enabled
        self.write_enabled = write_enabled
        self._active_page: ScanPage | GradingPage | DashboardPage | SettingsPage | None = None
        self._active_operation_id: str | None = None
        self._active_bridge: WorkerBridge | None = None
        self._active_cancellable = False
        self._retired_bridges: deque[WorkerBridge] = deque()
        self._closing = False
        self._close_finished_handled = False
        self._active_kind = ""
        self._validation_request: AnswerKeyRequest | None = None
        self._active_session_identity: tuple[str, int] | None = None
        self._settings_snapshot: Settings | None = None
        self._settings_revision: int | None = None
        self._pending_detail_exit: DetailPageRequest | None = None
        self._desktop_success: Callable[[object], None] | None = None
        self._desktop_busy: Callable[[bool], None] | None = None
        self._pending_navigation_page: int | None = None
        self._bind_pages()
        self.main_window.set_close_requires_controller(True)
        self._apply_access(diagnostic)

    def _bind_pages(self) -> None:
        self.scan_page.recognition_requested.connect(self.start_scan)
        self.scan_page.fresh_response_requested.connect(self.start_fresh_response)
        self.scan_page.cancel_requested.connect(self.cancel_active)
        self.scan_page.help_requested.connect(self.main_window.show_help)
        self.scan_page.reset_requested.connect(self._reset_scan)
        self.scan_page.profile_browse_requested.connect(self._pick_profile)
        self.scan_page.profile_import_requested.connect(self._import_profile)
        self.scan_page.source_browse_requested.connect(self._pick_source)
        self.scan_page.roster_browse_requested.connect(self._pick_roster)
        self.scan_page.sample_roster_requested.connect(self._sample_roster)
        self.grading_page.answer_key_browse_requested.connect(self._pick_answer_key)
        self.grading_page.answer_key_dropped.connect(self._validate_answer_key)
        self.grading_page.other_response_requested.connect(self._pick_other_response)
        self.grading_page.sample_download_requested.connect(self._sample_answer_key)
        self.grading_page.grade_requested.connect(self.start_grading)
        self.grading_page.cancel_requested.connect(self.cancel_active)
        self.grading_page.result_navigation_requested.connect(self._navigate_results)
        self.dashboard_page.request_emitted.connect(self._handle_dashboard_request)
        self.detail_page.back_requested.connect(self._detail_back)
        self.detail_page.save_requested.connect(self._save_detail)
        self.detail_page.discard_requested.connect(self._discard_detail)
        self.detail_page.close_requested.connect(self._close_detail)
        self.detail_page.unsaved_changes_requested.connect(self._confirm_detail_exit)
        self.detail_page.work_item_load_requested.connect(self._load_detail_work_item)
        self.detail_page.preview_requested.connect(self._preview_detail)
        self.settings_page.save_requested.connect(self._save_settings)
        self.settings_page.profile_browse_requested.connect(self._pick_settings_profile)
        self.settings_page.profile_import_requested.connect(self._import_profile)
        self.main_window.write_authority_requested.connect(self.set_write_authority)
        self.main_window.detail_navigation_requested.connect(self._detail_navigation_requested)
        self.main_window.close_requested.connect(self.close)
        self._load_g006_pages()

    def _apply_access(self, diagnostic: str | None) -> None:
        self.main_window.set_write_authority(self.write_enabled)
        self.scan_page.set_write_enabled(self.write_enabled)
        self.grading_page.set_write_enabled(self.write_enabled)
        self.detail_page.set_write_enabled(self.write_enabled)
        self.settings_page.set_write_enabled(self.write_enabled, diagnostic)
        if not self.write_enabled:
            self.main_window.set_status(get_message("status.read_only"))
            self.main_window.show_diagnostic(diagnostic or get_message("bootstrap.read_only_body"))

    def _load_g006_pages(self) -> None:
        dashboard_load = self.services.dashboard_load
        if dashboard_load is None:
            self.main_window.show_diagnostic(_error_text(self._unavailable()))
        else:
            result = dashboard_load()
            if isinstance(result, Ok) and isinstance(result.value, DashboardListing):
                self.dashboard_page.set_entries(result.value.entries)
            else:
                error = (
                    result.errors[0] if isinstance(result, Err) else self._invalid_service_result()
                )
                self.main_window.show_diagnostic(_error_text(error))
        self._refresh_profile_catalog()
        settings_load = self.services.settings_load
        if settings_load is None:
            self.settings_page.set_settings_unavailable(_error_text(self._unavailable()))
            return
        settings_result = settings_load()
        if isinstance(settings_result, Ok) and isinstance(settings_result.value, SettingsState):
            self._apply_settings_snapshot(
                settings_result.value.settings, settings_result.value.revision
            )
            self._present_warnings(settings_result.warnings)
        else:
            error = (
                settings_result.errors[0]
                if isinstance(settings_result, Err)
                else self._invalid_service_result()
            )
            self.settings_page.set_settings_unavailable(_error_text(error))

    def _handle_dashboard_request(self, request: DashboardRequest) -> None:
        if request.action == "detail":
            detail_handler = self.services.dashboard_detail
            detail_operation = (
                None if detail_handler is None else partial(detail_handler, request.selection)
            )
            self._start_desktop_action(
                self.dashboard_page,
                detail_operation,
                self._show_detail,
                self.dashboard_page.set_busy,
            )
            return
        if request.action == "trash":
            self._start_desktop_action(
                self.dashboard_page,
                self.services.dashboard_trash_load,
                self._show_trash,
                self.dashboard_page.set_busy,
            )
            return
        handlers: dict[str, tuple[Callable[..., object] | None, type[object], bool]] = {
            "delete": (self.services.dashboard_delete, SoftDeleteResult, True),
            "backup": (self.services.dashboard_backup, BackupExportResult, False),
            "restore": (self.services.dashboard_restore, BackupRestoreResult, True),
            "combined": (self.services.dashboard_combined, CombinedReportResult, False),
            "trash_restore": (self.services.dashboard_trash, TrashRestoreResult, True),
            "trash_delete": (self.services.dashboard_trash, PermanentDeleteResult, True),
        }
        item = handlers.get(request.action)
        if item is None:
            self.main_window.show_diagnostic(_error_text(self._unavailable()))
            return
        action_handler, expected_type, refresh_dashboard = item
        operation: Callable[[], object] | None
        if action_handler is None:
            operation = None
        elif request.action == "restore":
            operation = action_handler
        elif request.action.startswith("trash_"):
            operation = partial(action_handler, request)
        else:
            operation = partial(action_handler, request.selection)
        self._start_desktop_action(
            self.dashboard_page,
            operation,
            lambda result: self._finish_dashboard_action(
                result, expected_type, refresh_dashboard=refresh_dashboard
            ),
            self.dashboard_page.set_busy,
        )

    def _show_detail(self, result: object) -> None:
        if not isinstance(result, DetailPageDisplay):
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        self.detail_page.set_display(result)
        self.main_window.show_detail()

    def _show_trash(self, result: object) -> None:
        if not isinstance(result, DashboardListing):
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        dialog = self.dashboard_page.create_trash_dialog(result.entries)
        dialog.exec()

    def _start_desktop_action(
        self,
        page: DashboardPage | SettingsPage,
        operation: Callable[[], object] | None,
        completed: Callable[[object], None],
        busy: Callable[[bool], None],
        *,
        operation_id: str | None = None,
    ) -> None:
        if self._closing or self._active_bridge is not None and self._active_bridge.active:
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        if operation is None:
            self.main_window.show_diagnostic(_error_text(self._unavailable()))
            return
        self._desktop_success = completed
        self._desktop_busy = busy
        self._start(
            page,
            operation_id or uuid4().hex,
            lambda _cancelled, _progress: operation(),
            kind="desktop-service",
        )

    def _finish_dashboard_action(
        self, result: object, expected_type: type[object], *, refresh_dashboard: bool = False
    ) -> None:
        if not isinstance(result, expected_type):
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        if refresh_dashboard and self.services.dashboard_load is not None:
            loaded = self.services.dashboard_load()
            if isinstance(loaded, Ok) and isinstance(loaded.value, DashboardListing):
                self.dashboard_page.set_entries(loaded.value.entries)
            else:
                error = (
                    loaded.errors[0] if isinstance(loaded, Err) else self._invalid_service_result()
                )
                self.main_window.show_diagnostic(_error_text(error))

    def _load_detail_work_item(self, request: object) -> None:
        if not isinstance(request, DetailLoadRequest):
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        handler = self.services.detail_load
        if handler is None:
            self.main_window.show_diagnostic(_error_text(self._unavailable()))
            return
        result = handler(request)
        if isinstance(result, Err):
            self.main_window.show_diagnostic(_error_text(result.errors[0]))
            return
        if (
            not isinstance(result, Ok)
            or not isinstance(result.value, DetailLoadResult)
            or result.value.correlation_id != request.correlation_id
            or result.value.student.work_item_id != request.work_item_id
        ):
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        self.detail_page.apply_loaded_work_item(result.value)

    def _preview_detail(self, request: object) -> None:
        if not isinstance(request, DetailPageRequest) or request.intent != "preview":
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        handler = self.services.detail_preview
        if handler is None:
            self.main_window.show_diagnostic(_error_text(self._unavailable()))
            return
        result = handler(request)
        if isinstance(result, Err):
            self.main_window.show_diagnostic(_error_text(result.errors[0]))
            return
        if (
            not isinstance(result, Ok)
            or not isinstance(result.value, DetailPreviewResult)
            or result.value.correlation_id != request.correlation_id
            or not (
                result.value.display.session_id == request.session_id
                and result.value.display.revision == request.revision
                and result.value.display.detail_handle == request.detail_handle
            )
        ):
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        self.detail_page.apply_preview(result.value)

    def _save_detail(self, request: object) -> None:
        if not self.write_enabled:
            self._clear_pending_detail_navigation()
            self.detail_page.set_write_enabled(False)
            if isinstance(request, DetailPageRequest):
                self.detail_page.save_failed(request.correlation_id)
            return
        if not isinstance(request, DetailPageRequest) or request.intent != "save":
            self._clear_pending_detail_navigation()
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        handler = self.services.detail_save
        if handler is None:
            self._clear_pending_detail_navigation()
            self.detail_page.save_failed(request.correlation_id)
            self.main_window.show_diagnostic(_error_text(self._unavailable()))
            return
        result = handler(request)
        if isinstance(result, Err):
            self._clear_pending_detail_navigation()
            self.detail_page.save_failed(request.correlation_id)
            self.main_window.show_diagnostic(_error_text(result.errors[0]))
            return
        if (
            not isinstance(result, Ok)
            or not isinstance(result.value, DetailSaveResult)
            or result.value.correlation_id != request.correlation_id
            or not self._matches_detail_save(result.value.display, request)
        ):
            self._clear_pending_detail_navigation()
            self.detail_page.save_failed(request.correlation_id)
            self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
            return
        self.detail_page.save_completed(result.value)
        if self._pending_detail_exit is not None:
            self._finish_detail_exit(
                replace(
                    self._pending_detail_exit,
                    session_id=result.value.display.session_id,
                    revision=result.value.display.revision,
                    detail_handle=result.value.display.detail_handle,
                )
            )

    @staticmethod
    def _matches_detail_save(display: DetailPageDisplay, request: DetailPageRequest) -> bool:
        return display.session_id == request.session_id and display.revision > request.revision

    def _detail_back(self, request: object) -> None:
        if isinstance(request, DetailPageRequest):
            self._finish_detail_exit(request)

    def _close_detail(self, request: object) -> None:
        if isinstance(request, DetailPageRequest):
            self._finish_detail_exit(request)

    def _discard_detail(self, request: object) -> None:
        if isinstance(request, DetailPageRequest):
            self._finish_detail_exit(request)

    def _confirm_detail_exit(self, request: object) -> None:
        if not isinstance(request, DetailPageRequest):
            self._clear_pending_detail_navigation()
            return
        self._pending_detail_exit = request
        decision = self.main_window.confirm_detail_exit()
        if decision == "save":
            self._save_detail(replace(request, intent="save"))
        elif decision == "discard":
            self._discard_detail(replace(request, intent="discard"))
        else:
            self._clear_pending_detail_navigation()
            self.main_window.cancel_close_request()

    def _detail_navigation_requested(self, page_index: int) -> None:
        self._pending_navigation_page = page_index
        self.detail_page.request_back()

    def _clear_pending_detail_navigation(self) -> None:
        self._pending_detail_exit = None
        self._pending_navigation_page = None

    def _finish_detail_exit(self, request: DetailPageRequest) -> None:
        if request.detail_handle is not None:
            handler = self.services.detail_close
            if handler is None:
                self._clear_pending_detail_navigation()
                self.main_window.show_diagnostic(_error_text(self._unavailable()))
                return
            result = handler(replace(request, intent="close", edits=()))
            if isinstance(result, Err):
                self._clear_pending_detail_navigation()
                self.main_window.show_diagnostic(_error_text(result.errors[0]))
                return
            if not isinstance(result, Ok) or result.value is not None:
                self._clear_pending_detail_navigation()
                self.main_window.show_diagnostic(_error_text(self._invalid_service_result()))
                return
            self._present_warnings(result.warnings)
        target = self._pending_navigation_page
        self._clear_pending_detail_navigation()
        self.detail_page.set_display(None)
        if target is not None:
            self.main_window.show_dashboard()
            self.main_window.navigate_to(target)
        elif request.intent == "close":
            self._shutdown_for_close()
        else:
            self.main_window.show_dashboard()

    def _save_settings(self, request: SettingsPageRequest) -> None:
        if not self.write_enabled:
            self.settings_page.set_write_enabled(False, get_message("status.read_only"))
            return
        if self._settings_snapshot is None:
            self.settings_page.set_settings_unavailable(_error_text(self._unavailable()))
            return
        handler = self.services.settings_save
        if handler is None:
            self.settings_page.set_save_error(self._unavailable())
            return
        if request.expected_revision != self._settings_revision or not isinstance(
            request.settings, Settings
        ):
            self.settings_page.set_save_error(self._invalid_service_result())
            return
        command = SettingsSaveCommand(request.settings, request.expected_revision, uuid4().hex)
        self._start_desktop_action(
            self.settings_page,
            lambda: handler(command),
            lambda result: self._finish_settings_save(result, command),
            self.settings_page.set_busy,
            operation_id=command.operation_id,
        )

    def _finish_settings_save(self, result: object, command: SettingsSaveCommand) -> None:
        if (
            not isinstance(result, SettingsSaveResult)
            or not result.committed
            or result.operation_id != command.operation_id
            or command.operation_id != self._active_operation_id
            or result.revision <= command.expected_revision
        ):
            self.settings_page.set_save_error(self._invalid_service_result())
            return
        loader = self.services.settings_load
        if loader is None:
            self.settings_page.set_save_error(self._unavailable())
            return
        reloaded = loader()
        if (
            not isinstance(reloaded, Ok)
            or not isinstance(reloaded.value, SettingsState)
            or reloaded.value.revision != result.revision
        ):
            error = (
                reloaded.errors[0] if isinstance(reloaded, Err) else self._invalid_service_result()
            )
            self.settings_page.set_save_error(error)
            return
        self._apply_settings_snapshot(reloaded.value.settings, reloaded.value.revision)
        self.settings_page.set_saved(result)
        self._present_warnings(reloaded.warnings)

    def set_write_authority(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("write authority must be bool")
        self.write_enabled = self.write_enabled and self._write_capability and enabled
        self._apply_access(None)

    def start_scan(self, request: ScanPageRequest) -> None:
        if not self.write_enabled:
            self._present_error(
                self.scan_page, ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied")
            )
            return
        settings = self._settings_snapshot
        if settings is None:
            self._present_error(
                self.scan_page,
                ErrorInfo("SETTINGS_UNAVAILABLE", "error.settings_unavailable"),
            )
            return
        command = ScanCommand(
            request.session_id or f"scan-{uuid4().hex}",
            uuid4().hex,
            0,
            request.exam_name,
            None,
            ExamTerm.UNSPECIFIED,
            request.profile.path,
            request.roster_path,
            ScanSource(request.source.paths),
            request.sensitivity,
            settings.use_multiprocessing,
        )
        self.grading_page.clear_result_available()
        service = self.services.scan
        self._start_write(
            self.scan_page,
            command,
            None if service is None else service.run_scan,
            None if service is None else service.cancel_scan,
            context_operation=self.services.scan_context,
        )

    def start_import(self, command: ImportResponseCommand) -> None:
        self.grading_page.clear_result_available()
        service = self.services.response_import
        self._start_write(
            self.scan_page, command, None if service is None else service.import_response_book
        )

    def start_fresh_response(self) -> None:
        """Pick and atomically import a response workbook as a new session."""
        if not self.write_enabled:
            self._present_error(
                self.scan_page, ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied")
            )
            return
        if self._active_bridge is not None and self._active_bridge.active:
            return
        picker = self.services.fresh_response_picker
        importer = self.services.import_fresh_response_selection
        if picker is None or importer is None:
            self._present_error(self.scan_page, self._unavailable())
            return
        intent = FreshResponseIntent(uuid4().hex)
        selection = picker(intent)
        if selection is None:
            return

        def import_response(value: FreshResponseIntent) -> object:
            return importer(value, selection)

        self.grading_page.clear_result_available()
        self._start_write(
            self.scan_page,
            intent,
            import_response,
            kind="fresh-response-import",
        )

    def start_grading(self, request: GradingPageRequest) -> None:
        if request.answer_key_path is None or request.answer_key_sheet is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        command = RegradeCommand(
            request.session_id,
            request.revision,
            request.answer_key_path,
            request.answer_key_sheet,
            request.operation_id,
        )
        service = self.services.grading
        self._start_write(
            self.grading_page,
            command,
            None if service is None else service.regrade,
            self.services.cancel_operation,
            context_operation=self.services.grading_context,
            kind="grading",
            session_identity=(request.session_id, request.revision),
        )

    def _start_write[CommandT: WriteCommand](
        self,
        page: ScanPage | GradingPage,
        command: CommandT,
        operation: Callable[[CommandT], object] | None,
        cancel: Callable[[CancelOperationCommand], Result[None]] | None = None,
        *,
        context_operation: Callable[[CommandT, Event, Callable[[object], None]], object]
        | None = None,
        kind: str = "write",
        session_identity: tuple[str, int] | None = None,
    ) -> None:
        if not self.write_enabled:
            self._present_error(page, ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied"))
            return
        if operation is None and context_operation is None:
            self._present_error(page, self._unavailable())
            return
        operation_id = command.operation_id

        def worker(cancelled: Event, progress: Callable[[object], None]) -> object:
            if context_operation is not None:
                return context_operation(command, cancelled, progress)
            if operation is None:
                raise RuntimeError("operation contract disappeared")
            return operation(command)

        cancel_hook: CancelHook | None = None
        if cancel is not None:

            def cancel_hook() -> Result[None]:
                cancelled = cancel(CancelOperationCommand(operation_id))
                if isinstance(cancelled, Ok) and cancelled.value is None:
                    return cancelled
                if isinstance(cancelled, Err):
                    return cancelled
                return Err((self._invalid_service_result(),))

        self._start(
            page,
            operation_id,
            worker,
            cancel_hook,
            kind=kind,
            session_identity=session_identity,
        )

    def _start(
        self,
        page: ScanPage | GradingPage | DashboardPage | SettingsPage,
        operation_id: str,
        operation: Operation,
        cancel_hook: CancelHook | None = None,
        *,
        kind: str = "write",
        session_identity: tuple[str, int] | None = None,
    ) -> None:
        if self._closing or self._active_bridge is not None and self._active_bridge.active:
            error = ErrorInfo("UI_OPERATION_FAILED", "error.ui_operation_failed")
            if isinstance(page, ScanPage | GradingPage):
                self._present_error(page, error)
            else:
                self.main_window.show_diagnostic(_error_text(error))
            return
        bridge = WorkerBridge(parent=self)
        self._active_page, self._active_operation_id, self._active_bridge = (
            page,
            operation_id,
            bridge,
        )
        self._active_kind = kind
        self._active_session_identity = session_identity
        self._active_cancellable = cancel_hook is not None
        bridge.progress.connect(self._progress)
        bridge.succeeded.connect(self._succeeded)
        bridge.failed.connect(self._failed)
        bridge.cancelled.connect(self._cancelled)
        bridge.terminal.connect(self._terminal)
        bridge.finished.connect(self._finished)
        bridge.close_finished.connect(self._close_finished)
        self.main_window.set_close_requires_controller(True)
        self._set_busy(page, True, operation_id)
        self.main_window.set_status(get_message("status.processing"))
        bridge.start(operation, cancel_hook=cancel_hook)

    def cancel_active(self, operation_id: object = None) -> None:
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or operation_id != self._active_operation_id
            or self._active_bridge is None
        ):
            return
        self._active_bridge.cancel()

    def close(self) -> None:
        if self._closing:
            return
        if self.main_window.exam_page.currentWidget() is self.detail_page:
            self.detail_page.request_close()
            return
        self._closing = True
        self._shutdown_for_close()

    def _shutdown_for_close(self) -> None:
        self._closing = True
        self._desktop_success = None
        self._desktop_busy = None
        bridge = self._active_bridge
        if bridge is None or not bridge.active:
            self.main_window.allow_close()
            return
        bridge.close()

    def _finished(self) -> None:
        bridge = self.sender()
        if not isinstance(bridge, WorkerBridge) or bridge is not self._active_bridge:
            return
        self._active_bridge = None
        self._retired_bridges.append(bridge)
        if len(self._retired_bridges) > 64:
            self._retired_bridges.popleft().deleteLater()
        if not self._closing:
            self.main_window.set_close_requires_controller(True)

    def _close_finished(self) -> None:
        if not self._closing or self._close_finished_handled:
            return
        self._close_finished_handled = True
        self.main_window.allow_close()

    def _progress(self, progress: object) -> None:
        if self._closing or self._active_page is None:
            return
        if isinstance(self._active_page, ScanPage) and isinstance(progress, ScanProgress):
            self._active_page.set_progress(
                progress.completed,
                progress.total,
                progress.failed,
                elapsed_seconds=progress.elapsed_ms / 1000,
                eta_seconds=None if progress.eta_ms is None else progress.eta_ms / 1000,
            )
        elif isinstance(self._active_page, GradingPage) and isinstance(
            progress, GradingProgressDisplay
        ):
            self._active_page.set_grading_progress(progress)

    def _succeeded(self, result: object) -> None:
        if self._closing:
            return
        if self._active_kind == "desktop-service":
            callback = self._desktop_success
            if callback is not None:
                callback(result)
            return
        if self._closing or self._active_page is None:
            return
        page = self._active_page
        connected_session: ConnectedSessionDisplay | None = None
        if self._active_kind in {"response-import", "fresh-response-import"}:
            if not isinstance(result, ConnectedSessionDisplay):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return
            if self._active_kind == "fresh-response-import" and (
                result.revision != 1 or result.is_regrade
            ):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return
            connected_session = result
        elif self._active_kind in {"write", "grading"}:
            if (
                not isinstance(result, SessionCreateResult | CommitGenerationResult)
                or not result.committed
                or result.operation_id != self._active_operation_id
            ):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return
            session_display = self.services.session_display
            if session_display is None:
                self._present_error(page, self._unavailable())
                self.main_window.set_status(get_message("status.failed"))
                return
            session_display_result = session_display(result)
            if isinstance(session_display_result, Err):
                self._present_error(page, session_display_result.errors[0])
                self.main_window.set_status(get_message("status.failed"))
                return
            if not isinstance(session_display_result, Ok) or not isinstance(
                session_display_result.value, ConnectedSessionDisplay
            ):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return
            connected_session = session_display_result.value
            if (
                connected_session.session_id != result.session_id
                or connected_session.revision != result.revision
            ):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return
            self._present_warnings(session_display_result.warnings)

        if self._active_kind == "answer-key-validation" and self._validation_request is not None:
            if not isinstance(result, AnswerKeyValidationDisplay):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return
            self.grading_page.set_validation_result(result)
        elif isinstance(page, ScanPage):
            page.set_result(result)
        elif self._active_kind == "grading":
            identity = self._active_session_identity
            if (
                identity is None
                or not isinstance(result, CommitGenerationResult)
                or not result.committed
                or result.session_id != identity[0]
                or result.revision != identity[1] + 1
                or connected_session is None
                or connected_session.session_id != result.session_id
                or connected_session.revision != result.revision
            ):
                self._present_error(page, self._invalid_service_result())
                self.main_window.set_status(get_message("status.failed"))
                return

        if connected_session is not None:
            self.grading_page.set_connected_session(connected_session)
            if self._active_kind == "grading":
                self.grading_page.set_result_available(
                    connected_session.session_id, connected_session.revision
                )
            else:
                self.grading_page.clear_result_available()
            self.grading_page.set_operation_id(uuid4().hex)
            self.main_window.set_current_session(connected_session.exam_name)
            self.main_window.set_grading_available(True)
            if self._active_kind == "fresh-response-import":
                self.main_window.navigate_to(self.main_window.GRADING_PAGE)
        self.main_window.set_status(get_message("status.completed"))

    def _failed(self, error: object) -> None:
        if self._closing:
            return
        if self._active_kind == "desktop-service":
            self.main_window.show_diagnostic(_error_text(error))
            return
        if not self._closing and self._active_page is not None:
            self._present_error(self._active_page, error)
            self.main_window.set_status(get_message("status.failed"))

    def _cancelled(self) -> None:
        if self._closing:
            return
        if self._active_kind == "desktop-service":
            self.main_window.set_status("작업이 취소되었습니다")
            return
        if self._closing or self._active_page is None:
            return
        if isinstance(self._active_page, ScanPage):
            self._active_page.set_cancelled()
        elif isinstance(self._active_page, GradingPage):
            self._active_page.complete_cancel()
        self.main_window.set_status("작업이 취소되었습니다")

    def _terminal(self) -> None:
        page = self._active_page
        if page is not None and not self._closing:
            next_operation_id = uuid4().hex if isinstance(page, GradingPage) else None
            self._set_busy(page, False, next_operation_id)
        self._active_page, self._active_operation_id = None, None
        self._active_kind = ""
        self._active_session_identity = None
        self._active_cancellable = False
        self._desktop_success = None
        self._desktop_busy = None

    def _set_busy(
        self,
        page: ScanPage | GradingPage | DashboardPage | SettingsPage,
        busy: bool,
        operation_id: str | None,
    ) -> None:
        if isinstance(page, ScanPage):
            page.set_busy(
                busy,
                operation_id,
                cancellable=busy and self._active_cancellable,
            )
        elif isinstance(page, GradingPage):
            page.set_operation_id(operation_id)
            page.set_busy(busy)
            if busy and not self._active_cancellable:
                page.cancel_button.setEnabled(False)
        else:
            page.set_busy(busy)

    def _present_error(
        self,
        page: ScanPage | GradingPage | DashboardPage | SettingsPage,
        error: object,
    ) -> None:
        if isinstance(page, ScanPage | GradingPage):
            page.set_error(_error_text(error))
        else:
            self.main_window.show_diagnostic(_error_text(error))

    def _pick_profile(self) -> None:
        picker = self.services.profile_picker
        if picker is None:
            self._present_error(self.scan_page, self._unavailable())
            return
        selection = picker()
        if selection is None:
            self.scan_page.set_profile_picker_cancelled()
        else:
            self._import_profile(selection)

    def _pick_settings_profile(self) -> None:
        if not self.write_enabled:
            self.settings_page.set_write_enabled(False, get_message("status.read_only"))
            return
        picker = self.services.profile_picker
        if picker is None:
            self.settings_page.set_save_error(self._unavailable())
            return
        selection = picker()
        if selection is not None:
            self._import_profile(selection)

    def _refresh_profile_catalog(self) -> tuple[SettingsProfileCandidate, ...]:
        catalog_loader = self.services.profile_catalog
        if catalog_loader is None:
            self.scan_page.set_profiles(())
            self.settings_page.set_profile_candidates(())
            return ()
        catalog = catalog_loader()
        if isinstance(catalog, Err):
            self.settings_page.set_save_error(catalog.errors[0])
            return ()
        profiles: list[ValidatedProfileState] = []
        candidates: list[SettingsProfileCandidate] = []
        for item in catalog.value:
            diagnostic = (
                None
                if item.is_valid
                else _error_text(item.diagnostics[0])
                if item.diagnostics
                else "프로필을 검증할 수 없습니다."
            )
            candidates.append(SettingsProfileCandidate(item.filename, item.is_valid, diagnostic))
            if item.profile is not None:
                dimensions = (
                    (item.profile.page.source_width, item.profile.page.source_height)
                    if item.profile.page is not None
                    else None
                )
                profiles.append(
                    ValidatedProfileState(
                        item.profile.profile_name,
                        item.filename,
                        dimensions,
                        f"{len(item.profile.regions)}개 영역",
                        tuple(_error_text(error) for error in item.diagnostics),
                        validated=item.is_valid,
                    )
                )
        self.scan_page.set_profiles(tuple(profiles))
        self.settings_page.set_profile_candidates(tuple(candidates))
        return tuple(candidates)

    def _import_profile(self, selection: ImportSelection) -> None:
        if not isinstance(selection, ImportSelection) or selection.kind is not ImportKind.PROFILE:
            raise TypeError("selection must be a profile ImportSelection")
        if not self.write_enabled:
            self._present_error(
                self.scan_page, ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied")
            )
            return
        if self._settings_snapshot is None:
            self._present_error(
                self.scan_page,
                ErrorInfo("SETTINGS_UNAVAILABLE", "error.settings_unavailable"),
            )
            return
        importer = self.services.profile_import
        if importer is None:
            self._present_error(self.scan_page, self._unavailable())
            return
        self._start_desktop_action(
            self.settings_page,
            lambda: importer(
                ProfileImportRequest(selection.paths[0], CollisionPolicy.ERROR, None, "controller")
            ),
            self._finish_profile_import,
            self.settings_page.set_busy,
        )

    def _finish_profile_import(self, result: object) -> None:
        if not isinstance(result, ProfileImportResult):
            self.settings_page.set_save_error(self._invalid_service_result())
            return
        candidates = self._refresh_profile_catalog()
        candidate = next((item for item in candidates if item.name == result.stored_name), None)
        if candidate is None:
            self.settings_page.set_save_error(self._invalid_service_result())
            return
        self.settings_page.set_imported_profile(candidate, candidates)

    def _apply_settings_snapshot(self, settings: Settings, revision: int) -> None:
        self._settings_snapshot = settings
        self._settings_revision = revision
        self.settings_page.set_settings(settings, revision)
        self.scan_page.set_defaults(settings.default_profile, settings.default_sensitivity)

    def _pick_source(self, kind: ImportKind) -> None:
        picker = self.services.source_picker
        if picker is None:
            self._present_error(self.scan_page, self._unavailable())
            return
        selection = picker(kind)
        if selection is None:
            self.scan_page.set_source_picker_cancelled()
        else:
            self.scan_page.set_source(selection)

    def _pick_roster(self, kind: ImportKind) -> None:
        picker = self.services.roster_picker
        if picker is None:
            self._present_error(self.scan_page, self._unavailable())
            return
        selection = picker(kind)
        if selection is None:
            self.scan_page.set_roster_picker_cancelled()
        else:
            self.scan_page.set_roster(selection.paths[0])

    def _sample_roster(self) -> None:
        if not self.write_enabled:
            self._present_error(
                self.scan_page, ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied")
            )
            return
        if self.services.sample_roster is None:
            self._present_error(self.scan_page, self._unavailable())
            return
        self.services.sample_roster()

    def _pick_answer_key(self, request: GradingPageRequest) -> None:
        picker = self.services.answer_key_picker
        if picker is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        selection = picker(request)
        if selection is not None:
            self.grading_page.set_answer_key_selection(*selection)
            self._validate_answer_key(
                replace(
                    request,
                    answer_key_path=selection[0],
                    answer_key_sheet=selection[1],
                )
            )

    def _validate_answer_key(self, request: GradingPageRequest) -> None:
        if request.answer_key_path is None or request.answer_key_sheet is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        service = self.services.answer_key
        if service is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        validation_request = AnswerKeyRequest(request.answer_key_path, request.answer_key_sheet)
        self._validation_request = validation_request

        def validate(_: Event, __: Callable[[object], None]) -> object:
            display = GradingPresenter.validation(service.validate_answer_key(validation_request))
            return replace(
                display,
                source_path=validation_request.path,
                sheet_name=validation_request.sheet_name,
            )

        self._start(
            self.grading_page,
            request.operation_id,
            validate,
            kind="answer-key-validation",
        )

    def _pick_other_response(self, request: GradingPageRequest) -> None:
        picker = self.services.other_response_picker
        importer = self.services.import_response_selection
        if picker is None or importer is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        selection = picker(request)
        if selection is None:
            return

        def import_response(command: GradingPageRequest) -> object:
            return importer(command, selection)

        self.grading_page.clear_result_available()
        self._start_write(
            self.grading_page,
            request,
            import_response,
            kind="response-import",
        )

    def _sample_answer_key(self, request: GradingPageRequest) -> None:
        if not self.write_enabled:
            self._present_error(
                self.grading_page, ErrorInfo("ROOT_WRITE_DENIED", "error.root_write_denied")
            )
            return
        if self.services.sample_answer_key is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        self.services.sample_answer_key(request)

    def _navigate_results(self, request: GradingPageRequest) -> None:
        if self.services.result_navigation is None:
            self._present_error(self.grading_page, self._unavailable())
            return
        self.services.result_navigation(request)

    def _reset_scan(self) -> None:
        self.main_window.set_status("입력 항목을 초기화했습니다.")

    def _present_warnings(self, warnings: tuple[ErrorInfo, ...]) -> None:
        for warning in warnings:
            self.main_window.show_diagnostic(_error_text(warning))

    @staticmethod
    def _unavailable() -> ErrorInfo:
        return ErrorInfo(
            "SERVICE_UNAVAILABLE",
            "error.service_unavailable",
            context={"reason": "현재 작업 서비스를 사용할 수 없습니다."},
        )

    @staticmethod
    def _invalid_service_result() -> ErrorInfo:
        return ErrorInfo(
            "INVALID_SERVICE_RESULT",
            "error.invalid_service_result",
            context={"reason": "작업 서비스가 올바르지 않은 응답을 반환했습니다."},
        )


__all__ = ["AppController", "FreshResponseIntent", "ServicePorts"]
