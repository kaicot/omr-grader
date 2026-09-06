from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from omr_grader.application.dto import (
    AnswerKeyValidation,
    ImportResponseCommand,
    RegradeCommand,
    ResponseBookRequest,
)
from omr_grader.application.grading_use_case import GradingUseCase
from omr_grader.application.response_import_use_case import ResponseImportUseCase
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    ExamTerm,
    KeyQuestionStatus,
    OperationKind,
    SessionState,
)
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.domain.models import AnswerKeyEntry, AnswerKeySnapshot, AnswerValue
from omr_grader.infrastructure.grading_runtime import (
    CommittedGradingSnapshotReader,
    ResponseImportCommitCoordinator,
)
from omr_grader.infrastructure.session_store import SessionStore
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME


def _response_book(path: Path) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(list(RESPONSE_HEADERS))
    sheet.append([1, "scan.png", "00123456", "홍길동", "1", *("" for _ in range(99)), ""])
    book.save(path)


def _answer_key() -> AnswerKeySnapshot:
    entries = (
        AnswerKeyEntry(
            1,
            AnswerValue((1,), AnswerStatus.NORMAL),
            "1",
            KeyQuestionStatus.ANSWER,
        ),
    ) + tuple(
        AnswerKeyEntry(
            question,
            AnswerValue((), AnswerStatus.UNASKED),
            "0",
            KeyQuestionStatus.UNASKED,
        )
        for question in range(2, 101)
    )
    return AnswerKeySnapshot(
        1,
        AnswerKeySnapshotKind.WORKBOOK,
        "key.xlsx",
        "a" * 64,
        "Answers",
        "v1",
        entries,
        (),
    )


def _imported_store(tmp_path: Path) -> tuple[SessionStore, str]:
    source = tmp_path / "responses.xlsx"
    _response_book(source)
    store = SessionStore(tmp_path / "sessions")
    use_case = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
    validation = use_case.validate_response_book(
        ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "Math", 2026, ExamTerm.FIRST)
    )
    assert isinstance(validation, Ok)
    imported = use_case.import_response_book(
        ImportResponseCommand(
            validation.value.validation_token, "imported-session", "import-operation", 0
        )
    )
    assert isinstance(imported, Ok)
    return store, imported.value.session_id


def test_imported_session_is_immutably_published_and_is_the_grading_snapshot_authority(
    tmp_path: Path,
) -> None:
    store, session_id = _imported_store(tmp_path)

    snapshot = CommittedGradingSnapshotReader(store).read_grading_snapshot(session_id, 1)

    assert isinstance(snapshot, Ok)
    assert snapshot.value.state is SessionState.RECOGNIZED
    assert len(snapshot.value.responses) == 1
    assert snapshot.value.responses[0].student_id == "00123456"
    assert snapshot.value.projection_request.imported_responses[0].source_filename == "scan.png"


def test_regrade_uses_pinned_snapshot_and_reaches_the_single_commit_authority(
    tmp_path: Path,
) -> None:
    store, session_id = _imported_store(tmp_path)
    captured = []

    class AnswerKeys:
        def validate_answer_key(self, request):
            return Ok(AnswerKeyValidation(_answer_key()))

    class Coordinator:
        def commit_generation(self, mutation):
            captured.append(mutation)
            return Err((ErrorInfo("COMMIT_DENIED", "error.commit_denied"),))

    result = GradingUseCase(
        CommittedGradingSnapshotReader(store), AnswerKeys(), Coordinator()
    ).regrade(RegradeCommand(session_id, 1, "key.xlsx", "Answers", "regrade-operation"))

    assert isinstance(result, Err)
    assert result.errors[0].code == "COMMIT_DENIED"
    assert len(captured) == 1
    assert captured[0].operation_kind is OperationKind.REGRADE
    assert captured[0].expected_revision == 1
    assert captured[0].target_state is SessionState.GRADED
