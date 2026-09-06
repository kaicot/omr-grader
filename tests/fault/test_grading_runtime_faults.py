from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from omr_grader.application.dto import ImportResponseCommand, ResponseBookRequest
from omr_grader.application.response_import_use_case import ResponseImportUseCase
from omr_grader.domain.enums import ExamTerm
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.grading_runtime import (
    CommittedGradingSnapshotReader,
    ResponseImportCommitCoordinator,
)
from omr_grader.infrastructure.session_store import SessionStore
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME


def _write_response_book(path: Path) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(list(RESPONSE_HEADERS))
    sheet.append([1, "scan.png", "00123456", "홍길동", "1", *("" for _ in range(99)), ""])
    book.save(path)


def _store_with_imported_session(tmp_path: Path) -> SessionStore:
    source = tmp_path / "responses.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(list(RESPONSE_HEADERS))
    sheet.append([1, "scan.png", "00123456", "홍길동", "1", *("" for _ in range(99)), ""])
    book.save(source)
    store = SessionStore(tmp_path / "sessions")
    use_case = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
    validation = use_case.validate_response_book(
        ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "Math", 2026, ExamTerm.FIRST)
    )
    assert isinstance(validation, Ok)
    committed = use_case.import_response_book(
        ImportResponseCommand(
            validation.value.validation_token, "imported-session", "import-operation", 0
        )
    )
    assert isinstance(committed, Ok)
    return store


def test_grading_snapshot_rejects_malformed_canonical_artifact_and_closes_lease(
    tmp_path: Path,
) -> None:
    store = _store_with_imported_session(tmp_path)
    opened = store.open_committed_snapshot
    closed: list[bool] = []

    class Lease:
        def __init__(self, lease) -> None:
            self.snapshot_ref = lease.snapshot_ref
            self.manifest = lease.manifest
            self._lease = lease

        def open_allowlisted(self, path: str):
            if path == "semantic_inputs.json":
                return Ok(BytesIO(b'{"combined":[]}'))
            return self._lease.open_allowlisted(path)

        def close(self):
            closed.append(True)
            return self._lease.close()

    class Coordinator:
        def open_committed_snapshot(self, request):
            result = opened(request)
            assert isinstance(result, Ok)
            return Ok(Lease(result.value))

    result = CommittedGradingSnapshotReader(Coordinator()).read_grading_snapshot(
        "imported-session", 1
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "SESSION_GRADING_SNAPSHOT_INVALID"
    assert closed == [True]


def test_grading_snapshot_fails_closed_when_requested_revision_is_not_current(
    tmp_path: Path,
) -> None:
    store = _store_with_imported_session(tmp_path)

    result = CommittedGradingSnapshotReader(store).read_grading_snapshot("imported-session", 2)

    assert isinstance(result, Err)
    assert result.errors[0].code == "SESSION_REVISION_NOT_FOUND"


def test_response_validation_token_rejects_source_swap_close_and_replay(tmp_path: Path) -> None:
    source = tmp_path / "responses.xlsx"
    _write_response_book(source)
    use_case = ResponseImportUseCase(object())
    request = ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "Math", 2026, ExamTerm.FIRST)
    validation = use_case.validate_response_book(request)
    assert isinstance(validation, Ok)

    source.write_bytes(source.read_bytes() + b"changed")
    swapped = use_case.import_response_book(
        ImportResponseCommand(
            validation.value.validation_token, "swap-session", "swap-operation", 0
        )
    )
    replay = use_case.import_response_book(
        ImportResponseCommand(
            validation.value.validation_token, "replay-session", "replay-operation", 0
        )
    )

    assert isinstance(swapped, Err)
    assert swapped.errors[0].code == "XLSX_SOURCE_CHANGED"
    assert validation.value.validation_token.closed
    assert isinstance(replay, Err)
    assert replay.errors[0].code == "XLSX_SOURCE_CHANGED"


def test_response_validation_token_fails_closed_after_explicit_close(tmp_path: Path) -> None:
    source = tmp_path / "responses.xlsx"
    _write_response_book(source)
    use_case = ResponseImportUseCase(object())
    validation = use_case.validate_response_book(
        ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "Math", 2026, ExamTerm.FIRST)
    )
    assert isinstance(validation, Ok)
    validation.value.validation_token.close()

    result = use_case.import_response_book(
        ImportResponseCommand(
            validation.value.validation_token, "closed-session", "closed-operation", 0
        )
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "XLSX_VALIDATION_TOKEN_CLOSED"
