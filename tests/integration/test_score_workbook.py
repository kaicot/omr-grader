from dataclasses import replace

import pytest
from openpyxl import load_workbook
from tests.unit.test_grading import _key, _response
from tests.unit.test_response_book import YELLOW, _painted, _uncertain

import omr_grader.workbooks.score_book as score_book_module
from omr_grader.application.dto import ScoreInput
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    KeyQuestionStatus,
    SourceKind,
    StudentIdStatus,
)
from omr_grader.domain.grading import score_effective
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    EffectiveResponse,
)
from omr_grader.workbooks.score_book import (
    score_filename,
    write_final_book,
    write_score_book,
)


def test_score_and_final_workbooks_have_exact_projection_shape(tmp_path):
    key = _key()
    first = replace(
        _response("wi_first", (1, 2)),
        corrected_targets=("id_cell:0", "answer_cell:2"),
    )
    duplicate = _response("wi_duplicate", (1,))
    scores = score_effective(ScoreInput((first, duplicate), key))
    common = dict(
        exam_name="수학/1",
        committed_at="2026-07-28T01:02:03.123456Z",
        session_id="session-1",
        revision=2,
        manifest_sha256="b" * 64,
        responses=(first, duplicate),
        key=key,
        scores=scores,
        names_by_student_id={"12345678": "=수식아님"},
    )

    score_path = write_score_book(tmp_path, **common)
    final_path = write_final_book(tmp_path, **common)
    score_book = load_workbook(score_path, data_only=False)
    final_book = load_workbook(final_path, data_only=False)
    score = score_book.active
    final = final_book.active

    expected_score_headers = (
        "석차",
        "학번",
        "이름",
        "총점",
        *(f"Q{number}" for number in range(1, 101)),
        "비고",
    )
    assert score.title == "채점결과"
    assert tuple(cell.value for cell in score[1]) == expected_score_headers
    assert score.max_column == 105 and score.max_row == 3
    assert final.title == "최종성적표"
    assert tuple(cell.value for cell in final[1]) == (
        *expected_score_headers,
        "수정여부",
        "수정문항",
        "확정일시",
    )
    assert final.max_column == 108 and final.max_row == 3
    assert score.cell(2, 1).value == 1 and score.cell(2, 1).data_type == "n"
    assert score.cell(2, 4).value == 3.5 and score.cell(2, 4).data_type == "n"
    assert tuple(score.cell(2, column).value for column in range(5, 8)) == ("O", "O", "제외")
    assert score.cell(2, 3).value == "'=수식아님"
    assert {score.cell(row, 105).value for row in (2, 3)} == {"중복확인필요"}
    assert final.cell(2, 106).value is True and final.cell(2, 106).data_type == "b"
    assert final.cell(2, 107).value == "학번1,Q2"
    assert final.cell(2, 108).value == "2026-07-28T01:02:03.123456Z"
    assert score_book.sheetnames == ["채점결과", "응답내역", "색 설명"]
    responses = score_book["응답내역"]
    assert tuple(cell.value for cell in responses[1]) == expected_score_headers
    assert tuple(responses.cell(2, column).value for column in range(5, 8)) == (
        "1,2",
        None,
        None,
    )
    assert final_book.sheetnames == ["최종성적표", "응답내역", "색 설명"]
    assert {property.name: property.value for property in score_book.custom_doc_props} == {
        "schema": "1",
        "session_id": "session-1",
        "revision": "2",
        "manifest_sha256": "b" * 64,
    }
    assert score_path.name == "02_score_수학_1_260728_100203_채점결과.xlsx"
    assert final_path.name == "03_final_수학_1_260728_100203_최종성적표.xlsx"


def test_score_book_rejects_collisions_and_leaves_no_partial_file(tmp_path, monkeypatch):
    key = _key()
    response = _response("wi", (1, 2))
    common = dict(
        exam_name="시험",
        committed_at="2026-07-28T01:02:03.123456Z",
        session_id="session-1",
        revision=1,
        manifest_sha256="b" * 64,
        responses=(response,),
        key=key,
        scores=score_effective(ScoreInput((response,), key)),
    )

    original = write_score_book(tmp_path, **common)
    with pytest.raises(FileExistsError):
        write_score_book(tmp_path, **common)
    assert original.exists()

    def fail_save(self, filename):
        raise OSError("simulated write failure")

    monkeypatch.setattr(score_book_module.Workbook, "save", fail_save)
    failed_destination = tmp_path / "failed"
    with pytest.raises(OSError, match="simulated write failure"):
        write_score_book(failed_destination, **common)
    assert not (failed_destination / score_filename("시험", common["committed_at"])).exists()
    assert not tuple(failed_destination.glob(".*.tmp"))


# Fill and font colors (ARGB) of an answer scored as wrong; YELLOW is shared with the response book.
PINK = ("FFFFC7CE", "FF9C0006")
_CORRECT = {
    1: (1,),
    2: (2,),
    3: (1,),
    4: (3,),
    5: (1, 2),
    **{number: (1,) for number in range(6, 21)},
}


def _marked(*choices: int) -> AnswerValue:
    """What the reader reports for these marks: blank, one choice or several."""
    status = (
        AnswerStatus.BLANK
        if not choices
        else AnswerStatus.NORMAL
        if len(choices) == 1
        else AnswerStatus.MULTIPLE
    )
    return AnswerValue(choices, status)


def _scoring_key() -> AnswerKeySnapshot:
    """Q1=1, Q2=2, Q3=anything, Q4=3, Q5=1 and 2 together, Q6..Q20=1; Q21..Q100 are not asked."""
    unasked = AnswerValue((), AnswerStatus.UNASKED)
    entries = []
    for number in range(1, 101):
        if number == 3:
            entry = AnswerKeyEntry(
                number, AnswerValue((), AnswerStatus.ALL), "1", KeyQuestionStatus.ALL
            )
        elif number in _CORRECT:
            entry = AnswerKeyEntry(
                number, _marked(*_CORRECT[number]), "1", KeyQuestionStatus.ANSWER
            )
        else:
            entry = AnswerKeyEntry(number, unasked, "0", KeyQuestionStatus.UNASKED)
        entries.append(entry)
    return AnswerKeySnapshot(
        1, AnswerKeySnapshotKind.WORKBOOK, "key.xlsx", "a" * 64, "정답", "v1", tuple(entries), ()
    )


def _student(
    work_item_id: str, student_id: str, changes: dict[int, AnswerValue]
) -> EffectiveResponse:
    """Answers every asked question correctly except where the changes say otherwise."""
    answers = tuple(
        changes[number] if number in changes else _marked(*_CORRECT.get(number, ()))
        for number in range(1, 101)
    )
    return EffectiveResponse(
        work_item_id,
        SourceKind.IMPORTED_XLSX,
        "row",
        student_id,
        StudentIdStatus.NORMAL,
        answers,
    )


def _write(writer, tmp_path, responses):
    key = _scoring_key()
    path = writer(
        tmp_path,
        exam_name="시험",
        committed_at="2026-07-28T01:02:03.123456Z",
        session_id="session-1",
        revision=1,
        manifest_sha256="b" * 64,
        responses=responses,
        key=key,
        scores=score_effective(ScoreInput(responses, key)),
    )
    return load_workbook(path)


def _scenario(writer, tmp_path):
    """Three students: one perfect, one with every kind of wrong answer, one to be reviewed."""
    perfect = _student("wi_perfect", "20240000", {})
    wrong = _student(
        "wi_wrong",
        "20240001",
        {
            2: _marked(3),  # a wrong choice
            3: _marked(),  # blank, but Q3 accepts anything
            4: _marked(),  # blank on an asked question
            5: _marked(1, 2, 3),  # several marks where the key wants 1 and 2
            7: _marked(1, 2),  # several marks
            50: _uncertain(1),  # unconfirmed, but the key does not ask Q50
        },
    )
    review = _student(
        "wi_review",
        "20240002",
        {
            3: _uncertain(2),  # Q3 accepts anything, but an unconfirmed mark holds the score back
            4: _uncertain(),
            5: _uncertain(1, 2),
            9: _marked(2),  # wrong as well
            17: _uncertain(4),
        },
    )
    # Not in rank order on purpose: the sheet lists perfect, wrong, then the unscored review row.
    return _write(writer, tmp_path, (review, perfect, wrong))


# Q1 is column 5, so Qn sits in column n + 4 and the total in column 4.
_WRONG_ROW = {6: PINK, 8: PINK, 9: PINK, 11: PINK}
_REVIEW_ROW = {4: YELLOW, 7: YELLOW, 8: YELLOW, 9: YELLOW, 13: PINK, 21: YELLOW}


def test_score_book_paints_wrong_answers_pink_and_unconfirmed_answers_yellow(tmp_path):
    sheet = _scenario(write_score_book, tmp_path)["채점결과"]

    assert [sheet.cell(row, 2).value for row in (2, 3, 4)] == ["20240000", "20240001", "20240002"]
    assert _painted(sheet, 2) == {}
    assert _painted(sheet, 3) == _WRONG_ROW
    assert _painted(sheet, 4) == _REVIEW_ROW
    # Painting never changes what a cell says.
    assert [sheet.cell(3, column).value for column in range(5, 12)] == list("OXOXXOX")
    assert sheet.cell(3, 54).value == "제외"
    assert [sheet.cell(4, column).value for column in (7, 8, 9, 13, 21)] == [
        "검토",
        "검토",
        "검토",
        "X",
        "검토",
    ]
    assert [sheet.cell(row, 4).value for row in (2, 3, 4)] == [20, 16, None]


def test_score_book_names_the_unconfirmed_questions_in_the_note(tmp_path):
    sheet = _scenario(write_score_book, tmp_path)["채점결과"]

    # Q50 is unconfirmed too, but the key does not ask it, so the score and note are untouched.
    assert [sheet.cell(row, 105).value for row in (2, 3, 4)] == [None, None, "확인 필요: 3~5, 17번"]


def test_score_book_appends_the_review_note_to_the_duplicate_id_note(tmp_path):
    clean = _student("wi_clean", "20240002", {})
    review = _student("wi_review", "20240002", {3: _uncertain(2), 17: _uncertain(4)})

    sheet = _write(write_score_book, tmp_path, (review, clean))["채점결과"]

    assert [sheet.cell(row, 105).value for row in (2, 3)] == [
        "중복확인필요",
        "중복확인필요 / 확인 필요: 3, 17번",
    ]
    assert _painted(sheet, 3) == {4: YELLOW, 7: YELLOW, 21: YELLOW}


def test_response_sheet_of_the_score_book_uses_the_same_colors_and_keeps_its_values(tmp_path):
    sheet = _scenario(write_score_book, tmp_path)["응답내역"]

    assert _painted(sheet, 2) == {}
    assert _painted(sheet, 3) == _WRONG_ROW
    assert _painted(sheet, 4) == _REVIEW_ROW
    assert [sheet.cell(3, column).value for column in (6, 7, 8, 9)] == ["3", None, None, "1,2,3"]
    assert [sheet.cell(4, column).value for column in (7, 8, 9, 21)] == ["2", None, "1,2", "4"]
    assert sheet.cell(4, 105).value == "확인 필요: 3~5, 17번"


def test_final_book_uses_the_same_colors_without_touching_its_extra_columns(tmp_path):
    sheet = _scenario(write_final_book, tmp_path)["최종성적표"]

    assert _painted(sheet, 2) == {}
    assert _painted(sheet, 3) == _WRONG_ROW
    assert _painted(sheet, 4) == _REVIEW_ROW
    assert sheet.cell(4, 105).value == "확인 필요: 3~5, 17번"
    assert [sheet.cell(1, column).value for column in (106, 107, 108)] == [
        "수정여부",
        "수정문항",
        "확정일시",
    ]
    assert sheet.max_column == 108


@pytest.mark.parametrize(
    ("writer", "title"), ((write_score_book, "채점결과"), (write_final_book, "최종성적표"))
)
def test_score_and_final_books_freeze_identity_columns_and_bold_the_header(tmp_path, writer, title):
    book = _scenario(writer, tmp_path)

    for sheet in (book[title], book["응답내역"]):
        assert sheet.freeze_panes == "E2"
        assert all(cell.font.b for cell in sheet[1])
        assert not any(cell.font.b for row in sheet.iter_rows(min_row=2) for cell in row)
    assert book.active.title == title


@pytest.mark.parametrize("writer", (write_score_book, write_final_book))
def test_score_and_final_books_explain_the_colors_on_a_legend_sheet(tmp_path, writer):
    book = _scenario(writer, tmp_path)

    assert book.sheetnames[-1] == "색 설명"
    legend = book["색 설명"]
    rows = [[cell.value for cell in row] for row in legend.iter_rows()]
    assert [row[0] for row in rows] == ["분홍", "노랑", "색 없음"]
    assert all(term in rows[0][1] for term in ("오답", "무응답", "중복 표기"))
    assert "확인 필요" in rows[1][1] and "채점하지 않" in rows[1][1]
    assert (_painted(legend, 1), _painted(legend, 2), _painted(legend, 3)) == (
        {1: PINK},
        {1: YELLOW},
        {},
    )
