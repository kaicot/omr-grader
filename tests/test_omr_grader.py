import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import omr_grader as og


def test_answer_bubble_center_q1_option1():
    x, y = og.answer_bubble_center_pt(1, 1)
    assert abs(x - 244.68) < 0.1, f"x={x}"
    assert abs(y - 83.81) < 0.1, f"y={y}"


def test_answer_bubble_center_q21_option1():
    # 21번은 두 번째 블록의 1행(행 인덱스 0)이어야 함
    x, y = og.answer_bubble_center_pt(21, 1)
    assert abs(x - 361.32) < 0.1, f"x={x}"
    assert abs(y - 83.81) < 0.1, f"y={y}"


def test_answer_bubble_center_q100_option5():
    # 100번은 다섯 번째 블록의 마지막 행(행 인덱스 19)이어야 함
    x, y = og.answer_bubble_center_pt(100, 5)
    assert abs(x - (705.72 + 4 * 19.44 + 11.04 / 2)) < 0.1, f"x={x}"
    assert abs(y - (78.29 + 19 * 23.7789 + 11.04 / 2)) < 0.1, f"y={y}"


def test_id_bubble_center_col0_digit0():
    x, y = og.id_bubble_center_pt(0, 0)
    assert abs(x - (48.24 + 11.04 / 2)) < 0.1, f"x={x}"
    assert abs(y - (297.32 + 11.04 / 2)) < 0.1, f"y={y}"


def test_id_bubble_center_col7_digit9():
    x, y = og.id_bubble_center_pt(7, 9)
    assert abs(x - (48.24 + 7 * 19.8 + 11.04 / 2)) < 0.1, f"x={x}"
    assert abs(y - (297.32 + 9 * 18.7033 + 11.04 / 2)) < 0.1, f"y={y}"


ALL_TESTS = [
    test_answer_bubble_center_q1_option1,
    test_answer_bubble_center_q21_option1,
    test_answer_bubble_center_q100_option5,
    test_id_bubble_center_col0_digit0,
    test_id_bubble_center_col7_digit9,
]

if __name__ == "__main__":
    for t in ALL_TESTS:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(ALL_TESTS)} tests passed")
