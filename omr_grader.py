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
                if not row or row[0] == "" or len(row) < 2 or row[1] == "":
                    continue
                rows.append((int(row[0]), int(row[1])))
    else:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] is None or row[1] is None:
                continue
            rows.append((int(row[0]), int(row[1])))
        wb.close()

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


_ROTATIONS = (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE)

# 표지 제목("동명대학교...", "20__년 중간·기말고사") 텍스트가 찍히는 영역(pt 좌표).
# 답안표 테두리는 180도 회전해도 사각형 비율이 같아 그것만으로는 상하 반전을 구분할 수
# 없으므로, 진하게 인쇄된 이 표지 텍스트 영역과 그 반대편(대각선 대칭 위치)의 잉크
# 밀도를 비교해 상하가 뒤집혔는지 판별한다.
_HEADER_TEXT_PT = (41.83, 40.06, 200.71, 82.9)


def _is_upside_down(aligned_gray):
    x0, y0, x1, y1 = (int(c * ZOOM) for c in _HEADER_TEXT_PT)
    header_patch = aligned_gray[y0:y1, x0:x1]

    mx0 = PAGE_W_PT - _HEADER_TEXT_PT[2]
    my0 = PAGE_H_PT - _HEADER_TEXT_PT[3]
    mx1 = PAGE_W_PT - _HEADER_TEXT_PT[0]
    my1 = PAGE_H_PT - _HEADER_TEXT_PT[1]
    mx0, my0, mx1, my1 = (int(c * ZOOM) for c in (mx0, my0, mx1, my1))
    mirror_patch = aligned_gray[my0:my1, mx0:mx1]

    if header_patch.size == 0 or mirror_patch.size == 0:
        return False

    header_dark = np.mean(header_patch < DARK_THRESHOLD)
    mirror_dark = np.mean(mirror_patch < DARK_THRESHOLD)
    return mirror_dark > header_dark


def align_sheet(img_bgr):
    """스캔 이미지를 답안표 테두리 기준으로 투시 보정해
    (PAGE_H_PT*ZOOM, PAGE_W_PT*ZOOM) 크기의 정렬된 이미지로 반환.
    스캐너가 답안지를 90/180/270도 회전된 방향으로 스캔한 경우까지 시도하고,
    표지 텍스트 위치를 근거로 상하 반전 여부까지 보정한다."""
    try:
        dst = np.array(
            [
                [TABLE_BORDER_PT[0] * ZOOM, TABLE_BORDER_PT[1] * ZOOM],
                [TABLE_BORDER_PT[2] * ZOOM, TABLE_BORDER_PT[1] * ZOOM],
                [TABLE_BORDER_PT[2] * ZOOM, TABLE_BORDER_PT[3] * ZOOM],
                [TABLE_BORDER_PT[0] * ZOOM, TABLE_BORDER_PT[3] * ZOOM],
            ],
            dtype=np.float32,
        )
        out_size = (int(PAGE_W_PT * ZOOM), int(PAGE_H_PT * ZOOM))

        for rotate_code in _ROTATIONS:
            candidate = img_bgr if rotate_code is None else cv2.rotate(img_bgr, rotate_code)
            gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
            corners = find_table_border(gray)
            if corners is None:
                continue
            M = cv2.getPerspectiveTransform(corners, dst)
            aligned = cv2.warpPerspective(candidate, M, out_size, borderValue=(255, 255, 255))
            aligned_gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
            if _is_upside_down(aligned_gray):
                # 캔버스 전체를 뒤집으면 이미 dst 위치에 맞춰 배치된 표가 반대쪽
                # 구석으로 밀려나므로, 원본 후보를 180도 돌려 테두리 검출/정렬을
                # 처음부터 다시 수행한다.
                candidate180 = cv2.rotate(candidate, cv2.ROTATE_180)
                gray180 = cv2.cvtColor(candidate180, cv2.COLOR_BGR2GRAY)
                corners180 = find_table_border(gray180)
                if corners180 is not None:
                    M180 = cv2.getPerspectiveTransform(corners180, dst)
                    aligned = cv2.warpPerspective(
                        candidate180, M180, out_size, borderValue=(255, 255, 255)
                    )
            return aligned

        raise AlignmentError("답안표 테두리를 찾지 못했습니다")
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


def save_debug_overlay(aligned_img_bgr, recognition, out_path):
    """정렬된 이미지 위에 인식된 마킹 위치를 원으로 표시해 저장.
    확인필요로 플래그된 문항/학번 자리는 다른 색으로 강조."""
    overlay = aligned_img_bgr.copy()
    radius_px = int(BUBBLE_SIZE / 2 * ZOOM)

    for col in range(8):
        digit_char = recognition["student_id"][col]
        color = (0, 0, 255) if col in recognition["id_flagged_cols"] else (0, 200, 0)
        if digit_char == "?":
            continue
        cx, cy = id_bubble_center_pt(col, int(digit_char))
        cv2.circle(overlay, (int(cx * ZOOM), int(cy * ZOOM)), radius_px, color, 2)

    for q, value in recognition["answers"].items():
        flagged = q in recognition["flagged_questions"]
        color = (0, 0, 255) if flagged else (0, 200, 0)
        if isinstance(value, int):
            cx, cy = answer_bubble_center_pt(q, value)
            cv2.circle(overlay, (int(cx * ZOOM), int(cy * ZOOM)), radius_px, color, 2)
        # 미응답/중복은 원으로 표시할 단일 위치가 없으므로 그리지 않음
        # (엑셀의 확인필요 열 + 이 오버레이의 빨간 원 부재 자체가 "이상함" 신호)

    cv2.imwrite(out_path, overlay)


def run_pipeline(scan_paths, answer_key_path, output_dir):
    """스캔 파일 목록 + 정답표 경로를 받아 채점 엑셀을 생성하고 경로를 반환."""
    os.makedirs(output_dir, exist_ok=True)
    debug_dir = os.path.join(output_dir, "debug")
    os.makedirs(debug_dir, exist_ok=True)

    answer_key = load_answer_key(answer_key_path)
    num_questions = len(answer_key)

    scans = load_scan_images(scan_paths)

    records = []
    failed_labels = []
    for i, (label, img_bgr) in enumerate(scans):
        try:
            aligned = align_sheet(img_bgr)
        except AlignmentError:
            failed_labels.append(label)
            continue

        recognition = recognize_sheet(img_bgr, num_questions)
        score, wrong = grade_sheet(recognition["answers"], answer_key)

        records.append(
            {
                "label": label,
                "student_id": recognition["student_id"],
                "answers": recognition["answers"],
                "score": score,
                "wrong": wrong,
                "flagged_questions": recognition["flagged_questions"],
                "id_flagged_cols": recognition["id_flagged_cols"],
            }
        )

        safe_name = f"{i:04d}_{label.replace('.', '_')}.png"
        save_debug_overlay(aligned, recognition, os.path.join(debug_dir, safe_name))

    result_path = os.path.join(output_dir, "채점결과.xlsx")
    write_result_excel(result_path, records, answer_key, failed_labels)
    return result_path


# 참고: align_sheet를 recognize_sheet 내부에서도 다시 호출하므로 파일마다 정렬이 두 번 일어난다.
# 100장 단위 배치에서도 각 정렬은 수십ms 수준이라 체감 성능 문제는 없음 — ponytail: 중복 호출
# 제거보다 지금은 이 정도 단순함이 낫다. 느려지면 그때 recognize_sheet가 정렬된 이미지를
# 받도록 인터페이스를 바꾼다.


import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox


def _run_pipeline_worker(scan_paths, key_path, output_dir, log_queue, done_queue):
    try:
        result_path = run_pipeline(scan_paths, key_path, output_dir, )
        log_queue.put(f"완료: {result_path}")
        done_queue.put(("ok", result_path))
    except Exception as e:  # noqa: BLE001 - GUI 최상위 경계, 사용자에게 그대로 보여줌
        log_queue.put(f"오류: {e}")
        done_queue.put(("error", str(e)))


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("OMR 답안지 채점")
        self.scan_paths = []
        self.key_path = None
        self.log_queue = queue.Queue()
        self.done_queue = queue.Queue()

        tk.Button(root, text="답안지 폴더 선택", command=self.pick_folder).pack(fill="x", padx=10, pady=5)
        tk.Button(root, text="답안지 파일 선택(개별)", command=self.pick_files).pack(fill="x", padx=10, pady=5)
        self.scan_label = tk.Label(root, text="선택된 답안지: 없음")
        self.scan_label.pack(padx=10, anchor="w")

        tk.Button(root, text="정답표 파일 선택", command=self.pick_key).pack(fill="x", padx=10, pady=5)
        self.key_label = tk.Label(root, text="정답표: 없음")
        self.key_label.pack(padx=10, anchor="w")

        tk.Button(root, text="채점 시작", command=self.start).pack(fill="x", padx=10, pady=10)

        self.log_text = tk.Text(root, height=12, width=60)
        self.log_text.pack(padx=10, pady=5)

        self.root.after(200, self._poll_log_queue)

    def pick_folder(self):
        folder = filedialog.askdirectory(title="답안지 폴더 선택")
        if folder:
            self.scan_paths = [
                os.path.join(folder, f)
                for f in os.listdir(folder)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".pdf"))
            ]
            self.scan_label.config(text=f"선택된 답안지: {len(self.scan_paths)}개 (폴더: {folder})")

    def pick_files(self):
        files = filedialog.askopenfilenames(
            title="답안지 파일 선택",
            filetypes=[("답안지 파일", "*.png *.jpg *.jpeg *.pdf")],
        )
        if files:
            self.scan_paths = list(files)
            self.scan_label.config(text=f"선택된 답안지: {len(self.scan_paths)}개")

    def pick_key(self):
        path = filedialog.askopenfilename(
            title="정답표 파일 선택", filetypes=[("정답표", "*.xlsx *.csv")]
        )
        if path:
            self.key_path = path
            self.key_label.config(text=f"정답표: {path}")

    def start(self):
        if not self.scan_paths:
            messagebox.showerror("오류", "답안지를 먼저 선택하세요")
            return
        if not self.key_path:
            messagebox.showerror("오류", "정답표를 먼저 선택하세요")
            return
        output_dir = filedialog.askdirectory(title="결과를 저장할 폴더 선택")
        if not output_dir:
            return
        self.log_text.insert("end", "채점 시작...\n")
        threading.Thread(
            target=_run_pipeline_worker,
            args=(self.scan_paths, self.key_path, output_dir, self.log_queue, self.done_queue),
            daemon=True,
        ).start()

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            self.log_text.insert("end", self.log_queue.get() + "\n")
            self.log_text.see("end")
        while not self.done_queue.empty():
            status, msg = self.done_queue.get()
            if status == "ok":
                messagebox.showinfo("완료", f"채점 결과: {msg}")
            else:
                messagebox.showerror("오류", msg)
        self.root.after(200, self._poll_log_queue)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
