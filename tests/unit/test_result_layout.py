import pytest

from omr_grader.infrastructure.result_layout import (
    COORDINATE_DIR,
    OCR_IMAGE_DIR,
    REVIEW_DIR,
    SCORE_IMAGE_DIR,
    answer_key_filename,
    response_filename,
    result_base_name,
    result_workbook_filename,
    result_workbook_kind,
)


def test_result_layout_uses_safe_exam_name_and_korean_timestamp() -> None:
    base = result_base_name("26-2 생리학/중간고사", "2026-07-26T05:30:00.000000Z")

    assert base == "26-2_생리학_중간고사_260726_143000"
    assert OCR_IMAGE_DIR == "01인식결과이미지"
    assert SCORE_IMAGE_DIR == "02채점결과이미지"
    assert COORDINATE_DIR == "좌표데이터"
    assert REVIEW_DIR == "수동확인필요"


def test_result_workbook_names_put_the_stamp_and_kind_before_the_exam_name() -> None:
    created = "2026-10-07T06:57:03.000000Z"

    assert result_workbook_filename("채점결과", "26_1졸업고사p1", created) == (
        "261007_155703_채점결과_26_1졸업고사p1.xlsx"
    )
    assert response_filename("26-2 생리학/중간고사", created) == (
        "261007_155703_응답결과_26-2_생리학_중간고사.xlsx"
    )
    assert answer_key_filename("", created) == "261007_155703_정답표_시험.xlsx"
    with pytest.raises(ValueError):
        result_workbook_filename("01_ocr", "시험", created)


@pytest.mark.parametrize("kind", ("응답결과", "채점결과", "최종성적표", "정답표"))
def test_result_workbook_kind_reads_the_third_field(kind: str) -> None:
    name = result_workbook_filename(kind, "26_1졸업고사_p1", "2026-10-07T06:57:03.000000Z")

    assert result_workbook_kind(name) == kind


@pytest.mark.parametrize(
    "name",
    (
        "01_ocr_시험_260730_120000_응답결과.xlsx",
        "02_score_시험_260730_120000_채점결과.xlsx",
        "정답표_시험_260730_120000.xlsx",
        "260730_120000_메모_시험.xlsx",
        "260730_120000_채점결과_시험.pdf",
        "260730_120000_채점결과_.xlsx",
        "artifacts/260730_120000_채점결과_시험.xlsx",
        "정답표원본",
        "01원본스캔",
        "session.json",
    ),
)
def test_result_workbook_kind_rejects_other_names(name: str) -> None:
    assert result_workbook_kind(name) is None
