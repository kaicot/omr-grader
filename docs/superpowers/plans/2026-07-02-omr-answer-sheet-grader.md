# OMR 답안지 채점 프로그램 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 평판 스캐너로 스캔한 5지선다 OMR 답안지(학번 8자리 + 최대 100문항, `OCR100.pdf` 양식)를 읽어 정답표와 대조 채점하고, 학번 미설치 PC에서도 실행 가능한 단일 exe로 배포되는 프로그램을 만든다.

**Architecture:** `OCR100.pdf`가 벡터 PDF라는 점을 이용해 모든 마킹칸의 정확한 좌표를 소수의 격자 파라미터(원점+칸 간격)로 계산하는 템플릿을 만든다. 스캔 이미지에서는 인쇄된 답안표의 굵은 외곽 테두리 사각형을 검출해 투시 변환으로 정렬한 뒤, 템플릿 좌표를 그대로 적용해 각 마킹칸의 채움 정도를 측정한다. GUI(tkinter)로 폴더/정답표를 선택해 실행하고, 결과는 엑셀 + 디버그 오버레이 이미지로 저장한다. 최종적으로 PyInstaller로 단일 exe를 빌드해 Python 미설치 PC에 배포한다.

**Tech Stack:** Python 3.13, numpy, opencv-python-headless, openpyxl, pandas(미사용 가능성 있음, 필요시만), Pillow, PyMuPDF(fitz), tkinter(표준 라이브러리), PyInstaller — 전부 이 PC에 이미 설치되어 있어 신규 설치 없음.

## Global Constraints

- 새 외부 의존성을 추가하지 않는다 — numpy, opencv-python-headless, openpyxl, Pillow, PyMuPDF, tkinter, pyinstaller만 사용 (이미 설치됨).
- 프로그램 본체는 단일 스크립트 `omr_grader.py` 하나로 구성한다 (설계 확정 사항).
- 테스트는 pytest 등 프레임워크 없이 `assert` 기반 자체 검증 스크립트로 작성한다 (설계 확정 사항) — 각 테스트는 `tests/test_omr_grader.py` 안의 함수이며 `python tests/test_omr_grader.py` 로 전부 실행된다.
- 최종 배포 형태는 PyInstaller `--onefile` 단일 exe이며, 정식 설치 프로그램(Inno Setup 등)은 만들지 않는다.
- 문항 수는 GUI에서 별도 입력받지 않고 정답표 파일의 행 수로 자동 결정한다.
- 마킹이 애매한 경우(미응답/중복)는 항상 오답 처리하되 엑셀에 값 대신 표시하고 "확인필요" 열에 문항번호를 남긴다 — 임의로 추정해서 채점하지 않는다.

---

## 공통 상수 (Task 1에서 정의, 이후 모든 Task가 참조)

`OCR100.pdf`를 PyMuPDF로 직접 분석해 얻은 실측값이다 (임의 추정치 아님):

```python
PAGE_W_PT = 841.0
PAGE_H_PT = 595.0
ZOOM = 3.0  # 캔버스 렌더링 배율 (pt -> px), 216dpi 상당

# 답안 마킹 그리드 (100문항, 5블록 x 20행 x 5지선다)
BLOCK_X_STARTS = [239.16, 355.80, 472.44, 589.08, 705.72]  # 블록별 1번 옵션 x좌표(pt)
BUBBLE_X_PITCH = 19.44   # 같은 행 내 옵션 간 x간격(pt)
BUBBLE_SIZE = 11.04      # 마킹칸 한 변 크기(pt), 정사각형으로 근사
ROW_Y_START = 78.29      # 1행(문항1,21,41,61,81) y좌표(pt)
ROW_Y_PITCH = 23.7789    # 행 간 y간격(pt)

# 학번 그리드 (8자리 x 0~9)
ID_COL_X_START = 48.24
ID_COL_PITCH = 19.8
ID_ROW_Y_START = 297.32
ID_ROW_PITCH = 18.7033

# 답안표 전체를 감싸는 굵은 외곽 테두리 사각형 (정렬 기준점)
TABLE_BORDER_PT = (215.285, 39.579, 797.847, 546.666)  # x0,y0,x1,y1
```

---

### Task 1: 마킹칸 템플릿 좌표 계산

**Files:**
- Create: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Produces: `PAGE_W_PT`, `PAGE_H_PT`, `ZOOM`, `TABLE_BORDER_PT` 상수, `answer_bubble_center_pt(qnum: int, option: int) -> tuple[float, float]`, `id_bubble_center_pt(col: int, digit: int) -> tuple[float, float]`

- [ ] **Step 1: 테스트 폴더/파일 생성 및 실패하는 테스트 작성**

`tests/test_omr_grader.py`:

```python
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
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `ModuleNotFoundError: No module named 'omr_grader'` (아직 파일이 없으므로)

- [ ] **Step 3: `omr_grader.py`에 상수와 템플릿 함수 작성**

`omr_grader.py`:

```python
"""OMR 답안지(OCR100.pdf 양식) 스캔 채점 프로그램."""

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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `5 tests passed`

- [ ] **Step 5: Commit**

```bash
git init
git add omr_grader.py tests/test_omr_grader.py docs/superpowers
git commit -m "feat: add OMR bubble template geometry"
```

---

### Task 2: 정답표 로더

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: 없음 (표준 라이브러리 + openpyxl만 사용)
- Produces: `load_answer_key(path: str) -> dict[int, int]` — 키는 1부터 시작하는 연속된 문항번호, 값은 정답(1~5). 문항 수는 `len(반환값)`으로 알 수 있음(호출부에서 사용).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_omr_grader.py`에 추가:

```python
import csv
import tempfile


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
```

`ALL_TESTS` 리스트에 두 함수를 추가한다.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'load_answer_key'`

- [ ] **Step 3: `load_answer_key` 구현**

`omr_grader.py`에 추가:

```python
import csv
import os

import openpyxl


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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `7 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add answer key loader"
```

---

### Task 3: 스캔 입력 로더 (이미지 파일 + 멀티페이지 PDF)

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: `PAGE_W_PT`, `PAGE_H_PT`, `ZOOM` (Task 1)
- Produces: `load_scan_images(paths: list[str]) -> list[tuple[str, numpy.ndarray]]` — `(라벨, BGR 이미지)` 목록. PDF는 페이지마다 하나씩 분리됨.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
import fitz
import numpy as np


def _render_template_page(zoom=og.ZOOM):
    doc = fitz.open("OCR100.pdf")
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return img.copy()


def test_load_scan_images_single_page_pdf():
    with tempfile.TemporaryDirectory() as d:
        doc = fitz.open("OCR100.pdf")
        out_path = os.path.join(d, "scan.pdf")
        doc.save(out_path)
        results = og.load_scan_images([out_path])
        assert len(results) == 1
        label, img = results[0]
        assert img.ndim == 3 and img.shape[2] == 3


def test_load_scan_images_image_file(tmp_img_path=None):
    with tempfile.TemporaryDirectory() as d:
        img = _render_template_page()
        path = os.path.join(d, "student1.png")
        import cv2
        cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        results = og.load_scan_images([path])
        assert len(results) == 1
        label, loaded = results[0]
        assert label == "student1.png"
        assert loaded.shape[:2] == img.shape[:2]
```

`ALL_TESTS`에 두 함수 추가. (`OCR100.pdf`는 프로젝트 루트에 있으므로 테스트를 프로젝트 루트에서 실행한다는 전제 — Step 2/4의 Run 커맨드와 동일하게 루트에서 실행)

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'load_scan_images'`

- [ ] **Step 3: `load_scan_images` 구현**

`omr_grader.py`에 추가:

```python
import cv2
import fitz
import numpy as np


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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `9 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add scan image loader for images and multi-page pdf"
```

---

### Task 4: 마킹칸 채움 감지

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: 없음 (numpy만 사용)
- Produces:
  - `sample_fill_ratio(gray_img: np.ndarray, cx_px: float, cy_px: float, radius_px: float) -> float`
  - `detect_marked_index(fill_ratios: list[float], threshold: float = FILL_RATIO_MARK) -> int | list[int] | None` — 단일 마킹이면 0-based 인덱스, 미응답이면 `None`, 중복이면 인덱스 리스트.
  - 상수 `DARK_THRESHOLD = 128`, `FILL_RATIO_MARK = 0.35`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_sample_fill_ratio_filled_circle():
    img = np.full((60, 60), 255, dtype=np.uint8)
    cv2.circle(img, (30, 30), 10, 0, -1)  # 채워진 검은 원
    ratio = og.sample_fill_ratio(img, 30, 30, 8)
    assert ratio > 0.9, f"ratio={ratio}"


def test_sample_fill_ratio_blank():
    img = np.full((60, 60), 255, dtype=np.uint8)
    ratio = og.sample_fill_ratio(img, 30, 30, 8)
    assert ratio < 0.05, f"ratio={ratio}"


def test_detect_marked_index_blank():
    assert og.detect_marked_index([0.02, 0.03, 0.01, 0.02, 0.02]) is None


def test_detect_marked_index_single():
    assert og.detect_marked_index([0.02, 0.03, 0.85, 0.02, 0.02]) == 2


def test_detect_marked_index_multi():
    result = og.detect_marked_index([0.02, 0.80, 0.03, 0.78, 0.02])
    assert result == [1, 3], f"result={result}"
```

`ALL_TESTS`에 다섯 함수 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'sample_fill_ratio'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
DARK_THRESHOLD = 128
FILL_RATIO_MARK = 0.35


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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `14 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add bubble fill ratio sampling and mark detection"
```

---

### Task 5: 스캔 이미지 정렬 (투시 보정)

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: `PAGE_W_PT`, `PAGE_H_PT`, `ZOOM`, `TABLE_BORDER_PT` (Task 1)
- Produces:
  - `find_table_border(gray_img: np.ndarray) -> np.ndarray | None` — 검출된 4개 모서리 좌표 `(4,2)` float32, `[tl, tr, br, bl]` 순서. 못 찾으면 `None`.
  - `class AlignmentError(Exception)`
  - `align_sheet(img_bgr: np.ndarray) -> np.ndarray` — 정렬 실패 시 `AlignmentError` 발생, 성공 시 `(PAGE_H_PT*ZOOM, PAGE_W_PT*ZOOM)` 크기의 BGR 이미지 반환.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def _render_template_page_gray(zoom=og.ZOOM):
    img = _render_template_page(zoom)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def test_find_table_border_on_clean_render():
    img_bgr = _render_template_page_gray()
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    corners = og.find_table_border(gray)
    assert corners is not None
    expected_tl = (og.TABLE_BORDER_PT[0] * og.ZOOM, og.TABLE_BORDER_PT[1] * og.ZOOM)
    assert abs(corners[0][0] - expected_tl[0]) < 15
    assert abs(corners[0][1] - expected_tl[1]) < 15


def test_align_sheet_recovers_rotated_scan():
    img_bgr = _render_template_page_gray()
    h, w = img_bgr.shape[:2]
    # 스캐너에 종이가 살짝 삐뚤게 놓인 상황을 흉내: 2도 회전 + 여백 추가
    canvas = cv2.copyMakeBorder(img_bgr, 80, 80, 80, 80, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    ch, cw = canvas.shape[:2]
    M = cv2.getRotationMatrix2D((cw / 2, ch / 2), 2.0, 1.0)
    rotated = cv2.warpAffine(canvas, M, (cw, ch), borderValue=(255, 255, 255))

    aligned = og.align_sheet(rotated)
    assert aligned.shape[0] == int(og.PAGE_H_PT * og.ZOOM)
    assert aligned.shape[1] == int(og.PAGE_W_PT * og.ZOOM)

    # 정렬된 이미지에서 다시 테두리를 검출하면 캔버스 기준 좌표와 거의 일치해야 함
    aligned_gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    corners = og.find_table_border(aligned_gray)
    assert corners is not None
    expected_tl = (og.TABLE_BORDER_PT[0] * og.ZOOM, og.TABLE_BORDER_PT[1] * og.ZOOM)
    assert abs(corners[0][0] - expected_tl[0]) < 20
    assert abs(corners[0][1] - expected_tl[1]) < 20


def test_align_sheet_raises_on_blank_image():
    blank = np.full((500, 700, 3), 255, dtype=np.uint8)
    try:
        og.align_sheet(blank)
        assert False, "AlignmentError가 발생해야 함"
    except og.AlignmentError:
        pass
```

`ALL_TESTS`에 세 함수 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'find_table_border'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `17 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add perspective alignment via table border detection"
```

---

### Task 6: 답안지 1장 전체 인식 (학번 + 문항)

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: `align_sheet`, `AlignmentError` (Task 5), `answer_bubble_center_pt`, `id_bubble_center_pt`, `ZOOM`, `BUBBLE_SIZE` (Task 1), `sample_fill_ratio`, `detect_marked_index` (Task 4)
- Produces:
  - 상수 `BLANK_LABEL = "미응답"`
  - `multi_label(options: list[int]) -> str`
  - `recognize_sheet(img_bgr: np.ndarray, num_questions: int) -> dict` — 반환 구조:
    ```python
    {
        "student_id": str,            # 8자리, 애매한 자리는 '?'
        "id_flagged_cols": list[int], # 0-based, 애매했던 학번 자리
        "answers": {qnum: int | str}, # int(1~5) 또는 BLANK_LABEL 또는 "중복(a,b)" 문자열
        "flagged_questions": list[int],
    }
    ```

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def _draw_filled_bubble(img_bgr, cx_pt, cy_pt, zoom=og.ZOOM, radius_pt=og.BUBBLE_SIZE / 2 * 0.6):
    cx_px = int(cx_pt * zoom)
    cy_px = int(cy_pt * zoom)
    r_px = int(radius_pt * zoom)
    cv2.circle(img_bgr, (cx_px, cy_px), r_px, (0, 0, 0), -1)


def test_recognize_sheet_end_to_end():
    canonical = _render_template_page()
    img_bgr = cv2.cvtColor(canonical, cv2.COLOR_RGB2BGR)

    # 학번 12345678 마킹
    student_id = "12345678"
    for col, ch in enumerate(student_id):
        cx, cy = og.id_bubble_center_pt(col, int(ch))
        _draw_filled_bubble(img_bgr, cx, cy)

    # Q1=3, Q2=미응답, Q3=1과4 중복 마킹
    cx, cy = og.answer_bubble_center_pt(1, 3)
    _draw_filled_bubble(img_bgr, cx, cy)
    for opt in (1, 4):
        cx, cy = og.answer_bubble_center_pt(3, opt)
        _draw_filled_bubble(img_bgr, cx, cy)

    result = og.recognize_sheet(img_bgr, num_questions=3)

    assert result["student_id"] == "12345678", result["student_id"]
    assert result["id_flagged_cols"] == []
    assert result["answers"][1] == 3
    assert result["answers"][2] == og.BLANK_LABEL
    assert result["answers"][3] == "중복(1,4)"
    assert set(result["flagged_questions"]) == {2, 3}


def test_recognize_sheet_raises_alignment_error_on_blank():
    blank = np.full((500, 700, 3), 255, dtype=np.uint8)
    try:
        og.recognize_sheet(blank, num_questions=10)
        assert False, "AlignmentError가 발생해야 함"
    except og.AlignmentError:
        pass
```

`ALL_TESTS`에 두 함수 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'recognize_sheet'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `19 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add full sheet recognition (student id + answers)"
```

---

### Task 7: 채점

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: 없음 (dict만 다룸)
- Produces: `grade_sheet(answers: dict[int, int | str], answer_key: dict[int, int]) -> tuple[int, list[int]]` — `(점수, 오답문항번호 리스트)`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_grade_sheet_basic():
    answers = {1: 3, 2: og.BLANK_LABEL, 3: 5, 4: "중복(1,4)"}
    key = {1: 3, 2: 1, 3: 2, 4: 1}
    score, wrong = og.grade_sheet(answers, key)
    assert score == 1
    assert wrong == [2, 3, 4]
```

`ALL_TESTS`에 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'grade_sheet'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `20 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add grading against answer key"
```

---

### Task 8: 엑셀 결과 작성

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: 없음 (openpyxl만 사용, dict 구조는 이전 태스크들과 동일)
- Produces: `write_result_excel(path: str, student_records: list[dict], answer_key: dict[int, int], failed_labels: list[str]) -> None`
  - `student_records`의 각 항목: `{"label": str, "student_id": str, "answers": dict, "score": int, "wrong": list[int], "flagged_questions": list[int], "id_flagged_cols": list[int]}`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_write_result_excel():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "result.xlsx")
        key = {1: 3, 2: 1}
        records = [
            {
                "label": "s1.png",
                "student_id": "12345678",
                "answers": {1: 3, 2: og.BLANK_LABEL},
                "score": 1,
                "wrong": [2],
                "flagged_questions": [2],
                "id_flagged_cols": [],
            }
        ]
        og.write_result_excel(path, records, key, failed_labels=["broken.png"])

        wb = openpyxl.load_workbook(path)
        ws = wb["채점결과"]
        header = [c.value for c in ws[1]]
        assert header == ["학번", "Q1", "Q2", "점수", "오답문항", "확인필요"]
        row2 = [c.value for c in ws[2]]
        assert row2[0] == "12345678"
        assert row2[1] == 3
        assert row2[2] == og.BLANK_LABEL
        assert row2[3] == 1
        assert row2[4] == "2"
        assert row2[5] == "Q2"

        fail_ws = wb["정렬실패"]
        assert fail_ws["A2"].value == "broken.png"
```

`ALL_TESTS`에 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'write_result_excel'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
from openpyxl import Workbook
from openpyxl.styles import PatternFill

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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `21 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add excel result writer"
```

---

### Task 9: 디버그 오버레이 이미지 저장

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: `answer_bubble_center_pt`, `id_bubble_center_pt`, `ZOOM` (Task 1)
- Produces: `save_debug_overlay(aligned_img_bgr: np.ndarray, recognition: dict, out_path: str) -> None`
  - 참고(ponytail): 원본이 아니라 `align_sheet`를 거친 정렬본 위에 그린다 — 원본으로 역변환하는 건 디버그 전용 기능치고 과함. 필요해지면 그때 추가.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_save_debug_overlay():
    canonical = _render_template_page()
    img_bgr = cv2.cvtColor(canonical, cv2.COLOR_RGB2BGR)
    recognition = {
        "student_id": "12345678",
        "id_flagged_cols": [],
        "answers": {1: 3, 2: og.BLANK_LABEL},
        "flagged_questions": [2],
    }
    with tempfile.TemporaryDirectory() as d:
        out_path = os.path.join(d, "s1_debug.png")
        og.save_debug_overlay(img_bgr, recognition, out_path)
        assert os.path.exists(out_path)
        saved = cv2.imread(out_path)
        assert saved.shape == img_bgr.shape
```

`ALL_TESTS`에 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'save_debug_overlay'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
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
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `22 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: add debug overlay image writer"
```

---

### Task 10: 전체 파이프라인 통합

**Files:**
- Modify: `omr_grader.py`
- Test: `tests/test_omr_grader.py`

**Interfaces:**
- Consumes: `load_answer_key`, `load_scan_images`, `recognize_sheet`, `grade_sheet`, `write_result_excel`, `save_debug_overlay`, `AlignmentError` (Tasks 2,3,6,7,8,9)
- Produces: `run_pipeline(scan_paths: list[str], answer_key_path: str, output_dir: str) -> str` — 결과 엑셀 파일 경로를 반환. `output_dir/debug/`에 오버레이 이미지 저장.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_run_pipeline_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        key_path = os.path.join(d, "key.csv")
        with open(key_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["문항번호", "정답"])
            w.writerow([1, 3])
            w.writerow([2, 1])
            w.writerow([3, 2])

        canonical = _render_template_page()
        img_bgr = cv2.cvtColor(canonical, cv2.COLOR_RGB2BGR)
        for col, ch in enumerate("11112222"):
            cx, cy = og.id_bubble_center_pt(col, int(ch))
            _draw_filled_bubble(img_bgr, cx, cy)
        for q, opt in ((1, 3), (2, 1), (3, 5)):
            cx, cy = og.answer_bubble_center_pt(q, opt)
            _draw_filled_bubble(img_bgr, cx, cy)
        good_path = os.path.join(d, "student_good.png")
        cv2.imwrite(good_path, img_bgr)

        blank_path = os.path.join(d, "student_broken.png")
        cv2.imwrite(blank_path, np.full((500, 700, 3), 255, dtype=np.uint8))

        out_dir = os.path.join(d, "out")
        result_path = og.run_pipeline([good_path, blank_path], key_path, out_dir)

        wb = openpyxl.load_workbook(result_path)
        ws = wb["채점결과"]
        assert ws["A2"].value == "11112222"
        # 열 구성: A=학번, B=Q1, C=Q2, D=Q3, E=점수 (문항 3개 기준)
        assert ws["E2"].value == 2  # Q1,Q2 정답, Q3 오답 -> 점수 2

        fail_ws = wb["정렬실패"]
        assert fail_ws["A2"].value == "student_broken.png"

        debug_file = os.path.join(out_dir, "debug", "student_good.png")
        assert os.path.exists(debug_file)
```

`ALL_TESTS`에 추가.

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python tests/test_omr_grader.py`
Expected: `AttributeError: module 'omr_grader' has no attribute 'run_pipeline'`

- [ ] **Step 3: 구현**

`omr_grader.py`에 추가:

```python
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
    for label, img_bgr in scans:
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

        safe_name = os.path.splitext(label)[0] + ".png"
        save_debug_overlay(aligned, recognition, os.path.join(debug_dir, safe_name))

    result_path = os.path.join(output_dir, "채점결과.xlsx")
    write_result_excel(result_path, records, answer_key, failed_labels)
    return result_path
```

**참고:** `align_sheet`를 recognize_sheet 내부에서도 다시 호출하므로 파일마다 정렬이 두 번 일어난다. 100장 단위 배치에서도 각 정렬은 수십ms 수준이라 체감 성능 문제는 없음 — ponytail: 중복 호출 제거보다 지금은 이 정도 단순함이 낫다. 느려지면 그때 `recognize_sheet`가 정렬된 이미지를 받도록 인터페이스를 바꾼다.

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python tests/test_omr_grader.py`
Expected: `23 tests passed`

- [ ] **Step 5: Commit**

```bash
git add omr_grader.py tests/test_omr_grader.py
git commit -m "feat: wire full grading pipeline end to end"
```

---

### Task 11: tkinter GUI

**Files:**
- Modify: `omr_grader.py`

**Interfaces:**
- Consumes: `run_pipeline` (Task 10)
- Produces: `main()` — GUI 실행 진입점. `if __name__ == "__main__": main()`

이 태스크는 UI 배선이라 자동화된 assert 테스트 대신 수동 스모크 테스트로 검증한다 (아래 Step 3).

- [ ] **Step 1: GUI 코드 작성**

`omr_grader.py` 맨 아래에 추가:

```python
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
```

- [ ] **Step 2: 문법 오류 없는지 확인**

Run: `python -c "import omr_grader"`
Expected: 에러 없이 조용히 종료 (import만 하고 `main()`은 호출 안 되므로 GUI는 뜨지 않음)

- [ ] **Step 3: 수동 스모크 테스트**

Run: `python omr_grader.py`
Expected:
- GUI 창이 뜬다
- "답안지 폴더 선택" → 아무 이미지 폴더나 선택 → 개수가 라벨에 표시됨
- "정답표 파일 선택" → Task 2에서 만든 형식의 xlsx/csv 선택 → 경로가 라벨에 표시됨
- "채점 시작" → 결과 저장 폴더 선택 → 로그창에 "완료: ..." 메시지와 함께 완료 팝업이 뜬다
- 선택한 결과 폴더에 `채점결과.xlsx`와 `debug/` 폴더가 생성되어 있다

- [ ] **Step 4: Commit**

```bash
git add omr_grader.py
git commit -m "feat: add tkinter GUI for folder/answer-key selection and grading"
```

---

### Task 12: PyInstaller 단일 exe 빌드

**Files:**
- Create: `omr_grader.spec` (PyInstaller가 첫 빌드 시 자동 생성 — 수동 작성 아님)

**Interfaces:**
- Consumes: `omr_grader.py` (Task 11까지 완성된 전체 프로그램)
- Produces: `dist/omr_grader.exe`

- [ ] **Step 1: 빌드**

Run:
```bash
python -m PyInstaller --onefile --windowed --name omr_grader omr_grader.py
```
Expected: `dist/omr_grader.exe` 생성됨, 콘솔에 `completed successfully` 메시지

- [ ] **Step 2: 이 PC에서 exe 단독 실행 검증**

Run: `./dist/omr_grader.exe` (더블클릭 또는 터미널에서 직접 실행)
Expected:
- Python 인터프리터 없이도(별도 venv/PATH 설정 없이) GUI가 뜬다
- Task 11의 수동 스모크 테스트를 exe로 동일하게 반복해 정상 동작 확인 (폴더 선택 → 정답표 선택 → 채점 시작 → 결과 엑셀/debug 폴더 생성)

- [ ] **Step 3: 학과사무실 PC로 배포**

`dist/omr_grader.exe` 파일 하나만 USB나 공유폴더로 복사해 학과사무실 PC에 옮기고, 그 PC에서 더블클릭 실행해 GUI가 뜨는지 확인한다 (Python 설치 여부와 무관하게 동작해야 함).

- [ ] **Step 4: Commit**

```bash
echo "dist/" >> .gitignore
echo "build/" >> .gitignore
echo "*.spec" >> .gitignore
git add .gitignore
git commit -m "chore: ignore pyinstaller build artifacts"
```
