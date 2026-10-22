"""A roster that cannot be read is explained in the teacher's terms, with the cell."""

from __future__ import annotations

from omr_grader.infrastructure.scan_runtime import _roster_failure


def _reason(code: str, field: str | None = None) -> str:
    return str(_roster_failure(code, field=field).errors[0].context["reason"])


def test_a_cell_problem_names_the_row_and_column() -> None:
    assert _reason("XLSX_CELL_TYPE", "B5").startswith("명단 5행 B칸(학번)을 읽지 못했습니다.")
    assert _reason("XLSX_CELL_TYPE", "C12").startswith("명단 12행 C칸(이름)을 읽지 못했습니다.")


def test_each_kind_of_problem_has_its_own_advice() -> None:
    assert "첫 줄이 '연번 · 학번 · 이름' 세 칸이 아닙니다" in _reason("XLSX_HEADERS_INVALID")
    assert "B3 칸에 수식이 있습니다" in _reason("XLSX_FORMULA_FORBIDDEN", "B3")
    assert "세 칸만 써야 합니다" in _reason("XLSX_DIMENSION_QUOTA")
    # Anything else keeps the general advice; every message says the roster is optional.
    assert _reason("XLSX_INVALID_PACKAGE").startswith("명단 파일을 읽지 못했습니다.")
    for code in ("XLSX_CELL_TYPE", "XLSX_HEADERS_INVALID", "XLSX_INVALID_PACKAGE"):
        assert _reason(code, "A2").endswith("명단 없이도 인식할 수 있습니다.")
