from __future__ import annotations

from decimal import Decimal

import pytest

from omr_grader.domain.enums import (
    AnswerStatus,
    CellStatus,
    FieldStatus,
    KeyQuestionStatus,
    ProcessingStatus,
    RosterRowStatus,
    RosterSnapshotKind,
    SourceKind,
    StudentIdStatus,
    TargetKind,
)
from omr_grader.domain.errors import ErrorInfo
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerRecognition,
    AnswerValue,
    AutomaticPage,
    CellEvidence,
    CorrectionDraft,
    IdCell,
    IdCorrectionValue,
    PageRef,
    PixelRect,
    RatioRect,
    RosterEntry,
    RosterSnapshot,
    StudentIdRecognition,
)


def digit_evidence(digit: int, *, index: int = 0, selected: bool = False) -> CellEvidence:
    return CellEvidence(index, None, digit, None, None, None, "0.5", selected, CellStatus.NORMAL)


def answer_evidence(question: int, choice: int) -> CellEvidence:
    return CellEvidence(
        question * 10 + choice,
        question,
        None,
        choice,
        PixelRect(choice, question, 1, 1),
        RatioRect("0", "0", "0.1", "0.1"),
        "0.5",
        choice == 1,
        CellStatus.NORMAL,
    )


def id_cell(selected_digit: str | None = "0") -> IdCell:
    status = FieldStatus.NORMAL if selected_digit is not None else FieldStatus.BLANK
    return IdCell(
        selected_digit,
        status,
        tuple(
            digit_evidence(digit, index=digit, selected=selected_digit == str(digit))
            for digit in range(10)
        ),
    )


def answer(question: int, value: AnswerValue | None = None) -> AnswerRecognition:
    return AnswerRecognition(
        question,
        value or AnswerValue((1,), AnswerStatus.NORMAL),
        tuple(answer_evidence(question, choice) for choice in range(1, 6)),
    )


def page_ref() -> PageRef:
    return PageRef(
        1,
        "session-1",
        "work-1",
        SourceKind.IMAGE,
        "a" * 64,
        "scan.png",
        "원본",
        1,
        None,
        0,
        0,
        "scan-1",
    )


def automatic_page(status: ProcessingStatus = ProcessingStatus.PROCESSED) -> AutomaticPage:
    failed = status in {ProcessingStatus.FAILED, ProcessingStatus.UNPROCESSABLE}
    student_id = (
        StudentIdRecognition(None, StudentIdStatus.UNREADABLE, ())
        if failed
        else StudentIdRecognition(
            "00000000", StudentIdStatus.NORMAL, tuple(id_cell("0") for _ in range(8))
        )
    )
    answers = () if failed else tuple(answer(question) for question in range(1, 101))
    evidence = (
        ()
        if failed
        else tuple(cell for item in student_id.cells for cell in item.candidates)
        + tuple(cell for item in answers for cell in item.cells)
    )
    return AutomaticPage(
        1,
        page_ref(),
        status,
        None if failed else 0,
        None if failed else "0.99",
        None if failed else "0.98",
        None if failed else (1000, 1400),
        None if failed else ("1",) * 9,
        None if failed else ("1",) * 9,
        student_id,
        answers,
        evidence,
        (ErrorInfo("PAGE_UNREADABLE", "error.page_unreadable"),) if failed else (),
    )


@pytest.mark.parametrize(
    "value",
    ["+1", "1.0", "01", "1e-1", "NaN", "Infinity", "0.0000000000001"],
)
def test_ratio_rect_rejects_noncanonical_decimal_values(value: str) -> None:
    with pytest.raises(ValueError):
        RatioRect(value, "0", "0.1", "0.1")


def test_ratio_rect_preserves_canonical_decimal_values() -> None:
    rect = RatioRect("0", "0.25", "1", "0.000000000001")

    assert tuple(Decimal(value) for value in (rect.x, rect.y, rect.w, rect.h)) == (
        Decimal("0"),
        Decimal("0.25"),
        Decimal("1"),
        Decimal("0.000000000001"),
    )


@pytest.mark.parametrize("args", [(0, 0, 0, 1), (0, 0, 1, -1), (True, 0, 1, 1)])
def test_pixel_rect_rejects_nonpositive_dimensions_and_boolean_coordinates(
    args: tuple[int, int, int, int],
) -> None:
    with pytest.raises(ValueError):
        PixelRect(*args)


def test_cell_evidence_accepts_one_identity_kind_only() -> None:
    digit = digit_evidence(3)
    choice = answer_evidence(7, 4)

    assert digit.digit == 3 and digit.question is None
    assert choice.question == 7 and choice.choice == 4 and choice.digit is None

    with pytest.raises(ValueError):
        CellEvidence(0, 1, 3, 1, None, None, None, False, CellStatus.NORMAL)
    with pytest.raises(ValueError):
        CellEvidence(0, None, None, None, None, None, None, False, CellStatus.NORMAL)
    with pytest.raises(ValueError):
        CellEvidence(0, 101, None, 1, None, None, None, False, CellStatus.NORMAL)


def test_id_cell_and_student_id_recognition_require_complete_consistent_columns() -> None:
    normal = id_cell("3")
    assert normal.selected_digit == "3"
    assert (
        StudentIdRecognition("33333333", StudentIdStatus.NORMAL, (normal,) * 8).value == "33333333"
    )

    with pytest.raises(ValueError):
        IdCell("3", FieldStatus.BLANK, normal.candidates)
    with pytest.raises(ValueError):
        IdCell("3", FieldStatus.NORMAL, normal.candidates[:-1])
    with pytest.raises(ValueError):
        StudentIdRecognition("33333333", StudentIdStatus.NORMAL, (normal,) * 7)
    with pytest.raises(ValueError):
        StudentIdRecognition(None, StudentIdStatus.NORMAL, (id_cell(None),) * 8)


@pytest.mark.parametrize(
    ("choices", "status"),
    [
        ((1, 1), AnswerStatus.MULTIPLE),
        ((2, 1), AnswerStatus.MULTIPLE),
        ((1,), AnswerStatus.BLANK),
        ((), AnswerStatus.NORMAL),
    ],
)
def test_answer_value_rejects_noncanonical_or_status_mismatched_choices(
    choices: tuple[int, ...], status: AnswerStatus
) -> None:
    with pytest.raises(ValueError):
        AnswerValue(choices, status)


def test_answer_key_answer_status_retains_exact_single_and_multiple_sets() -> None:
    for choices, answer_status in (((1,), AnswerStatus.NORMAL), ((2, 4), AnswerStatus.MULTIPLE)):
        entry = AnswerKeyEntry(
            1, AnswerValue(choices, answer_status), "1", KeyQuestionStatus.ANSWER
        )
        assert entry.answer.choices == choices

    for choices, answer_status in (((), AnswerStatus.ALL), ((), AnswerStatus.UNASKED)):
        with pytest.raises(ValueError, match="key status"):
            AnswerKeyEntry(1, AnswerValue(choices, answer_status), "1", KeyQuestionStatus.ANSWER)


def test_roster_rows_apply_issue_precedence_and_snapshot_grouping() -> None:
    issues = (RosterRowStatus.DUPLICATE_ID.value, RosterRowStatus.NAME_CONFLICT.value)
    first = RosterEntry(
        "row-1", 2, 0, "00000000", "00000000", "Kim", RosterRowStatus.DUPLICATE_ID, issues
    )
    second = RosterEntry(
        "row-2", 3, 1, "00000000", "00000000", "Lee", RosterRowStatus.DUPLICATE_ID, issues
    )
    snapshot = RosterSnapshot(
        1,
        RosterSnapshotKind.WORKBOOK,
        "roster.xlsx",
        "a" * 64,
        "Roster",
        "v1",
        (first, second),
        (),
    )

    assert snapshot.rows == (first, second)
    assert first.status is RosterRowStatus.DUPLICATE_ID

    with pytest.raises(ValueError, match="roster status"):
        RosterEntry(
            "row-1",
            2,
            0,
            "00000000",
            "00000000",
            "Kim",
            RosterRowStatus.NAME_CONFLICT,
            issues,
        )
    with pytest.raises(ValueError, match="roster group"):
        RosterSnapshot(
            1,
            RosterSnapshotKind.WORKBOOK,
            "roster.xlsx",
            "a" * 64,
            "Roster",
            "v1",
            (
                first,
                RosterEntry(
                    "row-2",
                    3,
                    1,
                    "00000000",
                    "00000000",
                    "Lee",
                    RosterRowStatus.DUPLICATE_ID,
                    (RosterRowStatus.DUPLICATE_ID.value,),
                ),
            ),
            (),
        )


def test_answer_recognition_requires_exactly_one_cell_for_each_choice() -> None:
    valid = answer(2)
    assert valid.value == AnswerValue((1,), AnswerStatus.NORMAL)

    with pytest.raises(ValueError):
        AnswerRecognition(2, valid.value, valid.cells[:-1])
    with pytest.raises(ValueError):
        AnswerRecognition(2, valid.value, valid.cells[:4] + (answer_evidence(3, 5),))


@pytest.mark.parametrize("status", [ProcessingStatus.FAILED, ProcessingStatus.UNPROCESSABLE])
def test_automatic_page_failed_discriminant_is_empty_unreadable_and_diagnosed(
    status: ProcessingStatus,
) -> None:
    complete = automatic_page()
    failed = automatic_page(status)

    assert len(complete.answers) == 100
    assert len(complete.evidence) == 580
    assert AutomaticPage.from_dict(complete.to_dict()) == complete
    assert AutomaticPage.from_dict(failed.to_dict()) == failed
    with pytest.raises(ValueError, match="wire fields"):
        AutomaticPage.from_dict(complete.to_dict() | {"unknown": True})
    with pytest.raises(ValueError, match="failed page"):
        AutomaticPage(
            1,
            failed.page_ref,
            status,
            0,
            None,
            None,
            None,
            None,
            None,
            failed.student_id,
            (),
            (),
            failed.errors,
        )
    with pytest.raises(ValueError, match="failed page"):
        AutomaticPage(
            1,
            failed.page_ref,
            status,
            None,
            None,
            None,
            None,
            None,
            None,
            StudentIdRecognition(None, StudentIdStatus.UNREADABLE, ()),
            (),
            (),
            (),
        )
    with pytest.raises(ValueError, match="failed page"):
        AutomaticPage(
            1,
            failed.page_ref,
            status,
            None,
            None,
            None,
            None,
            None,
            None,
            failed.student_id,
            (),
            (),
            (ErrorInfo("PAGE_WARNING", "warning.page_warning"),),
        )


@pytest.mark.parametrize(
    "status", [ProcessingStatus.PROCESSED, ProcessingStatus.NEEDS_MANUAL_REVIEW]
)
def test_automatic_page_processed_discriminant_requires_complete_recognition_and_evidence(
    status: ProcessingStatus,
) -> None:
    complete = automatic_page(status)

    with pytest.raises(ValueError, match="processed page recognition incomplete"):
        AutomaticPage(
            1,
            complete.page_ref,
            ProcessingStatus.PROCESSED,
            0,
            "0.99",
            "0.98",
            (1000, 1400),
            ("1",) * 9,
            ("1",) * 9,
            complete.student_id,
            complete.answers[:-1],
            complete.evidence,
        )
    with pytest.raises(ValueError, match="processed evidence"):
        AutomaticPage(
            1,
            complete.page_ref,
            ProcessingStatus.PROCESSED,
            0,
            "0.99",
            "0.98",
            (1000, 1400),
            ("1",) * 9,
            ("1",) * 9,
            complete.student_id,
            complete.answers,
            complete.evidence[:-1],
        )


def test_correction_draft_enforces_target_range_and_value_kind() -> None:
    identifier = IdCorrectionValue("1", FieldStatus.NORMAL)
    response = AnswerValue((1,), AnswerStatus.NORMAL)

    assert (
        CorrectionDraft("work-1", TargetKind.ID_CELL, 0, identifier, identifier, "수정").target_key
        == 0
    )
    assert (
        CorrectionDraft(
            "work-1", TargetKind.ANSWER_CELL, 100, response, response, "수정"
        ).target_key
        == 100
    )
    draft = CorrectionDraft("work-1", TargetKind.ANSWER_CELL, 100, response, response, "수정")
    assert CorrectionDraft.from_dict(draft.to_dict()) == draft
    with pytest.raises(ValueError, match="wire fields"):
        CorrectionDraft.from_dict(draft.to_dict() | {"unknown": True})

    with pytest.raises(ValueError):
        IdCorrectionValue("1", FieldStatus.BLANK)
    with pytest.raises(ValueError):
        IdCorrectionValue(None, FieldStatus.NORMAL)
    with pytest.raises(ValueError):
        CorrectionDraft("work-1", TargetKind.ID_CELL, 8, identifier, identifier, "수정")
    with pytest.raises(ValueError):
        CorrectionDraft("work-1", TargetKind.ANSWER_CELL, 1, identifier, identifier, "수정")
