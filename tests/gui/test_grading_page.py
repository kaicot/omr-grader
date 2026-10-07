from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from omr_grader.application.grading_presenter import (
    AnswerKeyValidationDisplay,
    ConnectedSessionDisplay,
    GradingProgressDisplay,
)
from omr_grader.ui import grading_page as grading_page_module
from omr_grader.ui.grading_page import GradingPage

SESSION = ConnectedSessionDisplay("session-1", 3, "26-2 생리학 중간고사", "C:/응답결과.xlsx")


class _Clock:
    """A monotonic clock the test advances by hand instead of sleeping."""

    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(grading_page_module, "monotonic", clock)
    return clock


def _valid_key() -> AnswerKeyValidationDisplay:
    return AnswerKeyValidationDisplay(
        "C:/정답표_생리학.xlsx", "정답표", "정답표_생리학.xlsx", 30, 70, "30", ()
    )


def _ready_page(qtbot) -> GradingPage:
    page = GradingPage()
    qtbot.addWidget(page)
    page.set_connected_session(SESSION)
    page.set_operation_id("grade-1")
    page.set_answer_key_selection("C:/정답표_생리학.xlsx", "정답표")
    page.set_validation_result(_valid_key())
    page.show()
    return page


def test_grade_request_carries_only_immutable_session_revision_key_and_operation_values(
    qtbot,
) -> None:
    page = _ready_page(qtbot)
    with qtbot.waitSignal(page.grade_requested) as signal:
        QTest.mouseClick(page.grade_button, Qt.MouseButton.LeftButton)
    request = signal.args[0]
    assert request.session_id == "session-1"
    assert request.revision == 3
    assert request.response_path == "C:/응답결과.xlsx"
    assert request.answer_key_path == "C:/정답표_생리학.xlsx"
    assert request.answer_key_sheet == "정답표"
    assert request.operation_id == "grade-1"
    assert request.intent == "grade"
    assert not request.is_regrade


def test_regrade_request_emits_immutable_request_with_regrade_flag(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_connected_session(
        ConnectedSessionDisplay("session-1", 3, "26-2 생리학 중간고사", "C:/응답결과.xlsx", True)
    )

    with qtbot.waitSignal(page.grade_requested) as signal:
        QTest.mouseClick(page.grade_button, Qt.MouseButton.LeftButton)

    request = signal.args[0]
    assert request.intent == "grade"
    assert request.is_regrade is True
    assert request.session_id == "session-1"
    assert request.revision == 3
    assert request.response_path == "C:/응답결과.xlsx"
    assert request.answer_key_path == "C:/정답표_생리학.xlsx"
    assert request.operation_id == "grade-1"


def test_mutating_actions_require_write_access_idle_session_and_operation_id(qtbot) -> None:
    page = GradingPage()
    qtbot.addWidget(page)
    page.show()
    assert not page.grade_button.isEnabled()
    assert not page.upload_button.isEnabled()
    page.set_connected_session(SESSION)
    page.set_operation_id("grade-1")
    assert page.upload_button.isEnabled()
    page.set_write_enabled(False)
    assert not page.upload_button.isEnabled()
    assert not page.sample_button.isEnabled()


def test_browse_and_drop_requests_preserve_selection_for_validation_and_retry(qtbot) -> None:
    page = _ready_page(qtbot)
    assert page.answer_key_drop_widget.acceptDrops()
    with qtbot.waitSignal(page.answer_key_browse_requested) as browse:
        QTest.mouseClick(page.upload_button, Qt.MouseButton.LeftButton)
    assert browse.args[0].intent == "answer_key_browse"
    with qtbot.waitSignal(page.answer_key_dropped) as drop:
        assert page.answer_key_drop_widget.set_selection(("C:/새_정답표.xlsx",))
    assert drop.args[0].answer_key_path == "C:/새_정답표.xlsx"
    assert drop.args[0].answer_key_sheet == "정답표"
    page.set_error("Sheet1: 정답은 1~5만 입력할 수 있습니다.")
    assert "정답은" in page.error_label.text()
    assert not page.grade_button.isEnabled()
    page.set_validation_result(
        AnswerKeyValidationDisplay(
            "C:/새_정답표.xlsx", "Sheet1", "새_정답표.xlsx", 30, 70, "30", ()
        )
    )
    assert page.grade_button.isEnabled()


def test_validation_rule_errors_are_displayed_and_block_grading(qtbot) -> None:
    page = _ready_page(qtbot)
    errors = (
        "2번 문항: 문항번호가 중복되었습니다.",
        "3번 문항: 정답은 1~5만 입력할 수 있습니다.",
        "4번 문항: 복수정답은 AND로 입력해야 합니다.",
        "5번 문항: 전체 정답은 ALL로 입력해야 합니다.",
        "6번 문항: 미출제 문항은 UNASKED로 입력해야 합니다.",
        "7번 문항: 배점은 0보다 큰 숫자여야 합니다.",
    )
    page.set_validation_result(
        AnswerKeyValidationDisplay(
            "C:/정답표_생리학.xlsx", "정답표", "정답표_생리학.xlsx", 0, 1, "0", errors
        )
    )

    assert page.error_label.text() == "\n".join(f"• {error}" for error in errors)
    assert page.validation_status_label.text() == "정답표 검증 오류를 수정하세요."
    assert not page.grade_button.isEnabled()


def test_validation_errors_are_visible_and_block_grading(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_validation_result(
        AnswerKeyValidationDisplay(
            None, None, None, 0, 0, "0", ("12번 문항: 정답은 1~5만 입력할 수 있습니다.",)
        )
    )
    assert "12번 문항" in page.error_label.text()
    assert "오류" in page.validation_status_label.text()
    assert not page.grade_button.isEnabled()


def test_progress_cancel_cleanup_and_state_preservation(qtbot, clock) -> None:
    page = _ready_page(qtbot)
    page.set_busy(True)
    assert page.progress_frame.isVisible()
    assert page.progress_bar.minimum() == 0
    assert page.progress_bar.maximum() == 0
    clock.advance(65)
    page.set_grading_progress(GradingProgressDisplay(4, 10, 65, 90))
    assert page.progress_frame.isVisible()
    assert page.progress_bar.value() == 4
    assert "4/10" in page.progress_label.text()
    assert "경과 1분 5초" in page.progress_label.text()
    assert not page.grade_button.isEnabled()
    with qtbot.waitSignal(page.cancel_requested) as cancelled:
        QTest.mouseClick(page.cancel_button, Qt.MouseButton.LeftButton)
    assert cancelled.args[0].intent == "cancel"
    page.complete_cancel()
    assert page.key_label.text() == "선택한 정답표: 정답표_생리학.xlsx (시트: 정답표)"
    assert page.grade_button.isEnabled()
    assert not page._progress_timer.isActive()


def test_progress_without_a_total_is_indeterminate_and_never_estimates(qtbot, clock) -> None:
    page = _ready_page(qtbot)

    page.set_grading_progress(GradingProgressDisplay(0, 0, 0, None, "시험 기록을 읽는 중"))

    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 0)
    assert page.progress_label.text() == "시험 기록을 읽는 중 · 경과 0초"
    clock.advance(12)
    page.set_grading_progress(
        GradingProgressDisplay(0, 0, 12, 30, "채점 이미지와 결과 엑셀을 저장하는 중")
    )
    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 0)
    assert page.progress_label.text() == "채점 이미지와 결과 엑셀을 저장하는 중 · 경과 12초"
    page.set_grading_progress(GradingProgressDisplay(0, 0, 12, None))
    assert page.progress_label.text() == "채점 준비 중 · 경과 12초"
    assert "남은 시간" not in page.progress_label.text()


def test_counted_progress_shows_the_remaining_time_only_while_work_is_left(qtbot, clock) -> None:
    page = _ready_page(qtbot)
    status = "점수를 계산하는 중 ({}명)"

    page.set_grading_progress(GradingProgressDisplay(0, 10, 0, 60, status.format("0 / 10")))
    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 10)
    assert page.progress_bar.value() == 0
    assert page.progress_label.text() == "점수를 계산하는 중 (0 / 10명) · 경과 0초"

    clock.advance(20)
    page.set_grading_progress(GradingProgressDisplay(4, 10, 20, 90, status.format("4 / 10")))
    assert page.progress_bar.value() == 4
    assert page.progress_label.text() == (
        "점수를 계산하는 중 (4 / 10명) · 경과 20초 · 남은 시간 약 1분 30초"
    )

    page.set_grading_progress(GradingProgressDisplay(4, 10, 20, None, status.format("4 / 10")))
    assert "남은 시간" not in page.progress_label.text()

    page.set_grading_progress(GradingProgressDisplay(10, 10, 20, 5, status.format("10 / 10")))
    assert page.progress_bar.value() == 10
    assert page.progress_label.text() == "점수를 계산하는 중 (10 / 10명) · 경과 20초"


def test_counted_progress_without_a_status_names_the_counts(qtbot, clock) -> None:
    page = _ready_page(qtbot)

    page.set_grading_progress(GradingProgressDisplay(4, 10, 0, 90))

    assert page.progress_label.text() == "채점 중: 4/10 · 경과 0초 · 남은 시간 약 1분 30초"


def test_the_ticker_redraws_elapsed_and_counts_the_remaining_time_down(qtbot, clock) -> None:
    page = _ready_page(qtbot)
    assert not page._progress_timer.isActive()

    page.set_grading_progress(GradingProgressDisplay(2, 10, 0, 30, "점수를 계산하는 중"))
    assert page._progress_timer.isActive()
    assert page._progress_timer.interval() == 1000

    clock.advance(10)
    # The timer's own timeout drives the redraw between worker events.
    page._progress_timer.timeout.emit()
    assert page.progress_label.text() == "점수를 계산하는 중 · 경과 10초 · 남은 시간 약 20초"
    clock.advance(19)
    page._refresh_progress()
    assert page.progress_label.text() == "점수를 계산하는 중 · 경과 29초 · 남은 시간 약 1초"
    clock.advance(1)
    page._refresh_progress()
    assert page.progress_label.text() == "점수를 계산하는 중 · 경과 30초"

    # A new event restarts the estimate but not the elapsed time.
    page.set_grading_progress(GradingProgressDisplay(3, 10, 30, 8, "점수를 계산하는 중"))
    assert page.progress_label.text() == "점수를 계산하는 중 · 경과 30초 · 남은 시간 약 8초"


def test_elapsed_time_is_the_pages_own_clock_not_the_reported_one(qtbot, clock) -> None:
    page = _ready_page(qtbot)

    page.set_grading_progress(GradingProgressDisplay(1, 10, 4000, None))
    assert page.progress_label.text() == "채점 중: 1/10 · 경과 0초"
    clock.advance(3725)
    page.set_grading_progress(GradingProgressDisplay(2, 10, 1, None))
    assert page.progress_label.text() == "채점 중: 2/10 · 경과 1시간 2분"


def test_clearing_the_progress_hides_the_panel_and_stops_the_ticker(qtbot, clock) -> None:
    page = _ready_page(qtbot)
    page.set_grading_progress(GradingProgressDisplay(2, 10, 0, 30, "점수를 계산하는 중"))
    assert page.progress_frame.isVisible()
    assert page._progress_timer.isActive()

    page.set_grading_progress(None)

    assert page.progress_frame.isHidden()
    assert not page._progress_timer.isActive()
    clock.advance(30)
    page._refresh_progress()
    assert page.progress_label.text() == "점수를 계산하는 중 · 경과 0초 · 남은 시간 약 30초"

    # The next run counts from zero again.
    page.set_grading_progress(GradingProgressDisplay(0, 0, 0, None))
    assert page.progress_label.text() == "채점 준비 중 · 경과 0초"
    assert page.progress_frame.isVisible()
    assert page._progress_timer.isActive()


def test_busy_and_cancel_compat_entry_points_drive_the_same_clock(qtbot, clock) -> None:
    page = _ready_page(qtbot)

    page.set_busy(True)
    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 0)
    assert page.progress_label.text() == "채점 준비 중 · 경과 0초"
    assert page._progress_timer.isActive()
    clock.advance(5)
    page._progress_timer.timeout.emit()
    assert page.progress_label.text() == "채점 준비 중 · 경과 5초"
    page.set_busy(False)
    assert page.progress_frame.isHidden()
    assert not page._progress_timer.isActive()

    page.set_busy(True, 1, 4, "채점 중입니다")
    assert page.progress_label.text() == "채점 중입니다 · 경과 0초"
    page.complete_cancel()
    assert page.progress_frame.isHidden()
    assert not page._progress_timer.isActive()


@pytest.mark.parametrize(
    ("seconds", "text"),
    (
        (0, "0초"),
        (-3, "0초"),
        (59.9, "59초"),
        (60, "1분 0초"),
        (125, "2분 5초"),
        (3599, "59분 59초"),
        (3600, "1시간 0분"),
        (7384, "2시간 3분"),
    ),
)
def test_durations_read_as_seconds_minutes_or_hours(seconds, text) -> None:
    assert GradingPage._time_text(seconds) == text


def test_grading_actions_are_visually_explicit_and_dropzone_matches_scan(qtbot) -> None:
    page = _ready_page(qtbot)

    assert page.answer_key_drop_widget.objectName() == "importDropWidget"
    assert page.grade_button.text() == "채점 실행"
    assert page.grade_button.objectName() == "primaryActionButton"
    assert page.result_button.text() == "채점 결과보기"
    assert page.result_button.objectName() == "primaryActionButton"


def test_sample_result_and_regrade_history_requests(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_connected_session(
        ConnectedSessionDisplay("session-1", 3, "시험", "C:/응답.xlsx", True)
    )
    page.set_grading_history("이전 채점: 2026-07-28 10:00")
    assert "_휴지통" in page.regrade_label.text()
    assert "이전 채점" in page.regrade_label.text()
    with qtbot.waitSignal(page.sample_download_requested) as sample:
        QTest.mouseClick(page.sample_button, Qt.MouseButton.LeftButton)
    assert sample.args[0].intent == "sample_download"
    page.set_result_available("session-1", 3)
    assert page.grade_button.isHidden()
    assert page.answer_key_card.isHidden()
    assert page.validation_card.isHidden()
    assert page.reset_button.isVisible()
    with qtbot.waitSignal(page.result_navigation_requested) as result:
        QTest.mouseClick(page.result_button, Qt.MouseButton.LeftButton)
    assert result.args[0].intent == "result_navigation"


def test_grading_reset_returns_completed_page_to_initial_inputs(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_result_available("session-1", 3)

    QTest.mouseClick(page.reset_button, Qt.MouseButton.LeftButton)

    assert page.result_button.isHidden()
    assert page.grade_button.isVisible()
    assert page.answer_key_card.isVisible()
    assert page.validation_card.isVisible()
    assert page.answer_key_drop_widget.selection is None
    assert page.question_count_label.text() == "-"


def test_grading_page_omits_redundant_response_import_action(qtbot) -> None:
    page = _ready_page(qtbot)

    assert page.findChild(type(page.upload_button), "otherResponseButton") is None


def test_result_navigation_remains_read_only_but_is_gated_while_busy(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_result_available("session-1", 3)
    page.set_write_enabled(False)

    assert page.result_button.isEnabled()
    with qtbot.waitSignal(page.result_navigation_requested) as result:
        QTest.mouseClick(page.result_button, Qt.MouseButton.LeftButton)
    assert result.args[0].intent == "result_navigation"

    page.set_grading_progress(GradingProgressDisplay(1, 2, 1, None))
    assert not page.result_button.isEnabled()
    assert not page.upload_button.isEnabled()


def test_result_navigation_is_cleared_when_connected_identity_changes(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_result_available("session-1", 3)

    page.set_connected_session(ConnectedSessionDisplay("session-2", 4, "새 시험", "C:/새응답.xlsx"))
    page.set_operation_id("grade-2")

    assert page.result_button.isHidden()
    requests = []
    page.result_navigation_requested.connect(requests.append)
    QTest.mouseClick(page.result_button, Qt.MouseButton.LeftButton)
    assert requests == []


def test_a_failed_grading_run_keeps_the_validated_answer_key_usable(qtbot) -> None:
    page = _ready_page(qtbot)
    assert page.grade_button.isEnabled()

    page.set_error(
        "작업을 완료하지 못했습니다. 입력과 실행 환경을 확인하세요.\n"
        "오류 코드: DASHBOARD_SESSION_NOT_FOUND"
    )

    assert "DASHBOARD_SESSION_NOT_FOUND" in page.error_label.text()
    assert page.validation_status_label.text() == "정답표 검증 완료 (이상 없음)"
    assert page.grade_button.isEnabled()
    assert page.question_count_label.text() == "30 문항"
