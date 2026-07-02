"""OMR 답안지(OCR100.pdf 양식) 스캔 채점 프로그램."""

import csv
import os

import cv2
import fitz
import numpy as np
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import PatternFill

PAGE_W_PT = 841.0
PAGE_H_PT = 595.0
ZOOM = 3.0

BLOCK_X_STARTS = [239.16, 355.80, 472.44, 589.08, 705.72]
BUBBLE_X_PITCH = 19.44
BUBBLE_SIZE = 11.04
ROW_Y_START = 78.29
ROW_Y_PITCH = 23.7789

ID_COL_X_START = 48.24
ID_COL_PITCH = 19.8
ID_ROW_Y_START = 297.32
ID_ROW_PITCH = 18.7033

TABLE_BORDER_PT = (215.285, 39.579, 797.847, 546.666)

DARK_THRESHOLD = 128
FILL_RATIO_MARK = 0.35


def answer_bubble_center_pt(qnum, option):
    """qnum: 1~100, option: 1~5. 반환: (x, y) pt 좌표."""
    block = (qnum - 1) // 20
    row = (qnum - 1) % 20
    x = BLOCK_X_STARTS[block] + (option - 1) * BUBBLE_X_PITCH + BUBBLE_SIZE / 2
    y = ROW_Y_START + row * ROW_Y_PITCH + BUBBLE_SIZE / 2
    return x, y


def id_bubble_center_pt(col, digit):
    """col: 0~7 (학번 자리), digit: 0~9. 반환: (x, y) pt 좌표."""
    x = ID_COL_X_START + col * ID_COL_PITCH + BUBBLE_SIZE / 2
    y = ID_ROW_Y_START + digit * ID_ROW_PITCH + BUBBLE_SIZE / 2
    return x, y


def load_answer_key(path):
    """정답표(xlsx/csv, 문항번호+정답 2열)를 읽어 {문항번호: 정답} dict로 반환.
    문항번호는 1부터 연속되어야 하며, 이 dict의 길이가 이번 시험의 문항 수가 된다."""
    ext = os.path.splitext(path)[1].lower()
    rows = []
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # 헤더 skip
            for row in reader:
                if not row:
                    continue
                rows.append((int(row[0]), int(row[1])))
    else:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] is None:
                continue
            rows.append((int(row[0]), int(row[1])))

    key = dict(rows)
    expected = set(range(1, len(key) + 1))
    if set(key.keys()) != expected:
        raise ValueError(
            f"정답표 문항번호가 1~{len(key)}로 연속되어 있지 않습니다: {sorted(key.keys())}"
        )
    return key


def load_scan_images(paths):
    """이미지 파일(jpg/png) 또는 멀티페이지 PDF 경로 목록을 받아
    (라벨, BGR 이미지) 목록으로 변환. PDF는 페이지마다 한 학생으로 분리."""
    results = []
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            doc = fitz.open(p)
            base = os.path.splitext(os.path.basename(p))[0]
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), alpha=False)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, 3
                )
                img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                label = f"{base}_p{i + 1}"
                results.append((label, img_bgr))
        else:
            img_bgr = cv2.imread(p)
            if img_bgr is None:
                raise ValueError(f"이미지를 읽을 수 없습니다: {p}")
            label = os.path.basename(p)
            results.append((label, img_bgr))
    return results


def sample_fill_ratio(gray_img, cx_px, cy_px, radius_px):
    """(cx_px, cy_px) 중심, radius_px 반지름의 원형 영역에서
    DARK_THRESHOLD보다 어두운 픽셀의 비율(0~1)을 반환."""
    x0 = max(int(cx_px - radius_px), 0)
    x1 = min(int(cx_px + radius_px) + 1, gray_img.shape[1])
    y0 = max(int(cy_px - radius_px), 0)
    y1 = min(int(cy_px + radius_px) + 1, gray_img.shape[0])
    patch = gray_img[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0

    yy, xx = np.ogrid[: patch.shape[0], : patch.shape[1]]
    local_cx = cx_px - x0
    local_cy = cy_px - y0
    mask = (xx - local_cx) ** 2 + (yy - local_cy) ** 2 <= radius_px ** 2
    pixels = patch[mask]
    if pixels.size == 0:
        return 0.0
    dark = np.count_nonzero(pixels < DARK_THRESHOLD)
    return dark / pixels.size


def detect_marked_index(fill_ratios, threshold=FILL_RATIO_MARK):
    """채움 비율 리스트에서 마킹된 칸의 0-based 인덱스를 판정.
    미응답이면 None, 정확히 하나 마킹되면 그 인덱스, 두 개 이상이면 인덱스 리스트."""
    marked = [i for i, r in enumerate(fill_ratios) if r >= threshold]
    if len(marked) == 0:
        return None
    if len(marked) == 1:
        return marked[0]
    return marked


class AlignmentError(Exception):
    pass


def _order_corners(pts):
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).flatten()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(diff)]
    bl = pts[np.argmax(diff)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def find_table_border(gray_img):
    """스캔 이미지에서 답안표를 감싸는 굵은 외곽 테두리 사각형의
    네 모서리(tl, tr, br, bl)를 검출. 못 찾으면 None."""
    blur = cv2.GaussianBlur(gray_img, (5, 5), 0)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = np.ones((5, 5), np.uint8)
    dilated = cv2.dilate(binary, kernel, iterations=2)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    target_ratio = (TABLE_BORDER_PT[2] - TABLE_BORDER_PT[0]) / (
        TABLE_BORDER_PT[3] - TABLE_BORDER_PT[1]
    )
    img_area = gray_img.shape[0] * gray_img.shape[1]

    best = None
    best_area = 0
    for c in contours:
        area = cv2.contourArea(c)
        if area < img_area * 0.15:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        pts = approx.reshape(4, 2).astype(np.float32)
        ordered = _order_corners(pts)
        w = np.linalg.norm(ordered[1] - ordered[0])
        h = np.linalg.norm(ordered[3] - ordered[0])
        if h == 0:
            continue
        ratio_error = abs((w / h) - target_ratio) / target_ratio
        if ratio_error > 0.15:
            continue
        if area > best_area:
            best_area = area
            best = ordered
    return best


def align_sheet(img_bgr):
    """스캔 이미지를 답안표 테두리 기준으로 투시 보정해
    (PAGE_H_PT*ZOOM, PAGE_W_PT*ZOOM) 크기의 정렬된 이미지로 반환."""
    try:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        corners = find_table_border(gray)
        if corners is None:
            raise AlignmentError("답안표 테두리를 찾지 못했습니다")

        dst = np.array(
            [
                [TABLE_BORDER_PT[0] * ZOOM, TABLE_BORDER_PT[1] * ZOOM],
                [TABLE_BORDER_PT[2] * ZOOM, TABLE_BORDER_PT[1] * ZOOM],
                [TABLE_BORDER_PT[2] * ZOOM, TABLE_BORDER_PT[3] * ZOOM],
                [TABLE_BORDER_PT[0] * ZOOM, TABLE_BORDER_PT[3] * ZOOM],
            ],
            dtype=np.float32,
        )
        M = cv2.getPerspectiveTransform(corners, dst)
        out_size = (int(PAGE_W_PT * ZOOM), int(PAGE_H_PT * ZOOM))
        return cv2.warpPerspective(img_bgr, M, out_size, borderValue=(255, 255, 255))
    except AlignmentError:
        raise
    except Exception as e:
        raise AlignmentError(f"답안지 정렬 중 오류가 발생했습니다: {e}") from e


BLANK_LABEL = "미응답"


def multi_label(options):
    return "중복(" + ",".join(str(o) for o in sorted(options)) + ")"


def recognize_sheet(img_bgr, num_questions):
    """답안지 스캔 이미지 1장을 인식해 학번과 num_questions개 문항의 답을 반환."""
    aligned = align_sheet(img_bgr)
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    radius_px = (BUBBLE_SIZE / 2 * 0.75) * ZOOM

    digits = []
    id_flagged_cols = []
    for col in range(8):
        ratios = []
        for digit in range(10):
            cx, cy = id_bubble_center_pt(col, digit)
            ratios.append(sample_fill_ratio(gray, cx * ZOOM, cy * ZOOM, radius_px))
        idx = detect_marked_index(ratios)
        if idx is None or isinstance(idx, list):
            digits.append("?")
            id_flagged_cols.append(col)
        else:
            digits.append(str(idx))
    student_id = "".join(digits)

    answers = {}
    flagged_questions = []
    for q in range(1, num_questions + 1):
        ratios = []
        for option in range(1, 6):
            cx, cy = answer_bubble_center_pt(q, option)
            ratios.append(sample_fill_ratio(gray, cx * ZOOM, cy * ZOOM, radius_px))
        idx = detect_marked_index(ratios)
        if idx is None:
            answers[q] = BLANK_LABEL
            flagged_questions.append(q)
        elif isinstance(idx, list):
            answers[q] = multi_label([i + 1 for i in idx])
            flagged_questions.append(q)
        else:
            answers[q] = idx + 1

    return {
        "student_id": student_id,
        "id_flagged_cols": id_flagged_cols,
        "answers": answers,
        "flagged_questions": flagged_questions,
    }


def grade_sheet(answers, answer_key):
    """인식된 답안과 정답표를 비교해 (점수, 오답문항번호 리스트)를 반환.
    미응답/중복 등 int가 아닌 답은 항상 오답으로 처리한다."""
    score = 0
    wrong = []
    for q, correct in answer_key.items():
        given = answers.get(q, BLANK_LABEL)
        if isinstance(given, int) and given == correct:
            score += 1
        else:
            wrong.append(q)
    return score, wrong


WRONG_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")


def write_result_excel(path, student_records, answer_key, failed_labels):
    """채점 결과를 엑셀로 저장. 시트 '채점결과'(학생별 1행) + '정렬실패'(실패 파일 목록)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "채점결과"

    qnums = sorted(answer_key.keys())
    header = ["학번"] + [f"Q{q}" for q in qnums] + ["점수", "오답문항", "확인필요"]
    ws.append(header)

    for rec in student_records:
        wrong_set = set(rec["wrong"])
        row = [rec["student_id"]]
        for q in qnums:
            row.append(rec["answers"].get(q, BLANK_LABEL))
        row.append(rec["score"])
        row.append(",".join(str(q) for q in rec["wrong"]))
        flagged = sorted(set(rec["flagged_questions"]))
        row.append(",".join(f"Q{q}" for q in flagged))
        ws.append(row)

        r = ws.max_row
        for i, q in enumerate(qnums):
            if q in wrong_set:
                ws.cell(row=r, column=2 + i).fill = WRONG_FILL

    fail_ws = wb.create_sheet("정렬실패")
    fail_ws.append(["파일명"])
    for label in failed_labels:
        fail_ws.append([label])

    wb.save(path)
