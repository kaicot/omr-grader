from __future__ import annotations

from threading import Event

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QThread
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QDialog

from omr_grader.application.dto import ProfileImportResult, Settings
from omr_grader.application.settings_use_case import SettingsState
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.infrastructure.dashboard_repository import DashboardListing
from omr_grader.infrastructure.form_detection import FormDetection
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.form_confirm_dialog import FormConfirmDialog
from omr_grader.ui.import_widgets import ImportKind, ImportSelection
from omr_grader.ui.main_window import MainWindow
from omr_grader.ui.scan_page import ScanPage, ValidatedProfileState

PDF = ImportSelection(ImportKind.PDF, ("C:/input/scans.pdf",))
SUMMARY = "학번 8자리 · 객관식 100문항 (1~20, 21~40, 41~60, 61~80, 81~100)"
DETECTING = "답안지 양식을 자동으로 확인하는 중입니다…"
SAVED = "자동양식_객관식100문항_a1b2c3.omrtemplate"
STORED = "자동양식_객관식100문항_a1b2c3_2.omrtemplate"
DECLINED = "자동 인식한 양식을 사용하지 않았습니다. OMR 프로필을 직접 선택하세요."


def _png() -> bytes:
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _detection(
    *,
    profile_filename: str | None = None,
    generated_profile: bytes | None = b'{"profile_name": "generated"}',
    pages_matching: int = 3,
    dropped_header_rows: int = 0,
    unmarked_first_rows: int = 0,
) -> FormDetection:
    return FormDetection(
        profile_filename,
        generated_profile,
        SAVED,
        8,
        100,
        ((1, 20), (21, 20), (41, 20), (61, 20), (81, 20)),
        3,
        pages_matching,
        _png(),
        dropped_header_rows,
        unmarked_first_rows,
    )


def _known_detection(filename: str) -> FormDetection:
    return _detection(profile_filename=filename, generated_profile=None)


def _profile(filename: str) -> ValidatedProfileState:
    return ValidatedProfileState(
        name=filename.removesuffix(".omrtemplate"),
        path=filename,
        dimensions=(1682, 1190),
        grid_summary="6개 영역",
        validated=True,
    )


class _Catalog:
    """What the Profiles folder lists; refreshing publishes it to the scan page."""

    def __init__(self, scan: ScanPage, *filenames: str) -> None:
        self.scan = scan
        self.profiles = [_profile(filename) for filename in filenames]

    def add(self, filename: str) -> None:
        self.profiles.append(_profile(filename))

    def refresh(self) -> tuple[()]:
        self.scan.set_profiles(tuple(self.profiles))
        return ()


class _AnsweringDialog(QDialog):
    """Answers at once instead of running a modal loop."""

    def __init__(self, outcome: QDialog.DialogCode, parent=None) -> None:
        super().__init__(parent)
        self._outcome = outcome

    def exec(self) -> int:
        return int(self._outcome)


class _DialogFactory:
    def __init__(self, outcome: QDialog.DialogCode) -> None:
        self.outcome = outcome
        self.calls: list[tuple[bytes, str, str | None, object]] = []

    def __call__(self, preview_png, summary, warning, parent) -> QDialog:
        self.calls.append((preview_png, summary, warning, parent))
        return _AnsweringDialog(self.outcome, parent)


class _Setup:
    def __init__(self, window: MainWindow, controller: AppController, catalog: _Catalog) -> None:
        self.window = window
        self.scan = window.scan_page
        self.controller = controller
        self.catalog = catalog

    def choose_source(self) -> None:
        self.scan.set_source(PDF)

    def finish(self, qtbot) -> None:
        qtbot.waitUntil(lambda: self.controller._active_bridge is None)


def _setup(qtbot, monkeypatch, *catalog: str, write_enabled: bool = True, **ports) -> _Setup:
    window = MainWindow()
    qtbot.addWidget(window)
    services = ServicePorts(
        settings_load=lambda: Ok(SettingsState(Settings("", 3, False), 1)), **ports
    )
    controller = AppController(
        window, window.scan_page, window.grading_page, services, write_enabled=write_enabled
    )
    published = _Catalog(window.scan_page, *catalog)
    monkeypatch.setattr(controller, "_refresh_profile_catalog", published.refresh)
    published.refresh()
    return _Setup(window, controller, published)


def test_form_ports_default_to_unavailable() -> None:
    ports = ServicePorts()

    assert ports.form_detect is None
    assert ports.form_save is None


def test_known_form_selects_the_saved_profile_off_the_ui_thread(qtbot, monkeypatch) -> None:
    threads: list[QThread] = []
    paths: list[tuple[str, ...]] = []

    def detect(selected: tuple[str, ...]) -> Ok[FormDetection]:
        threads.append(QThread.currentThread())
        paths.append(selected)
        return Ok(_known_detection("saved.omrtemplate"))

    setup = _setup(qtbot, monkeypatch, "other.omrtemplate", form_detect=detect)
    # The saved form reached the Profiles folder after the page last listed it.
    setup.catalog.add("saved.omrtemplate")
    setup.scan.exam_name_edit.setText("26-2 생리학 중간고사")
    setup.scan.profile_combo.setCurrentIndex(1)
    settings_status = setup.window.settings_page.status_label.text()

    setup.choose_source()
    assert setup.scan.form_status_label.text() == DETECTING
    assert not setup.scan.run_button.isEnabled()
    setup.finish(qtbot)

    assert paths == [("C:/input/scans.pdf",)]
    assert threads and threads[0] is not QCoreApplication.instance().thread()
    assert setup.scan.profile_combo.currentData().path == "saved.omrtemplate"
    assert setup.scan.form_status_label.text() == (
        f"자동 인식: {SUMMARY} · 저장된 양식 'saved.omrtemplate' 사용"
    )
    assert setup.scan.form_status_label.property("role") == "success"
    assert setup.scan.run_button.isEnabled()
    assert setup.window.status_label.text() == "준비됨"
    # Detection must not borrow the settings page's "saving" busy text.
    assert setup.window.settings_page.status_label.text() == settings_status
    setup.controller.close()


def test_a_reused_form_says_when_some_checked_pages_look_different(qtbot, monkeypatch) -> None:
    detection = _detection(
        profile_filename="saved.omrtemplate", generated_profile=None, pages_matching=2
    )
    setup = _setup(qtbot, monkeypatch, "saved.omrtemplate", form_detect=lambda paths: Ok(detection))

    setup.choose_source()
    setup.finish(qtbot)

    assert setup.scan.profile_combo.currentData().path == "saved.omrtemplate"
    assert setup.scan.form_status_label.text() == (
        f"자동 인식: {SUMMARY} · 저장된 양식 'saved.omrtemplate' 사용 · "
        "확인한 3쪽 가운데 1쪽은 양식이 달라 보입니다. 다른 양식의 답안지가 섞여 있는지 확인하세요."
    )
    setup.controller.close()


def test_a_reused_form_notes_first_rows_nobody_marked(qtbot, monkeypatch) -> None:
    detection = _detection(
        profile_filename="saved.omrtemplate", generated_profile=None, unmarked_first_rows=1
    )
    setup = _setup(qtbot, monkeypatch, "saved.omrtemplate", form_detect=lambda paths: Ok(detection))

    setup.choose_source()
    setup.finish(qtbot)

    assert setup.scan.form_status_label.text() == (
        f"자동 인식: {SUMMARY} · 저장된 양식 'saved.omrtemplate' 사용 · "
        "이번 답안지들은 문항 블록 1곳의 맨 윗줄을 아무도 칠하지 않았습니다. 저장된 양식이 머리글 줄을 문항으로 세고 있지 않은지 문항 범위를 확인하세요."
    )
    setup.controller.close()


def test_new_form_is_saved_selected_and_reported_after_confirmation(qtbot, monkeypatch) -> None:
    detection = _detection()
    saves: list[tuple[bytes, str]] = []
    factory = _DialogFactory(QDialog.DialogCode.Accepted)

    def save(payload: bytes, filename: str) -> Ok[ProfileImportResult]:
        saves.append((payload, filename))
        setup.catalog.add(STORED)
        return Ok(ProfileImportResult(STORED, "a" * 64))

    setup = _setup(
        qtbot,
        monkeypatch,
        "other.omrtemplate",
        form_detect=lambda paths: Ok(detection),
        form_save=save,
    )
    setup.controller._form_confirm_factory = factory

    setup.choose_source()
    setup.finish(qtbot)

    assert factory.calls == [(detection.preview_png, SUMMARY, None, setup.window)]
    assert saves == [(detection.generated_profile, detection.suggested_filename)]
    assert setup.scan.profile_combo.currentData().path == STORED
    assert (
        setup.scan.form_status_label.text() == f"새 양식으로 저장했습니다: {SUMMARY} · '{STORED}'"
    )
    assert setup.scan.form_status_label.property("role") == "success"
    assert setup.window.status_label.text() == "준비됨"
    setup.controller.close()


@pytest.mark.parametrize(
    ("matching", "dropped", "warning"),
    (
        (3, 0, None),
        (
            2,
            0,
            "확인한 3쪽 가운데 1쪽은 양식이 달라 보입니다. 다른 양식의 답안지가 섞여 있는지 확인하세요.",
        ),
        (
            3,
            2,
            "문항 블록 2곳의 맨 윗줄은 어느 답안지에서도 칠해지지 않아 머리글로 보고 문항에서 뺐습니다. 1번이 빠지지 않았는지 문항 범위를 확인하세요.",
        ),
        (
            2,
            2,
            "문항 블록 2곳의 맨 윗줄은 어느 답안지에서도 칠해지지 않아 머리글로 보고 문항에서 뺐습니다. 1번이 빠지지 않았는지 문항 범위를 확인하세요. 확인한 3쪽 가운데 1쪽은 양식이 달라 보입니다. 다른 양식의 답안지가 섞여 있는지 확인하세요.",
        ),
    ),
)
def test_confirmation_uses_the_real_dialog_and_warns_about_mixed_pages(
    qtbot, monkeypatch, matching, dropped, warning
) -> None:
    seen: dict[str, object] = {}

    def answer(self: FormConfirmDialog) -> int:
        seen.update(
            summary=self.summary_label.text(),
            warning=self.warning_label.text(),
            warning_hidden=self.warning_label.isHidden(),
            images=self.preview_view.active_image_count,
            parent=self.parentWidget(),
        )
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(FormConfirmDialog, "exec", answer)
    setup = _setup(
        qtbot,
        monkeypatch,
        form_detect=lambda paths: Ok(
            _detection(pages_matching=matching, dropped_header_rows=dropped)
        ),
    )
    assert setup.controller._form_confirm_factory is FormConfirmDialog

    setup.choose_source()
    setup.finish(qtbot)

    assert seen == {
        "summary": SUMMARY,
        "warning": warning or "",
        "warning_hidden": warning is None,
        "images": 1,
        "parent": setup.window,
    }
    setup.controller.close()


def test_declined_new_form_saves_nothing_and_leaves_the_manual_choice(qtbot, monkeypatch) -> None:
    saves: list[object] = []
    setup = _setup(
        qtbot,
        monkeypatch,
        "manual.omrtemplate",
        form_detect=lambda paths: Ok(_detection()),
        form_save=lambda payload, filename: saves.append((payload, filename)),
    )
    setup.controller._form_confirm_factory = _DialogFactory(QDialog.DialogCode.Rejected)
    setup.scan.exam_name_edit.setText("26-2 생리학 중간고사")

    setup.choose_source()
    setup.finish(qtbot)

    assert saves == []
    assert setup.scan.form_status_label.text() == DECLINED
    assert setup.scan.form_status_label.property("role") == "error"
    assert setup.scan.profile_combo.currentData() is None
    assert not setup.scan.run_button.isEnabled()
    # The combo box stays a working fallback.
    setup.scan.profile_combo.setCurrentIndex(1)
    assert setup.scan.profile_combo.isEnabled()
    assert setup.scan.run_button.isEnabled()
    setup.controller.close()


@pytest.mark.parametrize(
    ("code", "message"),
    (
        ("FORM_NOT_FOUND", "답안지에서 OMR 양식을 찾지 못했습니다."),
        ("FORM_ID_UNSUPPORTED", "학번 칸은 8자리(0~9)만 지원합니다."),
        ("FORM_ANSWERS_UNSUPPORTED", "문항은 5지선다만 지원합니다."),
        ("FORM_QUESTIONS_UNSUPPORTED", "문항 수는 1~100개만 지원합니다."),
        ("FORM_GEOMETRY_INVALID", "답안지 양식의 칸 배치를 해석하지 못했습니다."),
        ("INVALID_BUBBLE_RADIUS", "버블 크기를 확인하지 못했습니다."),
        ("SCAN_SOURCE_EMPTY", "선택한 스캔 파일에서 읽을 수 있는 답안지 쪽이 없습니다."),
    ),
)
def test_detection_error_is_reported_beside_the_profile_choice_not_as_a_failed_task(
    qtbot, monkeypatch, code, message
) -> None:
    error = ErrorInfo(code, f"error.{code.lower()}", None, {"reason": "internal detail"})
    shown: list[str] = []
    presented: list[object] = []
    saves: list[object] = []
    factory = _DialogFactory(QDialog.DialogCode.Accepted)
    setup = _setup(
        qtbot,
        monkeypatch,
        form_detect=lambda paths: Err((error,)),
        form_save=lambda payload, filename: saves.append(filename),
    )
    setup.controller._form_confirm_factory = factory
    monkeypatch.setattr(setup.window, "show_diagnostic", shown.append)
    monkeypatch.setattr(setup.controller, "_present_error", lambda *args: presented.append(args))

    setup.choose_source()
    setup.finish(qtbot)

    assert setup.scan.form_status_label.text() == message
    assert setup.scan.form_status_label.property("role") == "error"
    assert "오류 코드" not in setup.scan.progress_label.text()
    assert shown == []
    assert presented == []
    assert factory.calls == []
    assert saves == []
    assert setup.scan.profile_combo.currentData() is None
    assert setup.window.status_label.text() == "실패"
    assert setup.window.status_label.property("role") != "error"
    setup.controller.close()


def test_detection_exception_is_reported_beside_the_profile_choice(qtbot, monkeypatch) -> None:
    def detect(paths: tuple[str, ...]) -> Ok[FormDetection]:
        raise RuntimeError("broken")

    setup = _setup(qtbot, monkeypatch, form_detect=detect)

    setup.choose_source()
    setup.finish(qtbot)

    assert setup.scan.form_status_label.text() == "broken"
    assert setup.scan.form_status_label.property("role") == "error"
    assert "오류 코드" not in setup.scan.progress_label.text()
    setup.controller.close()


def test_without_a_detection_port_choosing_a_source_does_nothing(qtbot, monkeypatch) -> None:
    setup = _setup(qtbot, monkeypatch, "manual.omrtemplate")

    setup.choose_source()

    assert setup.controller._active_bridge is None
    assert setup.scan.form_status_label.isHidden()
    assert setup.scan.form_status_label.text() == ""
    assert setup.scan.profile_combo.currentData() is None
    setup.controller.close()


def test_detection_waits_for_another_operation_and_then_runs_for_the_scans(
    qtbot, monkeypatch
) -> None:
    detected: list[tuple[str, ...]] = []
    started, release = Event(), Event()

    def detect(paths: tuple[str, ...]) -> Ok[FormDetection]:
        detected.append(paths)
        return Ok(_known_detection("saved.omrtemplate"))

    def blocker() -> Ok[DashboardListing]:
        started.set()
        release.wait(5)
        return Ok(DashboardListing(()))

    setup = _setup(qtbot, monkeypatch, "saved.omrtemplate", form_detect=detect)
    setup.controller._start_desktop_action(
        setup.window.dashboard_page,
        blocker,
        lambda result: None,
        setup.window.dashboard_page.set_busy,
    )
    assert started.wait(2)

    setup.choose_source()
    # No result for earlier scans may stay up while the other operation runs.
    assert setup.scan.form_status_label.text() == DETECTING
    assert detected == []
    release.set()
    qtbot.waitUntil(lambda: detected == [PDF.paths], timeout=5000)
    setup.finish(qtbot)

    assert setup.scan.profile_combo.currentData().path == "saved.omrtemplate"
    assert setup.scan.form_status_label.text() == (
        f"자동 인식: {SUMMARY} · 저장된 양식 'saved.omrtemplate' 사용"
    )
    setup.controller.close()


def test_a_profile_picked_by_hand_while_detection_runs_is_kept(qtbot, monkeypatch) -> None:
    release = Event()
    saves: list[object] = []

    def detect(paths: tuple[str, ...]) -> Ok[FormDetection]:
        release.wait(5)
        return Ok(_detection())  # a new form would normally be offered and saved

    setup = _setup(
        qtbot,
        monkeypatch,
        "manual.omrtemplate",
        form_detect=detect,
        form_save=lambda payload, name: saves.append(name),
    )
    setup.choose_source()
    combo = setup.scan.profile_combo
    index = next(
        i
        for i in range(combo.count())
        if getattr(combo.itemData(i), "path", None) == "manual.omrtemplate"
    )
    combo.setCurrentIndex(index)
    combo.activated.emit(index)  # what a click on the list item sends
    release.set()
    setup.finish(qtbot)

    assert combo.currentData().path == "manual.omrtemplate"
    assert saves == []
    assert setup.scan.form_status_label.text() == (
        f"자동 인식: {SUMMARY} · 직접 고른 프로필을 그대로 사용합니다"
    )
    setup.controller.close()


def test_detection_is_skipped_while_the_controller_is_closing(qtbot, monkeypatch) -> None:
    detected: list[tuple[str, ...]] = []
    setup = _setup(
        qtbot,
        monkeypatch,
        form_detect=lambda paths: detected.append(paths) or Ok(_detection()),
    )
    setup.controller._closing = True

    setup.choose_source()

    assert detected == []
    assert setup.controller._active_bridge is None
    assert setup.scan.form_status_label.isHidden()
    setup.controller._closing = False
    setup.controller.close()


def test_accepted_new_form_is_not_saved_without_write_authority(qtbot, monkeypatch) -> None:
    saves: list[object] = []
    setup = _setup(
        qtbot,
        monkeypatch,
        write_enabled=False,
        form_detect=lambda paths: Ok(_detection()),
        form_save=lambda payload, filename: saves.append(filename),
    )
    setup.controller._form_confirm_factory = _DialogFactory(QDialog.DialogCode.Accepted)

    setup.choose_source()
    setup.finish(qtbot)

    assert saves == []
    assert "오류 코드: ROOT_WRITE_DENIED" in setup.scan.progress_label.text()
    assert setup.scan.form_status_label.text() == "실행 폴더에 쓸 권한이 없습니다."
    assert setup.scan.form_status_label.property("role") == "error"
    assert setup.scan.profile_combo.currentData() is None
    setup.controller.close()


def test_failed_save_is_presented_and_selects_no_profile(qtbot, monkeypatch) -> None:
    collision = ErrorInfo(
        "PROFILE_COLLISION",
        "error.profile_collision",
        None,
        {"reason": "같은 이름의 자동 양식 프로필이 너무 많습니다."},
    )
    setup = _setup(
        qtbot,
        monkeypatch,
        form_detect=lambda paths: Ok(_detection()),
        form_save=lambda payload, filename: Err((collision,)),
    )
    setup.controller._form_confirm_factory = _DialogFactory(QDialog.DialogCode.Accepted)

    setup.choose_source()
    setup.finish(qtbot)

    assert "오류 코드: PROFILE_COLLISION" in setup.scan.progress_label.text()
    assert setup.scan.form_status_label.text() == "같은 이름의 자동 양식 프로필이 너무 많습니다."
    assert setup.scan.form_status_label.property("role") == "error"
    assert setup.scan.profile_combo.currentData() is None
    assert setup.window.status_label.text() == "실패"
    setup.controller.close()


@pytest.mark.parametrize(
    ("save", "generated", "message"),
    (
        (None, b"{}", "현재 작업 서비스를 사용할 수 없습니다."),
        (lambda payload, filename: Ok("not a result"), b"{}", "올바르지 않은 응답"),
        (
            lambda payload, filename: Ok(ProfileImportResult(STORED, "a" * 64)),
            None,
            "올바르지 않은 응답",
        ),
    ),
    ids=("no-save-port", "malformed-save-result", "missing-generated-profile"),
)
def test_unusable_save_outcomes_fail_closed(qtbot, monkeypatch, save, generated, message) -> None:
    setup = _setup(
        qtbot,
        monkeypatch,
        form_detect=lambda paths: Ok(_detection(generated_profile=generated)),
        form_save=save,
    )
    setup.controller._form_confirm_factory = _DialogFactory(QDialog.DialogCode.Accepted)

    setup.choose_source()
    setup.finish(qtbot)

    assert message in setup.scan.form_status_label.text()
    assert setup.scan.form_status_label.property("role") == "error"
    assert setup.scan.profile_combo.currentData() is None
    setup.controller.close()


def test_saved_profile_missing_from_the_list_is_reported(qtbot, monkeypatch) -> None:
    setup = _setup(qtbot, monkeypatch, "other.omrtemplate")

    setup.controller._finish_form_detection(_known_detection("missing.omrtemplate"))

    assert setup.scan.form_status_label.text() == (
        "저장된 양식을 목록에서 찾을 수 없습니다. OMR 프로필을 직접 선택하세요."
    )
    assert setup.scan.form_status_label.property("role") == "error"
    setup.controller.close()


def test_saved_new_form_missing_from_the_list_is_reported(qtbot, monkeypatch) -> None:
    setup = _setup(
        qtbot,
        monkeypatch,
        form_save=lambda payload, filename: Ok(ProfileImportResult(STORED, "a" * 64)),
    )
    setup.controller._form_confirm_factory = _DialogFactory(QDialog.DialogCode.Accepted)

    setup.controller._finish_form_detection(_detection())

    assert setup.scan.form_status_label.text() == (
        "저장한 양식을 목록에서 찾을 수 없습니다. OMR 프로필을 직접 선택하세요."
    )
    setup.controller.close()


def test_unexpected_detection_result_is_a_failure(qtbot, monkeypatch) -> None:
    setup = _setup(qtbot, monkeypatch)
    setup.scan.set_form_detecting()

    setup.controller._finish_form_detection(object())

    assert "올바르지 않은 응답" in setup.scan.form_status_label.text()
    assert setup.scan.form_status_label.property("role") == "error"
    setup.controller.close()


def test_detection_of_replaced_scans_is_dropped_and_rerun_for_the_new_scans(
    qtbot, monkeypatch
) -> None:
    other = ImportSelection(ImportKind.PDF, ("C:/input/other.pdf",))
    release = Event()
    calls: list[tuple[str, ...]] = []

    def detect(paths: tuple[str, ...]):
        calls.append(tuple(paths))
        if len(calls) == 1:
            release.wait(5)
            return Ok(_known_detection("first.omrtemplate"))
        return Ok(_known_detection("second.omrtemplate"))

    setup = _setup(
        qtbot, monkeypatch, "first.omrtemplate", "second.omrtemplate", form_detect=detect
    )
    setup.choose_source()
    qtbot.waitUntil(lambda: len(calls) == 1)
    setup.scan.set_source(other)  # chosen while the first detection is still running
    release.set()
    qtbot.waitUntil(lambda: len(calls) == 2, timeout=5000)
    setup.finish(qtbot)

    assert calls == [PDF.paths, other.paths]
    assert "second.omrtemplate" in setup.scan.form_status_label.text()
    assert "first.omrtemplate" not in setup.scan.form_status_label.text()
