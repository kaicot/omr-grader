from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from omr_grader.application.grading_presenter import (
    AnswerKeyValidationDisplay,
    ConnectedSessionDisplay,
    GradingProgressDisplay,
)
from omr_grader.ui.grading_page import GradingPage

SESSION = ConnectedSessionDisplay("session-1", 3, "26-2 생리학 중간고사", "C:/응답결과.xlsx")


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
    assert not page.other_response_button.isEnabled()
    assert not page.sample_button.isEnabled()


def test_browse_and_drop_requests_preserve_selection_for_validation_and_retry(qtbot) -> None:
    page = _ready_page(qtbot)
    with qtbot.waitSignal(page.answer_key_browse_requested) as browse:
        QTest.mouseClick(page.upload_button, Qt.MouseButton.LeftButton)
    assert browse.args[0].intent == "answer_key_browse"
    with qtbot.waitSignal(page.answer_key_dropped) as drop:
        page.drop_answer_key("C:/새_정답표.xlsx", "Sheet1")
    assert drop.args[0].answer_key_path == "C:/새_정답표.xlsx"
    assert drop.args[0].answer_key_sheet == "Sheet1"
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


def test_progress_cancel_cleanup_and_state_preservation(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_grading_progress(GradingProgressDisplay(4, 10, 65, 90))
    assert page.progress_frame.isVisible()
    assert page.progress_bar.value() == 4
    assert "4/10" in page.progress_label.text()
    assert "경과 1분 05초" in page.progress_label.text()
    assert not page.grade_button.isEnabled()
    with qtbot.waitSignal(page.cancel_requested) as cancelled:
        QTest.mouseClick(page.cancel_button, Qt.MouseButton.LeftButton)
    assert cancelled.args[0].intent == "cancel"
    page.complete_cancel()
    assert page.key_label.text() == "선택한 정답표: 정답표_생리학.xlsx (시트: 정답표)"
    assert page.grade_button.isEnabled()


def test_sample_other_response_result_and_regrade_history_requests(qtbot) -> None:
    page = _ready_page(qtbot)
    page.set_connected_session(
        ConnectedSessionDisplay("session-1", 3, "시험", "C:/응답.xlsx", True)
    )
    page.set_grading_history("이전 채점: 2026-07-28 10:00")
    assert "_휴지통" in page.regrade_label.text()
    assert "이전 채점" in page.regrade_label.text()
    with qtbot.waitSignal(page.sample_download_requested) as sample:
        QTest.mouseClick(page.sample_button, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(page.other_response_requested) as response:
        QTest.mouseClick(page.other_response_button, Qt.MouseButton.LeftButton)
    assert sample.args[0].intent == "sample_download"
    assert response.args[0].intent == "other_response"
    page.set_result_available("session-1", 3)
    with qtbot.waitSignal(page.result_navigation_requested) as result:
        QTest.mouseClick(page.result_button, Qt.MouseButton.LeftButton)
    assert result.args[0].intent == "result_navigation"


def test_other_response_import_intent_can_be_retried_with_immutable_values(qtbot) -> None:
    page = _ready_page(qtbot)
    requests = []
    page.other_response_requested.connect(requests.append)

    QTest.mouseClick(page.other_response_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.other_response_button, Qt.MouseButton.LeftButton)

    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0].intent == "other_response"
    assert requests[0].session_id == "session-1"
    assert requests[0].revision == 3
    assert requests[0].response_path == "C:/응답결과.xlsx"
    assert requests[0].operation_id == "grade-1"


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
    assert not page.other_response_button.isEnabled()


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
