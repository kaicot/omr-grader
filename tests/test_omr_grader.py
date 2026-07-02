import sys
import os
import csv
import tempfile

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


def test_load_answer_key_csv():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "key.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["문항번호", "정답"])
            for q in range(1, 51):
                w.writerow([q, (q % 5) + 1])
        key = og.load_answer_key(path)
        assert len(key) == 50
        assert key[1] == 2
        assert key[50] == 1


def test_load_answer_key_missing_question_raises():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "key.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["문항번호", "정답"])
            w.writerow([1, 1])
            w.writerow([3, 2])  # 2번이 빠짐 -> 연속되지 않음
        try:
            og.load_answer_key(path)
            assert False, "ValueError가 발생해야 함"
        except ValueError:
            pass


ALL_TESTS = [
    test_answer_bubble_center_q1_option1,
    test_answer_bubble_center_q21_option1,
    test_answer_bubble_center_q100_option5,
    test_id_bubble_center_col0_digit0,
    test_id_bubble_center_col7_digit9,
    test_load_answer_key_csv,
    test_load_answer_key_missing_question_raises,
]

if __name__ == "__main__":
    for t in ALL_TESTS:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(ALL_TESTS)} tests passed")
