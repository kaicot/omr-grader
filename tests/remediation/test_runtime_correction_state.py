from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from openpyxl import Workbook
import pytest

from omr_grader.application.correction_use_case import CorrectionApplicationService
from omr_grader.application.backup_use_case import BackupApplicationService
from omr_grader.application.dto import (
    AnswerKeyValidation,
    BackupExportRequest,
    BackupValidateRequest,
    CollisionPolicy,
    CorrectionBatch,
    ImportResponseCommand,
    RegradeCommand,
    ResponseBookRequest,
    RestoreCommand,
    SnapshotRequest,
)
from omr_grader.application.grading_use_case import GradingUseCase
from omr_grader.application.response_import_use_case import ResponseImportUseCase
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    ExamTerm,
    KeyQuestionStatus,
    TargetKind,
    SnapshotPurpose,
)
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    CorrectionDraft,
    EffectiveResponse,
)
from omr_grader.infrastructure.detail_repository import DetailRepository, _read_correction_state
from omr_grader.infrastructure.grading_runtime import (
    CommittedGradingSnapshotReader,
    ResponseImportCommitCoordinator,
)
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME


def _key(answer: int) -> AnswerKeySnapshot:
    unanswered = AnswerValue((), AnswerStatus.UNASKED)
    return AnswerKeySnapshot(
        1,
        AnswerKeySnapshotKind.WORKBOOK,
        "runtime-key.xlsx",
        "a" * 64,
        "정답표",
        "runtime-test",
        (
            AnswerKeyEntry(1, AnswerValue((answer,), AnswerStatus.NORMAL), "1", KeyQuestionStatus.ANSWER),
            *(
                AnswerKeyEntry(question, unanswered, "0", KeyQuestionStatus.UNASKED)
                for question in range(2, 101)
            ),
        ),
        (),
    )


def _response_book(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = RESPONSE_SHEET_NAME
    sheet.append(list(RESPONSE_HEADERS))
    sheet.append([1, "synthetic.png", "00123456", "합성 학생", "1", *("" for _ in range(99)), ""])
    workbook.save(path)


def test_runtime_correction_state_survives_save_reopen_and_regrade(tmp_path: Path) -> None:
    source = tmp_path / "responses.xlsx"
    _response_book(source)
    store = SessionStore(tmp_path / "Data")
    coordinator = SessionCommitCoordinator(store)
    importer = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
    validated = importer.validate_response_book(
        ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "runtime", 2026, ExamTerm.FIRST)
    )
    assert isinstance(validated, Ok)
    imported = importer.import_response_book(
        ImportResponseCommand(validated.value.validation_token, "runtime-session", "import", 0)
    )
    assert isinstance(imported, Ok)

    class Keys:
        def __init__(self, key: AnswerKeySnapshot) -> None:
            self.key = key

        def validate_answer_key(self, _request: object) -> Ok[AnswerKeyValidation]:
            return Ok(AnswerKeyValidation(self.key))

    grader = GradingUseCase(CommittedGradingSnapshotReader(coordinator), Keys(_key(1)), coordinator)
    graded = grader.regrade(RegradeCommand("runtime-session", 1, "ignored.xlsx", "정답표", "grade"))
    assert isinstance(graded, Ok)

    details = DetailRepository(coordinator)
    correction = CorrectionApplicationService(details, coordinator)
    opened = details.read_correction_snapshot("runtime-session", 2)
    assert isinstance(opened, Ok)
    assert opened.value.correction_state.events == ()
    work_item_id = opened.value.responses[0].work_item_id
    before = opened.value.responses[0].answers[0]
    assert before == AnswerValue((1,), AnswerStatus.NORMAL)
    assert opened.value.lease.close().value is None
    batch = CorrectionBatch(
        "runtime-session",
        2,
        "runtime-session:2:q1",
        "correct",
        (
            CorrectionDraft(
                work_item_id,
                TargetKind.ANSWER_CELL,
                1,
                before,
                AnswerValue((2,), AnswerStatus.NORMAL),
                "runtime-test",
            ),
        ),
    )
    saved = correction.save_corrections(batch)
    assert isinstance(saved, Ok)
    assert saved.value.revision == 3

    reopened = details.read_correction_snapshot("runtime-session", 3)
    assert isinstance(reopened, Ok)
    assert reopened.value.responses[0].answers[0] == AnswerValue((2,), AnswerStatus.NORMAL)
    assert len(reopened.value.correction_state.events) == 1
    assert reopened.value.lease.close().value is None

    regraded = GradingUseCase(CommittedGradingSnapshotReader(coordinator), Keys(_key(2)), coordinator).regrade(
        RegradeCommand("runtime-session", 3, "ignored.xlsx", "정답표", "regrade")
    )
    assert isinstance(regraded, Ok)
    final = details.read_correction_snapshot("runtime-session", 4)
    assert isinstance(final, Ok)
    assert final.value.responses[0].answers[0] == AnswerValue((2,), AnswerStatus.NORMAL)
    assert len(final.value.correction_state.events) == 1
    assert final.value.lease.close().value is None

    session = next(path for path in store.root.iterdir() if (path / "CURRENT.json").is_file())
    pointer = json.loads((session / "CURRENT.json").read_text(encoding="utf-8"))
    state = json.loads((session / pointer["generation_relpath"] / "correction_state.json").read_text(encoding="utf-8"))
    assert state["snapshot"]["revision"] == 4
    assert state["events"][0]["before"] == before.to_dict()

    archive = tmp_path / "runtime.omrbak"
    backup = BackupApplicationService(coordinator, restore_publisher=None)
    exported = backup.export_backup(
        BackupExportRequest(
            SnapshotRequest("runtime-session", 4, SnapshotPurpose.BACKUP),
            str(archive),
            CollisionPolicy.ERROR,
            "backup",
        )
    )
    assert isinstance(exported, Ok)
    restored_store = SessionStore(tmp_path / "RestoredData")
    restored_store.root.mkdir()
    restore = BackupApplicationService(
        coordinator, restore_publisher=restored_store.restore_publisher()
    )
    backup_token = restore.validate_backup(BackupValidateRequest(str(archive)))
    assert isinstance(backup_token, Ok)
    restored = restore.restore_backup(
        RestoreCommand(backup_token.value, str(restored_store.root), "restore")
    )
    assert isinstance(restored, Ok)
    restored_coordinator = SessionCommitCoordinator(restored_store)
    restored_details = DetailRepository(restored_coordinator)
    restored_state = restored_details.read_correction_snapshot("runtime-session", 4)
    assert isinstance(restored_state, Ok)
    assert restored_state.value.responses[0].answers[0] == AnswerValue((2,), AnswerStatus.NORMAL)
    assert len(restored_state.value.correction_state.events) == 1
    assert restored_state.value.lease.close().value is None
    corrected_after_restore = CorrectionApplicationService(restored_details, restored_coordinator).save_corrections(
        CorrectionBatch(
            "runtime-session",
            4,
            "runtime-session:4:q1",
            "correct-after-restore",
            (
                CorrectionDraft(
                    work_item_id,
                    TargetKind.ANSWER_CELL,
                    1,
                    AnswerValue((2,), AnswerStatus.NORMAL),
                    AnswerValue((3,), AnswerStatus.NORMAL),
                    "runtime-test",
                ),
            ),
        )
    )
    assert isinstance(corrected_after_restore, Ok)
    assert corrected_after_restore.value.revision == 5


def test_fixed20_compact_adapter_is_read_only_and_declared_state_absence_is_corruption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "responses.xlsx"
    _response_book(source)
    store = SessionStore(tmp_path / "Data")
    coordinator = SessionCommitCoordinator(store)
    importer = ResponseImportUseCase(ResponseImportCommitCoordinator(store))
    validated = importer.validate_response_book(
        ResponseBookRequest(str(source), RESPONSE_SHEET_NAME, "runtime", 2026, ExamTerm.FIRST)
    )
    assert isinstance(validated, Ok)
    assert isinstance(
        importer.import_response_book(
            ImportResponseCommand(validated.value.validation_token, "fixed20", "import", 0)
        ),
        Ok,
    )

    class Keys:
        def validate_answer_key(self, _request: object) -> Ok[AnswerKeyValidation]:
            return Ok(AnswerKeyValidation(_key(1)))

    assert isinstance(
        GradingUseCase(CommittedGradingSnapshotReader(coordinator), Keys(), coordinator).regrade(
            RegradeCommand("fixed20", 1, "ignored.xlsx", "정답표", "grade")
        ),
        Ok,
    )
    opened = coordinator.open_committed_snapshot(
        SnapshotRequest("fixed20", 2, SnapshotPurpose.DETAIL)
    )
    assert isinstance(opened, Ok)
    lease = opened.value
    combined_file = lease.open_allowlisted("semantic_inputs.json")
    assert isinstance(combined_file, Ok)
    with combined_file.value:
        combined = json.load(combined_file.value)
    responses = tuple(
        EffectiveResponse.from_dict(value)
        for value in combined["combined"]["responses"]
    )
    session = next(path for path in store.root.iterdir() if (path / "CURRENT.json").is_file())
    pointer = json.loads((session / "CURRENT.json").read_text(encoding="utf-8"))
    state_path = session / pointer["generation_relpath"] / "correction_state.json"
    original_state = state_path.read_bytes()

    class LegacyLease:
        def __init__(self, delegate, *, declared: bool) -> None:
            self.snapshot_ref = delegate.snapshot_ref
            self.manifest = (
                delegate.manifest
                if declared
                else replace(
                    delegate.manifest,
                    files=tuple(
                        item
                        for item in delegate.manifest.files
                        if item.path != "correction_state.json"
                    ),
                )
            )
            self._delegate = delegate

        def open_allowlisted(self, path: str):
            if path == "correction_state.json":
                return Err((ErrorInfo("MISSING", "error.missing"),))
            return self._delegate.open_allowlisted(path)

    legacy = _read_correction_state(LegacyLease(lease, declared=False), responses, "fixed20")
    assert legacy.source_kind == "legacy-fixed20"
    assert not legacy.history_complete
    assert state_path.read_bytes() == original_state

    with pytest.raises(ValueError, match="declared correction state"):
        _read_correction_state(LegacyLease(lease, declared=True), responses, "fixed20")
    assert lease.close().value is None
