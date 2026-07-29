"""Screen 2: passive, value-only answer-key validation and grading controls."""

from __future__ import annotations

from os.path import basename

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omr_grader.application.grading_presenter import (
    AnswerKeyValidationDisplay,
    ConnectedSessionDisplay,
    GradingPageRequest,
    GradingProgressDisplay,
)


class GradingPage(QWidget):
    """A controller-driven grading view that never opens files or owns sessions."""

    other_response_requested = Signal(object)
    sample_download_requested = Signal(object)
    answer_key_browse_requested = Signal(object)
    answer_key_dropped = Signal(object)
    grade_requested = Signal(object)
    cancel_requested = Signal(object)
    result_navigation_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: ConnectedSessionDisplay | None = None
        self._validation: AnswerKeyValidationDisplay | None = None
        self._answer_key_path: str | None = None
        self._answer_key_sheet: str | None = None
        self._operation_id: str | None = None
        self._history_text = ""
        self._result_identity: tuple[str, int] | None = None
        self._write_enabled = True
        self._busy = False
        self._build_ui()
        self._refresh_state()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)
        title = QLabel("정답표 입력 및 자동 채점")
        title.setObjectName("gradingTitle")
        title.setProperty("role", "page-title")
        root.addWidget(title)
        self.session_label = QLabel("연결된 시험 정보: 응답 결과를 먼저 준비하세요.")
        self.session_label.setObjectName("connectedSessionLabel")
        self.session_label.setWordWrap(True)
        self.session_label.setAccessibleName("연결된 시험 정보")
        root.addWidget(self.session_label)
        self.other_response_button = QPushButton("다른 응답 엑셀 불러오기")
        self.other_response_button.setObjectName("otherResponseButton")
        self.other_response_button.setAccessibleName("다른 응답 엑셀 불러오기")
        self.other_response_button.clicked.connect(lambda: self._emit_intent("other_response"))
        root.addWidget(self.other_response_button, 0, Qt.AlignmentFlag.AlignLeft)
        cards = QHBoxLayout()
        cards.setSpacing(16)
        cards.addWidget(self._answer_key_card(), 1)
        cards.addWidget(self._validation_card(), 1)
        root.addLayout(cards)
        self.regrade_label = QLabel()
        self.regrade_label.setObjectName("regradeWarningLabel")
        self.regrade_label.setWordWrap(True)
        self.regrade_label.setAccessibleName("재채점 및 채점 이력 안내")
        self.regrade_label.setProperty("role", "warning")
        root.addWidget(self.regrade_label)
        self.error_label = QLabel()
        self.error_label.setObjectName("gradingErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("정답표 검증 오류")
        self.error_label.setProperty("role", "error")
        root.addWidget(self.error_label)
        self.progress_frame = QFrame()
        self.progress_frame.setObjectName("gradingProgressPanel")
        progress_layout = QVBoxLayout(self.progress_frame)
        progress_layout.setContentsMargins(12, 8, 12, 8)
        self.progress_label = QLabel("채점 대기 중")
        self.progress_label.setObjectName("gradingProgressLabel")
        self.progress_label.setAccessibleName("채점 진행 상태")
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("gradingProgressBar")
        self.progress_bar.setAccessibleName("채점 진행률")
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar)
        self.cancel_button = QPushButton("채점 중단")
        self.cancel_button.setObjectName("cancelGradingButton")
        self.cancel_button.setAccessibleName("채점 중단")
        self.cancel_button.clicked.connect(self._request_cancel)
        progress_layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignRight)
        root.addWidget(self.progress_frame)
        self.progress_frame.setVisible(False)
        actions = QHBoxLayout()
        actions.addStretch()
        self.grade_button = QPushButton("채점 실행 및 결과 보기")
        self.grade_button.setObjectName("gradeButton")
        self.grade_button.setAccessibleName("채점 실행 및 결과 보기")
        self.grade_button.setDefault(True)
        self.grade_button.clicked.connect(lambda: self._emit_intent("grade"))
        self.result_button = QPushButton("결과 보기")
        self.result_button.setObjectName("gradingResultButton")
        self.result_button.setAccessibleName("채점 결과 보기")
        self.result_button.clicked.connect(self._request_result_navigation)
        self.result_button.setVisible(False)
        actions.addWidget(self.grade_button)
        actions.addWidget(self.result_button)
        actions.addStretch()
        root.addLayout(actions)
        root.addStretch()

    def _answer_key_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("answerKeyUploadCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(card)
        heading = QLabel("정답표 업로드")
        heading.setProperty("role", "card-title")
        layout.addWidget(heading)
        self.sample_button = QPushButton("샘플 정답표 내려받기")
        self.sample_button.setObjectName("sampleAnswerKeyButton")
        self.sample_button.setAccessibleName("샘플 정답표 내려받기")
        self.sample_button.clicked.connect(lambda: self._emit_intent("sample_download"))
        layout.addWidget(self.sample_button)
        self.key_label = QLabel(
            "정답표 Excel 파일을 업로드하거나 끌어다 놓으세요.\n필수 열: 문항번호, 정답, 배점"
        )
        self.key_label.setObjectName("selectedAnswerKeyLabel")
        self.key_label.setWordWrap(True)
        self.key_label.setAccessibleName("선택한 정답표")
        layout.addWidget(self.key_label)
        self.upload_button = QPushButton("정답표 엑셀 찾아보기")
        self.upload_button.setObjectName("answerKeyUploadButton")
        self.upload_button.setAccessibleName("정답표 엑셀 찾아보기")
        self.upload_button.clicked.connect(lambda: self._emit_intent("answer_key_browse"))
        layout.addWidget(self.upload_button)
        return card

    def _validation_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("answerKeyValidationCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QGridLayout(card)
        heading = QLabel("정답표 검증 요약")
        heading.setProperty("role", "card-title")
        layout.addWidget(heading, 0, 0, 1, 2)
        self.question_count_label, self.unasked_count_label, self.total_points_label = (
            QLabel("-"),
            QLabel("-"),
            QLabel("-"),
        )
        self.validation_status_label = QLabel("정답표를 업로드하면 검증 결과가 표시됩니다.")
        self.validation_status_label.setObjectName("validationStatusLabel")
        self.validation_status_label.setAccessibleName("정답표 검증 상태")
        for row, (text, value) in enumerate(
            (
                ("총 출제 문항 수", self.question_count_label),
                ("미출제(채점 제외) 문항 수", self.unasked_count_label),
                ("총 배점 합계", self.total_points_label),
            ),
            1,
        ):
            layout.addWidget(QLabel(text), row, 0)
            layout.addWidget(value, row, 1)
        layout.addWidget(self.validation_status_label, 4, 0, 1, 2)
        return card

    def set_connected_session(self, session: ConnectedSessionDisplay | None) -> None:
        if session is not None and not isinstance(session, ConnectedSessionDisplay):
            raise TypeError("session must be ConnectedSessionDisplay or None")
        identity = None if session is None else (session.session_id, session.revision)
        if identity != self._session_identity():
            self.clear_result_available()
        self._session = session
        self.session_label.setText(
            "연결된 시험 정보: 응답 결과를 먼저 준비하세요."
            if session is None
            else f"연결된 시험 정보: {session.exam_name} ({session.response_path})"
        )
        self._set_history_label()
        self._refresh_state()

    def set_operation_id(self, operation_id: str | None) -> None:
        if operation_id is not None and (
            not isinstance(operation_id, str) or not operation_id.strip()
        ):
            raise ValueError("operation_id must be nonempty str or None")
        self._operation_id = operation_id
        self._refresh_state()

    def set_answer_key_selection(self, path: str | None, sheet_name: str | None) -> None:
        if (path is None) != (sheet_name is None):
            raise ValueError("path and sheet_name must be provided together")
        if path is not None and (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(sheet_name, str)
            or not sheet_name.strip()
        ):
            raise ValueError("path and sheet_name must be nonempty strings")
        self._answer_key_path, self._answer_key_sheet = path, sheet_name
        self.key_label.setText(
            f"선택한 정답표: {basename(path)} (시트: {sheet_name})"
            if path
            else "정답표 Excel 파일을 업로드하거나 끌어다 놓으세요.\n필수 열: 문항번호, 정답, 배점"
        )
        self._validation = None
        self._clear_validation_display()
        self._refresh_state()

    def drop_answer_key(self, path: str, sheet_name: str) -> None:
        """Forward a controller-selected drop value without performing file I/O."""
        if not self._can_mutate():
            return
        self.set_answer_key_selection(path, sheet_name)
        self._emit_intent("answer_key_drop")

    def set_validation_result(self, result: AnswerKeyValidationDisplay | None) -> None:
        self._validation = result
        if result is None:
            self._clear_validation_display()
        else:
            if result.source_path is not None:
                self._answer_key_path, self._answer_key_sheet = (
                    result.source_path,
                    result.sheet_name,
                )
            self.question_count_label.setText(f"{result.question_count} 문항")
            self.unasked_count_label.setText(f"{result.unasked_count} 문항")
            self.total_points_label.setText(f"{result.total_points} 점")
            self.error_label.setText("\n".join(f"• {error}" for error in result.errors))
            self.validation_status_label.setText(
                "정답표 검증 완료 (이상 없음)"
                if result.is_valid
                else "정답표 검증 오류를 수정하세요."
            )
        self._refresh_state()

    def set_error(self, error: str) -> None:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be nonempty str")
        self.error_label.setText(f"• {error}")
        self.validation_status_label.setText("정답표 검증 오류를 수정하세요.")
        self._validation = AnswerKeyValidationDisplay(
            self._answer_key_path,
            self._answer_key_sheet,
            basename(self._answer_key_path) if self._answer_key_path else None,
            0,
            0,
            "0",
            (error,),
        )
        self._refresh_state()

    def set_errors(self, errors: tuple[str, ...]) -> None:
        if not isinstance(errors, tuple) or not all(
            isinstance(error, str) and error.strip() for error in errors
        ):
            raise TypeError("errors must be a tuple of nonempty strings")
        if not errors:
            self._clear_validation_display()
            self._validation = None
        else:
            self.error_label.setText("\n".join(f"• {error}" for error in errors))
            self.validation_status_label.setText("정답표 검증 오류를 수정하세요.")
            self._validation = AnswerKeyValidationDisplay(
                self._answer_key_path,
                self._answer_key_sheet,
                basename(self._answer_key_path) if self._answer_key_path else None,
                0,
                0,
                "0",
                errors,
            )
        self._refresh_state()

    def set_grading_progress(self, progress: GradingProgressDisplay | None) -> None:
        self._busy = progress is not None
        self.progress_frame.setVisible(progress is not None)
        if progress is not None:
            self.progress_bar.setRange(0, max(progress.total, 1))
            self.progress_bar.setValue(progress.completed)
            eta = (
                "계산 중" if progress.eta_seconds is None else self._time_text(progress.eta_seconds)
            )
            status = progress.status or f"채점 중: {progress.completed}/{progress.total}"
            self.progress_label.setText(
                f"{status} · 경과 {self._time_text(progress.elapsed_seconds)} · 남은 시간 {eta}"
            )
        self._refresh_state()

    def set_busy(self, busy: bool, completed: int = 0, total: int = 0, status: str = "") -> None:
        """Compatibility display entry point; controllers should use set_grading_progress."""
        if type(busy) is not bool:
            raise TypeError("busy must be bool")
        self.set_grading_progress(
            GradingProgressDisplay(completed, total, 0, None, status) if busy else None
        )

    def set_write_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        self._write_enabled = enabled
        self._refresh_state()

    def set_result_available(self, session_id: str, revision: int) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a nonempty str")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a nonnegative int")
        self._result_identity = (session_id, revision)
        self._refresh_state()

    def clear_result_available(self) -> None:
        self._result_identity = None
        self._refresh_state()

    def set_grading_history(self, history_text: str) -> None:
        if not isinstance(history_text, str):
            raise TypeError("history_text must be str")
        self._history_text = history_text
        self._set_history_label()

    def complete_cancel(
        self, message: str = "채점을 중단했습니다. 기존 입력으로 다시 시도할 수 있습니다."
    ) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be nonempty str")
        self.set_grading_progress(None)
        self.error_label.setText(message)
        self._refresh_state()

    def _set_history_label(self) -> None:
        warning = (
            "재채점하면 기존 채점 결과는 세션의 _휴지통에 백업한 뒤 새 결과를 만듭니다."
            if self._session is not None and self._session.is_regrade
            else ""
        )
        self.regrade_label.setText(
            "\n".join(part for part in (warning, self._history_text) if part)
        )

    def _clear_validation_display(self) -> None:
        self.question_count_label.setText("-")
        self.unasked_count_label.setText("-")
        self.total_points_label.setText("-")
        self.validation_status_label.setText("정답표를 업로드하면 검증 결과가 표시됩니다.")
        self.error_label.clear()

    def _request_cancel(self) -> None:
        if self._busy:
            request = self._request("cancel")
            if request is not None:
                self.cancel_requested.emit(request)

    def _request_result_navigation(self) -> None:
        if self.result_button.isVisible() and not self._busy:
            request = self._request("result_navigation")
            if request is not None and self._result_identity == (
                request.session_id,
                request.revision,
            ):
                self.result_navigation_requested.emit(request)

    def _emit_intent(self, intent: str) -> None:
        if not self._can_mutate():
            return
        request = self._request(intent)
        if request is None:
            return
        signals = {
            "other_response": self.other_response_requested,
            "sample_download": self.sample_download_requested,
            "answer_key_browse": self.answer_key_browse_requested,
            "answer_key_drop": self.answer_key_dropped,
            "grade": self.grade_requested,
        }
        signals[intent].emit(request)

    def _request(self, intent: str) -> GradingPageRequest | None:
        if self._session is None or self._operation_id is None:
            return None
        return GradingPageRequest(
            self._session.session_id,
            self._session.revision,
            self._session.response_path,
            self._answer_key_path,
            self._answer_key_sheet,
            self._operation_id,
            intent,
            self._session.is_regrade,
        )

    def _can_mutate(self) -> bool:
        return (
            self._write_enabled
            and not self._busy
            and self._session is not None
            and self._operation_id is not None
        )

    def _refresh_state(self) -> None:
        can_mutate = self._can_mutate()
        has_key = self._answer_key_path is not None and self._answer_key_sheet is not None
        valid_key = self._validation is not None and self._validation.is_valid
        result_available = (
            self._result_identity is not None and self._result_identity == self._session_identity()
        )
        self.other_response_button.setEnabled(can_mutate)
        self.sample_button.setEnabled(can_mutate)
        self.upload_button.setEnabled(can_mutate)
        self.grade_button.setEnabled(can_mutate and has_key and valid_key)
        self.cancel_button.setEnabled(
            self._busy and self._session is not None and self._operation_id is not None
        )
        self.result_button.setVisible(result_available)
        self.result_button.setEnabled(
            result_available
            and not self._busy
            and self._session is not None
            and self._operation_id is not None
        )

    def has_connected_session(self, session_id: str, revision: int) -> bool:
        return self._session_identity() == (session_id, revision)

    def _session_identity(self) -> tuple[str, int] | None:
        if self._session is None:
            return None
        return self._session.session_id, self._session.revision

    @staticmethod
    def _time_text(seconds: int) -> str:
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}분 {seconds:02d}초" if minutes else f"{seconds}초"


__all__ = ["GradingPage"]
