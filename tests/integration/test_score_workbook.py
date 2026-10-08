from dataclasses import replace
from hashlib import sha256

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
from omr_grader.domain.errors import Err, Ok
from omr_grader.workbooks.answer_key import ANSWER_KEY_HEADERS
from omr_grader.workbooks.response_import import parse_response_book
from omr_grader.workbooks.schemas import RESPONSE_HEADERS, RESPONSE_SHEET_NAME
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
        "순번",
        "학번",
        "이름",
        "총점",
        "석차",
        *(f"Q{number}" for number in range(1, 101)),
        "비고",
    )
    assert score.title == "채점결과"
    assert tuple(cell.value for cell in score[1]) == expected_score_headers
    assert score.max_column == 106 and score.max_row == 3
    assert final.title == "최종성적표"
    assert tuple(cell.value for cell in final[1]) == (
        *expected_score_headers,
        "수정여부",
        "수정문항",
        "확정일시",
    )
    assert final.max_column == 109 and final.max_row == 3
    assert [score.cell(row, 1).value for row in (2, 3)] == [1, 2]
    assert score.cell(2, 5).value == 1 and score.cell(2, 5).data_type == "n"
    assert score.cell(2, 4).value == 3.5 and score.cell(2, 4).data_type == "n"
    assert tuple(score.cell(2, column).value for column in range(6, 9)) == ("1,2", None, None)
    assert score.cell(2, 3).value == "'=수식아님"
    assert {score.cell(row, 106).value for row in (2, 3)} == {"중복확인필요"}
    assert final.cell(2, 107).value is True and final.cell(2, 107).data_type == "b"
    assert final.cell(2, 108).value == "학번1,Q2"
    assert final.cell(2, 109).value == "2026-07-28T01:02:03.123456Z"
    assert score_book.sheetnames == SCORE_SHEETS
    outcomes = score_book["결과OX"]
    assert tuple(cell.value for cell in outcomes[1]) == expected_score_headers
    assert [outcomes.cell(2, column).value for column in (1, 2, 4, 5)] == [1, "12345678", 3.5, 1]
    assert tuple(outcomes.cell(2, column).value for column in range(6, 9)) == ("O", "O", "제외")
    responses = score_book[RESPONSE_SHEET_NAME]
    assert tuple(cell.value for cell in responses[1]) == RESPONSE_HEADERS
    assert tuple(responses.cell(2, column).value for column in range(1, 8)) == (
        1,
        "row",
        "12345678",
        "'=수식아님",
        "12",
        None,
        None,
    )
    answer_key = score_book["정답표"]
    assert tuple(cell.value for cell in answer_key[1]) == ANSWER_KEY_HEADERS
    assert answer_key.max_row == 101
    assert final_book.sheetnames == ["최종성적표", *SCORE_SHEETS[1:]]
    assert {property.name: property.value for property in score_book.custom_doc_props} == {
        "schema": "1",
        "session_id": "session-1",
        "revision": "2",
        "manifest_sha256": "b" * 64,
    }
    assert score_path.name == "260728_100203_채점결과_수학_1.xlsx"
    assert final_path.name == "260728_100203_최종성적표_수학_1.xlsx"


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


SCORE_SHEETS = [
    "채점결과",
    "결과OX",
    "문항 분석",
    RESPONSE_SHEET_NAME,
    "판독 근거",
    "정답표",
    "색 설명",
]
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


# Q1 is column 6, so Qn sits in column n + 5 and the total in column 4.
_WRONG_ROW = {7: PINK, 9: PINK, 10: PINK, 12: PINK}
_REVIEW_ROW = {4: YELLOW, 8: YELLOW, 9: YELLOW, 10: YELLOW, 14: PINK, 22: YELLOW}


def test_score_book_paints_wrong_answers_pink_and_unconfirmed_answers_yellow(tmp_path):
    book = _scenario(write_score_book, tmp_path)

    for title in ("채점결과", "결과OX"):
        sheet = book[title]
        assert [sheet.cell(row, 2).value for row in (2, 3, 4)] == [
            "20240000",
            "20240001",
            "20240002",
        ]
        assert _painted(sheet, 2) == {}
        assert _painted(sheet, 3) == _WRONG_ROW
        assert _painted(sheet, 4) == _REVIEW_ROW
        assert [sheet.cell(row, 4).value for row in (2, 3, 4)] == [20, 16, None]
        assert [sheet.cell(row, 5).value for row in (2, 3, 4)] == [1, 2, None]


def test_outcome_sheet_says_how_each_answer_was_scored(tmp_path):
    sheet = _scenario(write_score_book, tmp_path)["결과OX"]

    # Painting never changes what a cell says.
    assert [sheet.cell(3, column).value for column in range(6, 13)] == list("OXOXXOX")
    assert sheet.cell(3, 55).value == "제외"
    assert [sheet.cell(4, column).value for column in (8, 9, 10, 14, 22)] == [
        "검토",
        "검토",
        "검토",
        "X",
        "검토",
    ]


def test_score_sheet_shows_the_chosen_answers(tmp_path):
    sheet = _scenario(write_score_book, tmp_path)["채점결과"]

    assert [sheet.cell(2, column).value for column in range(6, 12)] == [1, 2, 1, 3, "1,2", 1]
    # Wrong choice, blank (Q3 accepts anything), blank on an asked question, several marks.
    assert [sheet.cell(3, column).value for column in range(6, 13)] == [
        1,
        3,
        None,
        None,
        "1,2,3",
        1,
        "1,2",
    ]
    # An unconfirmed Q50 is still shown, although the key does not ask it.
    assert sheet.cell(3, 55).value == 1
    assert [sheet.cell(4, column).value for column in (8, 9, 10, 14, 22)] == [
        2,
        None,
        "1,2",
        2,
        4,
    ]


def test_sheets_number_the_students_so_the_last_number_is_the_head_count(tmp_path):
    book = _scenario(write_final_book, tmp_path)

    for title in ("최종성적표", "결과OX"):
        assert book[title].max_row == 4
        assert [book[title].cell(row, 1).value for row in range(1, 5)] == ["순번", 1, 2, 3]


def test_score_book_names_the_unconfirmed_questions_in_the_note(tmp_path):
    sheet = _scenario(write_score_book, tmp_path)["채점결과"]

    # Q50 is unconfirmed too, but the key does not ask it, so the score and note are untouched.
    assert [sheet.cell(row, 106).value for row in (2, 3, 4)] == [None, None, "확인 필요: 3~5, 17번"]


def test_score_book_appends_the_review_note_to_the_duplicate_id_note(tmp_path):
    clean = _student("wi_clean", "20240002", {})
    review = _student("wi_review", "20240002", {3: _uncertain(2), 17: _uncertain(4)})

    sheet = _write(write_score_book, tmp_path, (review, clean))["채점결과"]

    # Same ID and no names: the rows keep the order they arrived in.
    assert [sheet.cell(row, 106).value for row in (2, 3)] == [
        "중복확인필요 / 확인 필요: 3, 17번",
        "중복확인필요",
    ]
    assert _painted(sheet, 2) == {4: YELLOW, 8: YELLOW, 22: YELLOW}


def test_rows_are_listed_by_name_then_students_without_a_name_by_id(tmp_path):
    key = _scoring_key()
    students = (
        _student("wi_c", "20240003", {}),
        _student("wi_unnamed", "20240001", {}),
        _student("wi_a", "20240009", {2: _marked(3)}),
        _student("wi_b", "20240002", {}),
    )
    path = write_score_book(
        tmp_path,
        exam_name="시험",
        committed_at="2026-07-28T01:02:03.123456Z",
        session_id="session-1",
        revision=1,
        manifest_sha256="b" * 64,
        responses=students,
        key=key,
        scores=score_effective(ScoreInput(students, key)),
        names_by_student_id={"20240003": "다현", "20240009": "가윤", "20240002": "나래"},
    )
    book = load_workbook(path)

    for sheet, name_column in (
        (book["채점결과"], 3),
        (book["결과OX"], 3),
        (book[RESPONSE_SHEET_NAME], 4),
    ):
        assert [sheet.cell(row, name_column).value for row in range(2, 6)] == [
            "가윤",
            "나래",
            "다현",
            None,
        ]
    # The rank still says who scored best.
    assert [book["채점결과"].cell(row, 5).value for row in range(2, 6)] == [4, 1, 1, 1]
    assert [book["채점결과"].cell(row, 1).value for row in range(2, 6)] == [1, 2, 3, 4]


def test_response_sheet_of_the_score_book_is_importable_once_reviews_are_cleared(tmp_path):
    book = _scenario(write_score_book, tmp_path)
    sheet = book[RESPONSE_SHEET_NAME]

    # Only unconfirmed answers are yellow here; the score colors stay on the score sheet.
    assert _painted(sheet, 2) == {}
    assert _painted(sheet, 3) == {54: YELLOW}
    assert _painted(sheet, 4) == {7: YELLOW, 8: YELLOW, 9: YELLOW, 21: YELLOW}
    assert [sheet.cell(3, column).value for column in (6, 7, 8, 9)] == ["3", None, None, "123"]
    assert [sheet.cell(4, column).value for column in (7, 8, 9, 21)] == ["2", None, "12", "4"]
    assert [sheet.cell(row, 105).value for row in (2, 3, 4)] == [
        None,
        "확인 필요: 50번",
        "확인 필요: 3~5, 17번",
    ]
    path = next(tmp_path.glob("*_채점결과_*.xlsx"))

    def parse():
        payload = path.read_bytes()
        with path.open("rb") as source:
            return parse_response_book(
                source,
                sheet_name=RESPONSE_SHEET_NAME,
                session_id="import-session",
                source_sha256=sha256(payload).hexdigest(),
            )

    refused = parse()
    assert isinstance(refused, Err)
    assert refused.errors[0].code == "XLSX_REVIEW_PENDING"
    for row in (3, 4):
        sheet.cell(row, 105).value = None
    book.save(path)
    parsed = parse()
    assert isinstance(parsed, Ok)
    assert [row.raw_student_id for row in parsed.value] == ["20240000", "20240001", "20240002"]


def test_final_book_uses_the_same_colors_without_touching_its_extra_columns(tmp_path):
    sheet = _scenario(write_final_book, tmp_path)["최종성적표"]

    assert _painted(sheet, 2) == {}
    assert _painted(sheet, 3) == _WRONG_ROW
    assert _painted(sheet, 4) == _REVIEW_ROW
    assert sheet.cell(4, 106).value == "확인 필요: 3~5, 17번"
    assert [sheet.cell(1, column).value for column in (107, 108, 109)] == [
        "수정여부",
        "수정문항",
        "확정일시",
    ]
    assert sheet.max_column == 109


@pytest.mark.parametrize(
    ("writer", "title"), ((write_score_book, "채점결과"), (write_final_book, "최종성적표"))
)
def test_score_and_final_books_freeze_identity_columns_and_bold_the_header(tmp_path, writer, title):
    book = _scenario(writer, tmp_path)

    for name, freeze in ((title, "F2"), ("결과OX", "F2"), (RESPONSE_SHEET_NAME, "E2")):
        sheet = book[name]
        assert sheet.freeze_panes == freeze
        assert all(cell.font.b for cell in sheet[1])
        assert not any(cell.font.b for row in sheet.iter_rows(min_row=2) for cell in row)
    assert book.active.title == title


@pytest.mark.parametrize("writer", (write_score_book, write_final_book))
def test_score_and_final_books_explain_the_colors_on_a_legend_sheet(tmp_path, writer):
    book = _scenario(writer, tmp_path)

    assert book.sheetnames[-1] == "색 설명"
    legend = book["색 설명"]
    rows = [[cell.value for cell in row] for row in legend.iter_rows()]
    assert [row[0] for row in rows] == [
        "분홍",
        "노랑",
        "색 없음",
        "노랑 (문항 분석)",
        "노랑 (판독 근거)",
        "전원",
    ]
    assert all(term in rows[0][1] for term in ("오답", "무응답", "중복 표기"))
    assert "확인 필요" in rows[1][1] and "채점하지 않" in rows[1][1]
    assert [_painted(legend, row) for row in range(1, 7)] == [
        {1: PINK},
        {1: YELLOW},
        {},
        {1: YELLOW},
        {1: YELLOW},
        {},
    ]
    assert "정답표의 '전원' = 모든 학생 정답 처리" in rows[5][1]
