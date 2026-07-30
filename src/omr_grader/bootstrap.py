"""Portable application bootstrap with a deliberately lazy Qt boundary."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.capabilities import (
    CapabilityToken,
    bootstrap_managed_paths,
    probe_root_capability,
)
from omr_grader.infrastructure.config_store import AppConfig, load_config
from omr_grader.infrastructure.logging_setup import configure_logging, daily_log_path
from omr_grader.infrastructure.paths import ManagedPaths, resolve_portable_root
from omr_grader.resources.messages import get_message
from omr_grader.workbooks.schemas import RESPONSE_SHEET_NAME

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication, QSplashScreen


@dataclass(frozen=True, slots=True)
class BootstrapState:
    """Runtime services available after the non-GUI startup sequence."""

    paths: ManagedPaths
    config: AppConfig | None
    write_enabled: bool
    diagnostic: str | None = None
    capability_token: CapabilityToken | None = None


def _state(
    paths: ManagedPaths,
    *,
    config: AppConfig | None = None,
    write_enabled: bool,
    diagnostic: str | None = None,
    capability_token: CapabilityToken | None = None,
) -> BootstrapState:
    return BootstrapState(
        paths=paths,
        config=config,
        write_enabled=write_enabled,
        diagnostic=diagnostic,
        capability_token=capability_token,
    )


def _canonical_response_workbook_selection(path: str) -> tuple[str, str] | None:
    """Return the only accepted response-workbook route from the native picker."""
    if not path or Path(path).suffix.lower() != ".xlsx":
        return None
    return path, RESPONSE_SHEET_NAME


T = TypeVar("T")
_RUNTIME_REFERENCES: tuple[object, ...] | None = None


def _warning_copy(issue: ErrorInfo) -> ErrorInfo:
    if issue.message_key.startswith("warning."):
        return issue
    return ErrorInfo(
        issue.code,
        f"warning.{issue.code.lower()}",
        issue.field_path,
        dict(issue.context),
        issue.retryable,
        issue.cause_type,
    )


def _result_warnings(result: Result[T]) -> tuple[ErrorInfo, ...]:
    if isinstance(result, Ok):
        return result.warnings
    return tuple(_warning_copy(issue) for issue in result.errors)


def bootstrap(paths: ManagedPaths | None = None) -> Result[BootstrapState]:
    """Prepare portable services without importing or initializing PySide6."""
    managed_paths = paths if paths is not None else ManagedPaths.from_root(resolve_portable_root())
    capability = probe_root_capability(managed_paths)
    if isinstance(capability, Err):
        configure_logging()
        return capability

    root = capability.value
    if not root.write_enabled:
        logging_result = configure_logging()
        config = load_config(root.paths)
        if isinstance(config, Err):
            return Ok(
                _state(
                    root.paths,
                    write_enabled=False,
                    diagnostic=root.read_only_reason,
                ),
                capability.warnings
                + _result_warnings(logging_result)
                + tuple(_warning_copy(issue) for issue in config.errors),
            )
        return Ok(
            BootstrapState(
                root.paths,
                None if config.warnings else config.value,
                False,
                root.read_only_reason,
            ),
            capability.warnings + _result_warnings(logging_result) + config.warnings,
        )

    token = root.token
    if token is None:
        logging_result = configure_logging()
        return Err(
            (
                ErrorInfo(
                    "INVALID_CAPABILITY_TOKEN",
                    "error.invalid_capability_token",
                    context={"reason": "쓰기 권한에 필요한 토큰이 없습니다."},
                ),
            )
            + (logging_result.errors if isinstance(logging_result, Err) else ())
        )
    managed = bootstrap_managed_paths(root.paths, token)
    if isinstance(managed, Err):
        logging_result = configure_logging()
        diagnostic = get_message("bootstrap.read_only_body")
        return Ok(
            _state(root.paths, write_enabled=False, diagnostic=diagnostic),
            capability.warnings
            + tuple(_warning_copy(issue) for issue in managed.errors)
            + _result_warnings(logging_result),
        )

    config = load_config(managed.value, token)
    if isinstance(config, Err):
        logging_result = configure_logging()
        return Ok(
            _state(
                managed.value,
                write_enabled=False,
                diagnostic=str(
                    config.errors[0].context.get("reason", get_message("bootstrap.read_only_body"))
                ),
            ),
            capability.warnings
            + tuple(_warning_copy(issue) for issue in config.errors)
            + _result_warnings(logging_result),
        )
    if any(warning.code == "CONFIG_MISSING" for warning in config.warnings):
        reloaded = load_config(managed.value, token)
        if isinstance(reloaded, Err) or any(
            warning.code == "CONFIG_MISSING" for warning in reloaded.warnings
        ):
            logging_result = configure_logging()
            errors = reloaded.errors if isinstance(reloaded, Err) else ()
            return Ok(
                _state(
                    managed.value,
                    write_enabled=False,
                    diagnostic=str(
                        errors[0].context.get("reason", get_message("bootstrap.read_only_body"))
                        if errors
                        else get_message("bootstrap.read_only_body")
                    ),
                ),
                capability.warnings
                + tuple(_warning_copy(issue) for issue in errors)
                + (reloaded.warnings if isinstance(reloaded, Ok) else ())
                + _result_warnings(logging_result),
            )
        config = reloaded
    persistence_warning = next(
        (warning for warning in config.warnings if warning.code != "CONFIG_MISSING"),
        None,
    )
    if persistence_warning is not None:
        logging_result = configure_logging()
        return Ok(
            _state(
                managed.value,
                write_enabled=False,
                diagnostic=str(persistence_warning.context["reason"]),
            ),
            capability.warnings + config.warnings + _result_warnings(logging_result),
        )
    daily_target = daily_log_path(managed.value.root)
    log_target = managed.value.log_path(daily_target.name)
    if isinstance(log_target, Err):
        logging_result = configure_logging()
        return Ok(
            _state(
                managed.value,
                write_enabled=False,
                diagnostic=str(log_target.errors[0].context["reason"]),
            ),
            capability.warnings
            + config.warnings
            + tuple(_warning_copy(issue) for issue in log_target.errors)
            + _result_warnings(logging_result),
        )
    logging_result = configure_logging(log_target.value)
    return Ok(
        BootstrapState(managed.value, config.value, True, capability_token=token),
        capability.warnings + config.warnings + _result_warnings(logging_result),
    )


def _diagnostic_from_errors(errors: tuple[ErrorInfo, ...]) -> str:
    if any(error.code == "ROOT_WRITE_DENIED" for error in errors):
        return get_message("bootstrap.read_only_body")
    return get_message("bootstrap.unavailable_body")


def _create_startup_splash() -> QSplashScreen:
    """Create the native startup surface before bootstrap and heavy imports."""
    from omr_grader.startup import create_splash

    return create_splash()


def _configure_application_branding(application: QApplication) -> None:
    from omr_grader.startup import configure_application_branding

    configure_application_branding(application)


def run(
    *,
    application: QApplication | None = None,
    startup_splash: QSplashScreen | None = None,
) -> int:
    """Launch the Qt shell after the portable bootstrap has completed."""
    global _RUNTIME_REFERENCES
    if sys.platform == "win32":
        from multiprocessing import freeze_support

        freeze_support()

    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    existing = application if application is not None else QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv)
    QApplication.setApplicationName(get_message("app.title"))
    QApplication.setOrganizationName(get_message("app.organization"))
    QApplication.setApplicationDisplayName(get_message("app.title"))
    _configure_application_branding(app)
    splash = startup_splash
    if splash is None:
        splash = _create_startup_splash()
        splash.show()
        app.processEvents()

    outcome = bootstrap()

    # Keep the remaining heavy imports behind the visible startup surface.
    from omr_grader.application.answer_key_use_case import AnswerKeyWorkbookUseCase
    from omr_grader.application.backup_use_case import BackupApplicationService
    from omr_grader.application.correction_use_case import (
        CorrectionApplicationService,
        CorrectionPreview,
    )
    from omr_grader.application.dashboard_use_case import (
        DashboardApplicationService,
        DashboardUseCase,
    )
    from omr_grader.application.detail_presenter import (
        DetailAnswerDisplay,
        DetailAnswerEdit,
        DetailIdEdit,
        DetailLoadRequest,
        DetailLoadResult,
        DetailPageDisplay,
        DetailPageRequest,
        DetailPreviewResult,
        DetailSaveResult,
        DetailStudentDisplay,
        DetailSummaryDisplay,
    )
    from omr_grader.application.dto import (
        BackupExportRequest,
        BackupExportResult,
        BackupRestoreResult,
        BackupValidateRequest,
        CollisionPolicy,
        CombinedReportRequest,
        CombinedReportResult,
        CommitGenerationResult,
        CorrectionBatch,
        ImportResponseCommand,
        PermanentDeleteResult,
        RegradeCommand,
        ResponseBookRequest,
        RestoreCommand,
        SessionCreateResult,
        SessionMutationRequest,
        Settings,
        SettingsSaveCommand,
        SettingsSaveResult,
        SnapshotRequest,
        SoftDeleteResult,
        TrashRestoreResult,
    )
    from omr_grader.application.grading_presenter import (
        ConnectedSessionDisplay,
        GradingProgressDisplay,
    )
    from omr_grader.application.grading_use_case import GradingUseCase
    from omr_grader.application.profile_use_case import ProfileApplicationService
    from omr_grader.application.response_import_use_case import ResponseImportUseCase
    from omr_grader.application.settings_use_case import SettingsApplicationService, SettingsState
    from omr_grader.domain.enums import (
        AnswerStatus,
        ExamTerm,
        FieldStatus,
        SnapshotPurpose,
        TargetKind,
    )
    from omr_grader.domain.grading import CORRECT, INCORRECT, question_outcomes
    from omr_grader.domain.models import (
        AnswerKeySnapshot,
        AnswerValue,
        CorrectionDraft,
        IdCorrectionValue,
    )
    from omr_grader.infrastructure.config_store import config_revision
    from omr_grader.infrastructure.dashboard_repository import DashboardRepository
    from omr_grader.infrastructure.detail_repository import DetailRepository
    from omr_grader.infrastructure.grading_runtime import (
        CommittedGradingSnapshotReader,
        ResponseImportCommitCoordinator,
    )
    from omr_grader.infrastructure.profile_store import ProfileStore
    from omr_grader.infrastructure.scan_runtime import ScanRuntime, bind_scan_runtime
    from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
    from omr_grader.ui.app_controller import AppController, FreshResponseIntent, ServicePorts
    from omr_grader.ui.dashboard_model import DashboardSelection
    from omr_grader.ui.dashboard_page import DashboardRequest
    from omr_grader.ui.grading_page import GradingPage
    from omr_grader.ui.import_widgets import ImportKind, ImportSelection
    from omr_grader.ui.main_window import MainWindow
    from omr_grader.ui.scan_page import ScanPage
    from omr_grader.workbooks.answer_key import write_answer_key_sample
    from omr_grader.workbooks.roster_sample import write_roster_sample

    diagnostic: str | None

    if isinstance(outcome, Err):
        write_enabled = False
        diagnostic = _diagnostic_from_errors(outcome.errors)
        runtime_paths = None
        runtime_token = None
    else:
        write_enabled = outcome.value.write_enabled
        diagnostic = outcome.value.diagnostic
        runtime_paths = outcome.value.paths
        runtime_token = outcome.value.capability_token

    scan_page = ScanPage()
    grading_page = GradingPage()
    window = MainWindow(scan_page, grading_page)

    def select_source(kind: ImportKind) -> ImportSelection | None:
        if kind is ImportKind.FOLDER:
            path = QFileDialog.getExistingDirectory(window, "스캔 이미지 폴더 선택")
            return None if not path else ImportSelection(kind, (path,))
        filter_text = "PDF 파일 (*.pdf)" if kind is ImportKind.PDF else "Excel 파일 (*.xlsx *.xlsm)"
        path, _ = QFileDialog.getOpenFileName(window, "파일 선택", filter=filter_text)
        return None if not path else ImportSelection(kind, (path,))

    def select_profile() -> ImportSelection | None:
        path, _ = QFileDialog.getOpenFileName(
            window, "OMR 프로필 선택", filter="OMR 프로필 (*.omrtemplate)"
        )
        return None if not path else ImportSelection(ImportKind.PROFILE, (path,))

    def select_answer_key(_: object) -> tuple[str, str] | None:
        path, _ = QFileDialog.getOpenFileName(window, "정답표 선택", filter="Excel 파일 (*.xlsx)")
        return None if not path else (path, "정답표")

    def select_response(_: object) -> tuple[str, str] | None:
        path, _ = QFileDialog.getOpenFileName(
            window, "응답 엑셀 선택", filter="Excel 파일 (*.xlsx)"
        )
        return _canonical_response_workbook_selection(path)

    def select_fresh_response(_: object) -> tuple[str, str] | None:
        path, _ = QFileDialog.getOpenFileName(
            window, "새 응답 엑셀 선택", filter="Excel 파일 (*.xlsx)"
        )
        return _canonical_response_workbook_selection(path)

    def unavailable(code: str = "SERVICE_UNAVAILABLE") -> Err:
        return Err((ErrorInfo(code, f"error.{code.lower()}"),))

    def choose_collision() -> CollisionPolicy | None:
        choice = QMessageBox.question(
            window,
            "파일 충돌",
            "같은 이름의 파일이 있으면 바꾸시겠습니까?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Yes:
            return CollisionPolicy.REPLACE
        if choice == QMessageBox.StandardButton.No:
            return CollisionPolicy.ERROR
        return None

    profiles = (
        None
        if runtime_paths is None
        else ProfileApplicationService(
            ProfileStore(runtime_paths, runtime_token if write_enabled else None)
        )
    )
    settings_load = None
    settings_save = None
    profile_import = None
    dashboard_load = None
    dashboard_detail = None
    dashboard_delete = None
    dashboard_backup = None
    dashboard_restore = None
    dashboard_combined = None
    dashboard_trash = None
    dashboard_trash_load = None
    detail_load = None
    detail_preview = None
    detail_save = None
    detail_close = None
    backup_service = None
    store = None
    coordinator = None
    detail_repository = None
    correction_service: CorrectionApplicationService | None = None
    scan_service = None
    response_import_service = None
    grading_service = None
    grading_context = None
    answer_key_service = None
    import_response_selection = None
    import_fresh_response_selection = None
    sample_roster = None
    sample_answer_key = None
    result_navigation = None
    session_display: Callable[
        [SessionCreateResult | CommitGenerationResult], Result[ConnectedSessionDisplay]
    ] | None = None
    displays: dict[str, DetailPageDisplay] = {}

    if runtime_paths is not None:
        store = SessionStore(runtime_paths)
        coordinator = SessionCommitCoordinator(store)
        repository = DashboardRepository(
            runtime_paths.data_dir / "dashboard_index.json",
            store.discover_active_committed_leases,
        )
        dashboard_service = DashboardApplicationService(
            coordinator, repository, write_enabled=write_enabled
        )
        report_service = DashboardUseCase(coordinator, write_enabled=write_enabled)
        detail_repository = DetailRepository(coordinator)
        dashboard_load = dashboard_service.list_exams
        dashboard_trash_load = repository.list_trash

        def selected(selection: DashboardSelection) -> tuple[str, int] | Err:
            if len(selection.session_ids) != 1 or len(selection.revisions) != 1:
                return unavailable("DASHBOARD_SELECTION_INVALID")
            return selection.session_ids[0], selection.revisions[0]

        def open_detail(selection: DashboardSelection) -> Result[DetailPageDisplay]:
            identity = selected(selection)
            if isinstance(identity, Err):
                return identity
            session_id, revision = identity
            opened = detail_repository.open_detail(session_id, revision)
            if isinstance(opened, Err):
                return opened
            handle, rows = opened.value
            listed = dashboard_service.list_exams()
            if isinstance(listed, Err):
                detail_repository.close_detail(handle.handle_id)
                return listed
            entry = next(
                (
                    item
                    for item in listed.value.entries
                    if item.session_id == session_id and item.revision == revision
                ),
                None,
            )
            if entry is None:
                detail_repository.close_detail(handle.handle_id)
                return unavailable("DASHBOARD_SESSION_NOT_FOUND")
            students = tuple(
                DetailStudentDisplay(
                    row.work_item_id,
                    row.student_id or "",
                    row.student_name or "",
                    row.rank,
                    row.score or "",
                    (),
                )
                for row in rows
            )
            display = DetailPageDisplay(
                session_id,
                revision,
                entry.exam_name,
                DetailSummaryDisplay(
                    entry.participant_count,
                    entry.average_score or "",
                    entry.highest_score or "",
                    entry.lowest_score or "",
                ),
                students,
                handle.handle_id,
            )
            displays[handle.handle_id] = display
            return Ok(display)

        dashboard_detail = open_detail

        def load_detail(request: DetailLoadRequest) -> Result[DetailLoadResult]:
            if request.detail_handle is None:
                return unavailable("DETAIL_HANDLE_REQUIRED")
            if request.correlation_id is None:
                return unavailable("DETAIL_CORRELATION_REQUIRED")
            display = displays.get(request.detail_handle)
            if (
                display is None
                or display.session_id != request.session_id
                or display.revision != request.revision
            ):
                return unavailable("DETAIL_HANDLE_INVALID")
            loaded = detail_repository.load_work_item(request.detail_handle, request.work_item_id)
            if isinstance(loaded, Err):
                return loaded
            student = next(
                (item for item in display.students if item.work_item_id == request.work_item_id),
                None,
            )
            if student is None:
                return unavailable("DETAIL_WORK_ITEM_NOT_FOUND")
            payload = loaded.value.payload
            answers_value = payload.get("answers")
            answers: tuple[DetailAnswerDisplay, ...] = ()
            if isinstance(answers_value, list):
                try:
                    answers = tuple(
                        DetailAnswerDisplay(
                            value["question"], value.get("answer"), value.get("correct")
                        )
                        for value in answers_value
                        if isinstance(value, dict)
                    )
                except (TypeError, ValueError, KeyError):
                    return unavailable("DETAIL_MATERIALIZATION_INVALID")
            return Ok(
                DetailLoadResult(
                    request.correlation_id,
                    DetailStudentDisplay(
                        student.work_item_id,
                        student.student_id,
                        student.name,
                        student.rank,
                        student.score,
                        answers,
                        loaded.value.image,
                    ),
                )
            )

        detail_load = load_detail

        def close_detail(request: DetailPageRequest) -> Result[None]:
            if request.detail_handle is None:
                return Ok(None)
            warnings = (
                ()
                if correction_service is None
                else correction_service.close_pending(request.session_id, request.revision)
            )
            if correction_service is not None:
                correction_previews.pop(request.detail_handle, None)
            closed = detail_repository.close_detail(request.detail_handle)
            if isinstance(closed, Ok):
                displays.pop(request.detail_handle, None)
                return Ok(None, closed.warnings + warnings)
            return closed

        detail_close = close_detail

        if write_enabled:
            scan_runtime = ScanRuntime(ProfileStore(runtime_paths, runtime_token), store)
            scan_service = bind_scan_runtime(scan_runtime)
            response_import_service = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
            answer_key_service = AnswerKeyWorkbookUseCase()
            grading_service = GradingUseCase(
                CommittedGradingSnapshotReader(coordinator),
                answer_key_service,
                coordinator,
            )

            def grading_context(
                command: RegradeCommand,
                _cancelled: Event,
                emit_progress: Callable[[object], None],
            ) -> Result[CommitGenerationResult]:
                started = monotonic()

                def report(completed: int, total: int) -> None:
                    elapsed = max(0, int(monotonic() - started))
                    eta = (
                        None
                        if completed <= 0
                        else max(0, int(elapsed * (total - completed) / completed))
                    )
                    emit_progress(
                        GradingProgressDisplay(
                            completed,
                            total,
                            elapsed,
                            eta,
                            (
                                "결과 파일 저장 중"
                                if total > 0 and completed == total
                                else f"현재 처리 중: {completed} / 총 {total}명"
                            ),
                        )
                    )

                return grading_service.regrade(command, report)

            def display_committed_session(
                result: SessionCreateResult | CommitGenerationResult,
            ) -> Result[ConnectedSessionDisplay]:
                if not result.committed:
                    return unavailable("SESSION_NOT_COMMITTED")
                listed = dashboard_service.list_exams()
                if isinstance(listed, Err):
                    return listed
                entry = next(
                    (
                        item
                        for item in listed.value.entries
                        if item.session_id == result.session_id and item.revision == result.revision
                    ),
                    None,
                )
                if entry is None:
                    return unavailable("DASHBOARD_SESSION_NOT_FOUND")
                return Ok(
                    ConnectedSessionDisplay(
                        result.session_id,
                        result.revision,
                        entry.exam_name,
                        "세션 내 응답 결과",
                        result.revision > 1,
                    )
                )

            session_display = display_committed_session

            def import_response_book_as_new_session(
                selection: tuple[str, str], operation_id: str | None = None
            ) -> Result[ConnectedSessionDisplay]:
                source_path, sheet_name = selection
                exam_name = Path(source_path).stem or "응답 가져오기"
                validated = response_import_service.validate_response_book(
                    ResponseBookRequest(
                        source_path,
                        sheet_name,
                        exam_name,
                        None,
                        ExamTerm.UNSPECIFIED,
                    )
                )
                if isinstance(validated, Err):
                    return validated
                imported = response_import_service.import_response_book(
                    ImportResponseCommand(
                        validated.value.validation_token,
                        operation_id if operation_id is not None else f"import-{uuid4().hex}",
                        uuid4().hex,
                        0,
                    )
                )
                if isinstance(imported, Err):
                    return imported
                if not imported.value.committed or imported.value.revision != 1:
                    return unavailable("RESPONSE_IMPORT_NOT_COMMITTED")
                return Ok(
                    ConnectedSessionDisplay(
                        imported.value.session_id,
                        imported.value.revision,
                        exam_name,
                        source_path,
                    ),
                    imported.warnings,
                )

            def import_response_selection_for_grading(
                _request: object, selection: tuple[str, str]
            ) -> Result[ConnectedSessionDisplay]:
                return import_response_book_as_new_session(selection)

            def import_fresh_response_selection_for_scan(
                intent: object, selection: tuple[str, str]
            ) -> Result[ConnectedSessionDisplay]:
                if not isinstance(intent, FreshResponseIntent):
                    return unavailable("FRESH_RESPONSE_INTENT_INVALID")
                return import_response_book_as_new_session(selection, intent.operation_id)

            def download_roster_sample() -> None:
                destination, _ = QFileDialog.getSaveFileName(
                    window,
                    "응시 학생 명단 샘플 저장",
                    "roster-sample.xlsx",
                    "Excel 파일 (*.xlsx)",
                )
                if not destination:
                    return
                written = write_roster_sample(destination)
                if isinstance(written, Err):
                    error = written.errors[0]
                    window.show_diagnostic(
                        str(error.context.get("reason", f"명단 샘플 저장 실패: {error.code}"))
                    )
                    return
                window.set_status("응시 학생 명단 샘플을 저장했습니다.")

            def download_answer_key_sample(_: object) -> None:
                destination, _ = QFileDialog.getSaveFileName(
                    window,
                    "정답표 샘플 저장",
                    "answer-key-sample.xlsx",
                    "Excel 파일 (*.xlsx)",
                )
                if not destination:
                    return
                written = write_answer_key_sample(destination)
                if isinstance(written, Err):
                    error = written.errors[0]
                    window.show_diagnostic(
                        str(error.context.get("reason", get_message(error.message_key)))
                    )
                    return
                window.set_status("정답표 샘플을 저장했습니다.")

            def navigate_to_results(_: object) -> None:
                window.navigate_to(MainWindow.EXAM_PAGE)

            import_response_selection = import_response_selection_for_grading
            import_fresh_response_selection = import_fresh_response_selection_for_scan
            sample_answer_key = download_answer_key_sample
            sample_roster = download_roster_sample
            result_navigation = navigate_to_results
            backup_service = BackupApplicationService(
                coordinator, restore_publisher=store.restore_publisher()
            )

            def delete_dashboard(selection: DashboardSelection) -> Result[SoftDeleteResult]:
                identity = selected(selection)
                if isinstance(identity, Err):
                    return identity
                return dashboard_service.soft_delete(
                    SessionMutationRequest(identity[0], identity[1], uuid4().hex)
                )

            def backup_dashboard(
                selection: DashboardSelection,
            ) -> Result[BackupExportResult]:
                identity = selected(selection)
                if isinstance(identity, Err):
                    return identity
                destination, _ = QFileDialog.getSaveFileName(
                    window, "백업 파일 저장", filter="OMR 백업 (*.omrbak)"
                )
                collision = choose_collision() if destination else None
                if not destination or collision is None:
                    return unavailable("BACKUP_DESTINATION_REQUIRED")
                return backup_service.export_backup(
                    BackupExportRequest(
                        SnapshotRequest(identity[0], identity[1], SnapshotPurpose.BACKUP),
                        destination,
                        collision,
                        uuid4().hex,
                    )
                )

            def restore_dashboard() -> Result[BackupRestoreResult]:
                source, _ = QFileDialog.getOpenFileName(
                    window, "백업 파일 선택", filter="OMR 백업 (*.omrbak)"
                )
                if not source:
                    return unavailable("BACKUP_SOURCE_REQUIRED")
                validated = backup_service.validate_backup(BackupValidateRequest(source))
                if isinstance(validated, Err):
                    return validated
                return backup_service.restore_backup(
                    RestoreCommand(validated.value, str(store.root), uuid4().hex)
                )

            def combined_dashboard(
                selection: DashboardSelection,
            ) -> Result[CombinedReportResult]:
                if not selection.session_ids:
                    return unavailable("DASHBOARD_SELECTION_INVALID")
                destination, _ = QFileDialog.getSaveFileName(
                    window, "통합 성적표 저장", filter="Excel 파일 (*.xlsx)"
                )
                collision = choose_collision() if destination else None
                if not destination or collision is None:
                    return unavailable("REPORT_DESTINATION_REQUIRED")
                return report_service.build_combined_report(
                    CombinedReportRequest(
                        selection.session_ids, True, destination, collision, uuid4().hex
                    )
                )

            def trash_dashboard(
                request: DashboardRequest,
            ) -> Result[TrashRestoreResult | PermanentDeleteResult]:
                identity = selected(request.selection)
                if isinstance(identity, Err):
                    return identity
                command = SessionMutationRequest(identity[0], identity[1], uuid4().hex)
                if request.action == "trash_restore":
                    restored = dashboard_service.restore_from_trash(command)
                    if isinstance(restored, Err):
                        return restored
                    return Ok(restored.value, restored.warnings)
                if request.action == "trash_delete":
                    deleted = dashboard_service.permanently_delete(command)
                    if isinstance(deleted, Err):
                        return deleted
                    return Ok(deleted.value, deleted.warnings)
                return unavailable("DASHBOARD_ACTION_INVALID")

            dashboard_delete = delete_dashboard
            dashboard_backup = backup_dashboard
            dashboard_restore = restore_dashboard
            dashboard_combined = combined_dashboard
            dashboard_trash = trash_dashboard

            correction_service = CorrectionApplicationService(detail_repository, coordinator)
            correction_previews: dict[str, tuple[str, CorrectionPreview, AnswerKeySnapshot]] = {}

            def correction_batch(request: DetailPageRequest) -> CorrectionBatch | Err:
                display = displays.get(request.detail_handle or "")
                if (
                    display is None
                    or display.session_id != request.session_id
                    or display.revision != request.revision
                    or not request.edits
                ):
                    return unavailable("DETAIL_CORRECTION_REQUEST_INVALID")
                drafts: list[CorrectionDraft] = []
                for edit in request.edits:
                    if isinstance(edit, DetailAnswerEdit):
                        before = (
                            AnswerValue((), AnswerStatus.BLANK)
                            if edit.before is None
                            else AnswerValue((edit.before,), AnswerStatus.NORMAL)
                        )
                        after = (
                            AnswerValue((), AnswerStatus.BLANK)
                            if edit.after is None
                            else AnswerValue((edit.after,), AnswerStatus.NORMAL)
                        )
                        drafts.append(
                            CorrectionDraft(
                                edit.work_item_id,
                                TargetKind.ANSWER_CELL,
                                edit.question,
                                before,
                                after,
                                "detail_page",
                            )
                        )
                    elif isinstance(edit, DetailIdEdit):
                        before_id = (
                            IdCorrectionValue(None, FieldStatus.BLANK)
                            if edit.before is None
                            else IdCorrectionValue(str(edit.before), FieldStatus.NORMAL)
                        )
                        after_id = (
                            IdCorrectionValue(None, FieldStatus.BLANK)
                            if edit.after is None
                            else IdCorrectionValue(str(edit.after), FieldStatus.NORMAL)
                        )
                        drafts.append(
                            CorrectionDraft(
                                edit.work_item_id,
                                TargetKind.ID_CELL,
                                edit.position - 1,
                                before_id,
                                after_id,
                                "detail_page",
                            )
                        )
                    else:
                        return unavailable("DETAIL_CORRECTION_REQUEST_INVALID")
                fingerprint = "|".join(
                    f"{draft.work_item_id}:{draft.target_kind.value}:{draft.target_key}:"
                    f"{draft.before.to_dict()}:{draft.after.to_dict()}"
                    for draft in drafts
                )
                return CorrectionBatch(
                    request.session_id,
                    request.revision,
                    f"{request.session_id}:{request.revision}:{fingerprint}",
                    request.correlation_id or uuid4().hex,
                    tuple(drafts),
                )

            def correction_display(
                display: DetailPageDisplay,
                preview: CorrectionPreview,
                answer_key: AnswerKeySnapshot,
                revision: int,
            ) -> DetailPageDisplay:
                response_by_id = {response.work_item_id: response for response in preview.responses}
                score_by_id = {row.work_item_id: row for row in preview.scores.rows}

                def answer_display(response: object) -> tuple[DetailAnswerDisplay, ...]:
                    assert hasattr(response, "answers")
                    outcomes = question_outcomes(response, answer_key)
                    return tuple(
                        DetailAnswerDisplay(
                            question,
                            value.choices[0]
                            if value.status is AnswerStatus.NORMAL and len(value.choices) == 1
                            else None,
                            True if outcome == CORRECT else False if outcome == INCORRECT else None,
                        )
                        for question, (value, outcome) in enumerate(
                            zip(response.answers, outcomes, strict=True), 1
                        )
                    )

                def id_digits(student_id: str | None) -> tuple[int | None, ...]:
                    return (
                        tuple(int(digit) for digit in student_id)
                        if student_id is not None and len(student_id) == 8
                        else (None,) * 8
                    )

                students = tuple(
                    DetailStudentDisplay(
                        student.work_item_id,
                        response_by_id[student.work_item_id].student_id or "",
                        student.name,
                        score_by_id[student.work_item_id].rank,
                        ""
                        if score_by_id[student.work_item_id].score is None
                        else str(score_by_id[student.work_item_id].score),
                        answer_display(response_by_id[student.work_item_id]),
                        student.image_bytes,
                        student.cells,
                        id_digits(response_by_id[student.work_item_id].student_id),
                        None,
                    )
                    for student in display.students
                    if student.work_item_id in response_by_id
                    and student.work_item_id in score_by_id
                )
                statistics = preview.scores.statistics
                return DetailPageDisplay(
                    display.session_id,
                    revision,
                    display.exam_name,
                    DetailSummaryDisplay(
                        statistics.participant_count,
                        "" if statistics.average_score is None else str(statistics.average_score),
                        "" if statistics.highest_score is None else str(statistics.highest_score),
                        "" if statistics.lowest_score is None else str(statistics.lowest_score),
                    ),
                    students,
                    display.detail_handle,
                )

            def retire_preview(
                detail_handle: str, session_id: str, revision: int
            ) -> tuple[ErrorInfo, ...]:
                correction_previews.pop(detail_handle, None)
                return correction_service.close_pending(session_id, revision)

            def preview_with_key(
                batch: CorrectionBatch,
            ) -> Result[tuple[CorrectionPreview, AnswerKeySnapshot]]:
                preview = correction_service.preview_corrections(batch)
                if isinstance(preview, Err):
                    return preview
                snapshot = detail_repository.read_correction_snapshot(
                    batch.session_id, batch.expected_revision
                )
                if isinstance(snapshot, Err):
                    correction_service.close_pending(batch.session_id, batch.expected_revision)
                    return snapshot
                closed = snapshot.value.lease.close()
                if isinstance(closed, Err):
                    correction_service.close_pending(batch.session_id, batch.expected_revision)
                    return closed
                return Ok(
                    (preview.value, snapshot.value.answer_key), preview.warnings + closed.warnings
                )

            def preview_corrections(request: DetailPageRequest) -> Result[DetailPreviewResult]:
                detail_handle = request.detail_handle
                batch = correction_batch(request)
                if isinstance(batch, Err):
                    if detail_handle is not None:
                        retire_preview(detail_handle, request.session_id, request.revision)
                    return batch
                if detail_handle is None:
                    return unavailable("DETAIL_HANDLE_REQUIRED")
                retired = retire_preview(detail_handle, request.session_id, request.revision)
                preview = preview_with_key(batch)
                if isinstance(preview, Err):
                    return preview
                correction_previews[detail_handle] = (
                    batch.idempotency_key,
                    preview.value[0],
                    preview.value[1],
                )
                return Ok(
                    DetailPreviewResult(
                        request.correlation_id or batch.idempotency_key,
                        correction_display(
                            displays[detail_handle],
                            preview.value[0],
                            preview.value[1],
                            request.revision,
                        ),
                    ),
                    retired + preview.warnings,
                )

            def save_corrections(request: DetailPageRequest) -> Result[DetailSaveResult]:
                detail_handle = request.detail_handle
                batch = correction_batch(request)
                if isinstance(batch, Err):
                    if detail_handle is not None:
                        retire_preview(detail_handle, request.session_id, request.revision)
                    return batch
                if detail_handle is None:
                    return unavailable("DETAIL_HANDLE_REQUIRED")
                cached = correction_previews.get(detail_handle)
                if cached is None or cached[0] != batch.idempotency_key:
                    retired = retire_preview(detail_handle, request.session_id, request.revision)
                    preview = preview_with_key(batch)
                    if isinstance(preview, Err):
                        return preview
                    preview_value, answer_key = preview.value
                else:
                    retired = ()
                    _, preview_value, answer_key = cached
                saved = correction_service.save_corrections(batch)
                correction_previews.pop(detail_handle, None)
                retired += correction_service.close_pending(request.session_id, request.revision)
                if isinstance(saved, Err):
                    return saved
                old_display = displays.pop(detail_handle)
                new_display = correction_display(
                    old_display, preview_value, answer_key, saved.value.revision
                )
                warnings = saved.warnings + retired
                closed = detail_repository.close_detail(detail_handle)
                if isinstance(closed, Ok):
                    warnings += closed.warnings
                else:
                    warnings += tuple(_warning_copy(issue) for issue in closed.errors)
                opened = detail_repository.open_detail(request.session_id, saved.value.revision)
                if isinstance(opened, Err):
                    return Ok(
                        DetailSaveResult(
                            request.correlation_id or batch.idempotency_key,
                            new_display,
                        ),
                        warnings + tuple(_warning_copy(issue) for issue in opened.errors),
                    )
                new_handle, _ = opened.value
                new_display = DetailPageDisplay(
                    new_display.session_id,
                    new_display.revision,
                    new_display.exam_name,
                    new_display.summary,
                    new_display.students,
                    new_handle.handle_id,
                )
                displays[new_handle.handle_id] = new_display
                return Ok(
                    DetailSaveResult(request.correlation_id or batch.idempotency_key, new_display),
                    warnings + opened.warnings,
                )

            detail_preview = preview_corrections
            detail_save = save_corrections
            token = runtime_token
            if token is not None and profiles is not None:
                settings = SettingsApplicationService(runtime_paths, token, profiles)
                settings_load = settings.load_settings

                def save_settings(command: SettingsSaveCommand) -> Result[SettingsSaveResult]:
                    return settings.save_settings(command)

                settings_save = save_settings
                profile_import = profiles.import_profile
        else:
            dashboard_delete = None
            dashboard_backup = None
            dashboard_restore = None
            dashboard_combined = None
            dashboard_trash = None
            detail_preview = None
            detail_save = None
            settings_save = None
            profile_import = None
            if isinstance(outcome, Ok) and outcome.value.config is not None:

                def load_readonly_settings() -> Result[SettingsState]:
                    config = outcome.value.config
                    if config is None:
                        return unavailable("SETTINGS_UNAVAILABLE")
                    return Ok(
                        SettingsState(
                            Settings(
                                config.default_profile,
                                config.default_sensitivity,
                                config.use_multiprocessing,
                            ),
                            config_revision(config),
                        )
                    )

                settings_load = load_readonly_settings

    services = ServicePorts(
        scan=scan_service,
        response_import=response_import_service,
        grading=grading_service,
        grading_context=grading_context,
        answer_key=answer_key_service,
        import_response_selection=import_response_selection,
        fresh_response_picker=select_fresh_response,
        import_fresh_response_selection=import_fresh_response_selection,
        sample_roster=sample_roster,
        sample_answer_key=sample_answer_key,
        result_navigation=result_navigation,
        session_display=session_display,
        profile_picker=select_profile,
        source_picker=select_source,
        roster_picker=select_source,
        answer_key_picker=select_answer_key,
        other_response_picker=select_response,
        settings_load=settings_load,
        settings_save=settings_save,
        profile_catalog=None if profiles is None else profiles.profile_catalog,
        profile_import=profile_import,
        dashboard_load=dashboard_load,
        dashboard_detail=dashboard_detail,
        dashboard_delete=dashboard_delete,
        dashboard_backup=dashboard_backup,
        dashboard_restore=dashboard_restore,
        dashboard_combined=dashboard_combined,
        dashboard_trash=dashboard_trash,
        dashboard_trash_load=dashboard_trash_load,
        detail_load=detail_load,
        detail_preview=detail_preview,
        detail_save=detail_save,
        detail_close=detail_close,
    )
    if isinstance(outcome, Ok):
        window.set_diagnostic_log_path(str(daily_log_path(outcome.value.paths.root)))
    controller = AppController(
        window,
        scan_page,
        grading_page,
        services,
        write_enabled=write_enabled,
        diagnostic=diagnostic,
        parent=window,
    )
    if isinstance(outcome, Ok):
        for warning in outcome.warnings:
            window.show_diagnostic(
                str(warning.context.get("reason", get_message(warning.message_key)))
            )
    # Python ownership is explicit: native Qt references alone are insufficient.
    _RUNTIME_REFERENCES = (
        window,
        controller,
        store,
        coordinator,
        backup_service,
        detail_repository,
        correction_service,
        displays,
    )
    window.show()
    if splash is not None:
        splash.finish(window)
    return app.exec()


__all__ = ["BootstrapState", "bootstrap", "run"]
