"""Sessions graded by 2.1.0–2.1.1 committed an unrounded average; they stay usable."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

import omr_grader.domain.grading as grading
from omr_grader.application.correction_use_case import CorrectionApplicationService
from omr_grader.application.dto import (
    AnswerKeyValidation,
    CorrectionBatch,
    FinalizeCommand,
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
    TargetKind,
)
from omr_grader.domain.errors import Ok
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    CorrectionDraft,
)
from omr_grader.infrastructure.dashboard_repository import DashboardRepository
from omr_grader.infrastructure.detail_repository import DetailRepository
from omr_grader.infrastructure.grading_runtime import (
    CommittedGradingSnapshotReader,
    ResponseImportCommitCoordinator,
)
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME

SESSION = "legacy-average"
LEGACY_THIRD = "0.3333333333333333333333333333"


def _key() -> AnswerKeySnapshot:
    unasked = AnswerValue((), AnswerStatus.UNASKED)
    return AnswerKeySnapshot(
        1,
        AnswerKeySnapshotKind.WORKBOOK,
        "legacy-key.xlsx",
        "a" * 64,
        "정답표",
        "legacy-average",
        (
            AnswerKeyEntry(
                1, AnswerValue((1,), AnswerStatus.NORMAL), "1", KeyQuestionStatus.ANSWER
            ),
            *(
                AnswerKeyEntry(question, unasked, "0", KeyQuestionStatus.UNASKED)
                for question in range(2, 101)
            ),
        ),
        (),
    )


class _Keys:
    def validate_answer_key(self, _request: object) -> Ok[AnswerKeyValidation]:
        return Ok(AnswerKeyValidation(_key()))


def _grader(coordinator: SessionCommitCoordinator) -> GradingUseCase:
    return GradingUseCase(CommittedGradingSnapshotReader(coordinator), _Keys(), coordinator)


def _unrounded_average(scores: tuple[Decimal, ...]) -> Decimal:
    """The quotient 2.1.0 and 2.1.1 committed: 28 significant digits, never rounded."""
    return sum(scores, Decimal(0)) / len(scores)


def _listing(store: SessionStore, tmp_path: Path) -> list[tuple[int, int, str | None]]:
    listed = DashboardRepository(
        tmp_path / "dashboard_index.json", store.discover_active_committed_leases
    ).list_active()
    assert isinstance(listed, Ok)
    return [
        (entry.revision, entry.participant_count, entry.average_score)
        for entry in listed.value.entries
    ]


@pytest.fixture
def legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SessionStore, SessionCommitCoordinator]:
    source = tmp_path / "responses.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(list(RESPONSE_HEADERS))
    for number, (student_id, answer) in enumerate(
        (("00123456", "1"), ("00123457", "2"), ("00123458", "2")), 1
    ):
        sheet.append(
            [number, f"scan{number}.png", student_id, f"학생{number}", answer]
            + ["" for _ in range(99)]
            + [""]
        )
    workbook.save(source)
    store = SessionStore(tmp_path / "Data")
    coordinator = SessionCommitCoordinator(store)
    importer = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
    validated = importer.validate_response_book(
        ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "legacy", 2026, ExamTerm.FIRST)
    )
    assert isinstance(validated, Ok)
    imported = importer.import_response_book(
        ImportResponseCommand(validated.value.validation_token, SESSION, "import", 0)
    )
    assert isinstance(imported, Ok)
    with monkeypatch.context() as patch:
        patch.setattr(grading, "score_average", _unrounded_average)
        graded = _grader(coordinator).regrade(
            RegradeCommand(SESSION, 1, "key.xlsx", "정답표", "legacy-grade")
        )
    assert isinstance(graded, Ok)
    assert graded.value.revision == 2
    committed = next((tmp_path / "Data").rglob("g00000002_*/semantic_inputs.json"))
    assert LEGACY_THIRD in committed.read_text(encoding="utf-8")
    return store, coordinator


def test_legacy_session_is_listed_with_its_average_rounded(
    legacy: tuple[SessionStore, SessionCommitCoordinator], tmp_path: Path
) -> None:
    store, _ = legacy

    assert _listing(store, tmp_path) == [(2, 3, "0.333333333333")]


def test_legacy_session_can_be_regraded_and_found_after_the_commit(
    legacy: tuple[SessionStore, SessionCommitCoordinator], tmp_path: Path
) -> None:
    store, coordinator = legacy

    regraded = _grader(coordinator).regrade(
        RegradeCommand(SESSION, 2, "key.xlsx", "정답표", "regrade")
    )

    assert isinstance(regraded, Ok)
    assert regraded.value.revision == 3
    assert _listing(store, tmp_path) == [(3, 3, "0.333333333333")]
    rewritten = next((tmp_path / "Data").rglob("g00000003_*/semantic_inputs.json"))
    assert LEGACY_THIRD not in rewritten.read_text(encoding="utf-8")


def test_legacy_session_corrections_can_be_opened_and_saved(
    legacy: tuple[SessionStore, SessionCommitCoordinator], tmp_path: Path
) -> None:
    store, coordinator = legacy
    details = DetailRepository(coordinator)
    opened = details.read_correction_snapshot(SESSION, 2)
    assert isinstance(opened, Ok)
    second = next(item for item in opened.value.responses if item.student_id == "00123457")
    assert opened.value.lease.close().value is None
    edit = CorrectionDraft(
        second.work_item_id,
        TargetKind.ANSWER_CELL,
        1,
        second.answers[0],
        AnswerValue((1,), AnswerStatus.NORMAL),
        "검수 확인",
    )

    saved = CorrectionApplicationService(details, coordinator).save_corrections(
        CorrectionBatch(SESSION, 2, "legacy:2:q1", "correct", (edit,))
    )

    assert isinstance(saved, Ok)
    assert saved.value.revision == 3
    assert _listing(store, tmp_path) == [(3, 3, "0.666666666667")]


def test_legacy_session_can_be_finalized(
    legacy: tuple[SessionStore, SessionCommitCoordinator], tmp_path: Path
) -> None:
    store, coordinator = legacy

    finalized = _grader(coordinator).finalize(FinalizeCommand(SESSION, 2, "finalize"))

    assert isinstance(finalized, Ok)
    assert finalized.value.revision == 3
    assert _listing(store, tmp_path) == [(3, 3, "0.333333333333")]
