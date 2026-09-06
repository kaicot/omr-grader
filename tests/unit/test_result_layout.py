from omr_grader.infrastructure.result_layout import (
    COORDINATE_DIR,
    OCR_IMAGE_DIR,
    REVIEW_DIR,
    SCORE_IMAGE_DIR,
    result_base_name,
)


def test_result_layout_uses_safe_exam_name_and_korean_timestamp() -> None:
    base = result_base_name("26-2 생리학/중간고사", "2026-07-26T05:30:00.000000Z")

    assert base == "26-2_생리학_중간고사_260726_143000"
    assert OCR_IMAGE_DIR == "01_인식결과_이미지"
    assert SCORE_IMAGE_DIR == "02_채점결과_이미지"
    assert COORDINATE_DIR == "좌표데이터"
    assert REVIEW_DIR == "수동확인필요"
