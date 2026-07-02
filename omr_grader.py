"""OMR 답안지(OCR100.pdf 양식) 스캔 채점 프로그램."""

import csv
import os

import cv2
import fitz
import numpy as np
import openpyxl

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
