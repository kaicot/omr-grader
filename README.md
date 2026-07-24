# OMR Grader

A simple Windows tool that grades scanned 5-choice OMR (bubble sheet) answer sheets and outputs an Excel result. Built for a fixed 100-question / 8-digit student ID answer form (`OCR100.pdf`).

Current release: **0.2.0**. It adds local grading history, preserved originals, re-analysis/re-grading, manual review, item analysis, and diagnostic recovery. See the [changelog](CHANGELOG.md).

---

## English

### What it does

1. Scan answer sheets with a flatbed scanner (individual image files or a multi-page PDF).
2. Prepare an answer key file (xlsx/csv) with question numbers and correct answers.
3. Run the program, pick the scans and the answer key, pick an output folder.
4. Get a graded Excel file plus a debug overlay image per sheet, so you can visually double-check any flagged (blank / double-marked / unclear) answer.

The number of questions is taken from however many rows are in your answer key — you don't configure it separately, and it doesn't have to be 100 (e.g. a 35-question exam using rows 1–35 works fine).

### Running it (no Python needed)

Get `omr_grader.exe` (build it yourself, see below, or ask whoever built it for a copy) and double-click it. A splash screen appears while it unpacks, then the GUI opens:

- **답안지 파일 선택(PDF, JPG, PNG)** — pick your scanned sheets
- **정답표 파일 선택(XLSX, CSV)** — pick your answer key
- **채점 시작(채점결과 저장 폴더 선택)** — pick where to save the results

A step-by-step walkthrough with screenshots (Korean) is available at [사용법 가이드](docs/guide/사용법-가이드.md) ([PDF](docs/guide/사용법-가이드.pdf)) — the screenshots alone are useful even if you don't read Korean.

### Answer key format

A 2-column file, question number then correct answer (1–5):

| 문항번호 | 정답 |
|---|---|
| 1 | 3 |
| 2 | 1 |
| ... | ... |

### Output

- `채점결과.xlsx` — one row per student: student ID, per-question answer, score, wrong-question list, and a "확인필요" (needs review) column for blank/ambiguous marks.
- `debug/` — one image per sheet with recognized marks circled (red = flagged for review), so you can eyeball anything uncertain against the original scan.

### Low-quality scan support

The recognizer corrects uneven lighting and scanner noise with local background normalization, then samples each bubble with a sensitivity profile instead of one fixed global threshold. In the GUI, use **인식 민감도** from 1 to 10: start at 6, raise it for faint pencil marks, and lower it when printed artifacts are being treated as marks. Ambiguous bubbles remain flagged for review rather than being guessed.

### Building from source

Requires Python 3.13 with `numpy`, `opencv-python-headless`, `openpyxl`, `Pillow`, `PyMuPDF`, `pyinstaller` (tkinter is stdlib).

```bash
python omr_grader.py                             # run the GUI directly
python -m PyInstaller --onefile --windowed --name omr_grader --splash splash.png omr_grader.py
```

### Author

Cho, Seung-Hyun (kaic21@gmail.com)

---

## 한국어

현재 버전은 **0.2.0**입니다. 로컬 채점 이력·원본 보존·재분석/재채점·수동 검토·문항 분석·진단 복구를 추가했습니다. 자세한 내용은 [변경 이력](CHANGELOG.md)을 확인하세요.

### 무엇을 하는 프로그램인가요

1. 평판 스캐너로 답안지를 스캔합니다 (개별 이미지 파일 또는 여러 장이 묶인 PDF).
2. 문항번호+정답이 담긴 정답표 파일(xlsx/csv)을 준비합니다.
3. 프로그램을 실행해 답안지와 정답표를 선택하고, 결과를 저장할 폴더를 선택합니다.
4. 채점된 엑셀 파일과, 답안지마다 인식된 마킹을 표시한 확인용 이미지를 받습니다 — 미응답/중복마킹 등 애매한 답은 원본과 바로 대조할 수 있습니다.

문항 수는 정답표 파일의 행 수로 자동 결정됩니다. 100문항이 아니어도(예: 35문항만 있는 시험) 별도 설정 없이 그대로 동작합니다.

### 실행 방법 (파이썬 설치 불필요)

`dist/omr_grader.exe`를 그대로 복사해서 더블클릭하면 됩니다. 실행 시 압축 해제되는 동안 로딩 화면이 뜨고, 이후 GUI 창이 열립니다.

- **답안지 파일 선택(PDF, JPG, PNG)** — 스캔한 답안지 선택
- **정답표 파일 선택(XLSX, CSV)** — 정답표 선택
- **채점 시작(채점결과 저장 폴더 선택)** — 결과 저장 위치 선택

스크린샷과 함께 하나하나 따라 할 수 있는 자세한 사용법은 [사용법 가이드](docs/guide/사용법-가이드.md) ([PDF](docs/guide/사용법-가이드.pdf))를 참고하세요.

### 정답표 형식

문항번호, 정답(1~5) 2열짜리 파일:

| 문항번호 | 정답 |
|---|---|
| 1 | 3 |
| 2 | 1 |
| ... | ... |

### 결과물

- `채점결과.xlsx` — 학생별 1행(학번, 문항별 답안, 점수, 오답문항, 확인필요 열 포함).
- `debug/` — 답안지별로 인식된 마킹을 원으로 표시한 이미지(빨간 원 = 확인 필요) — 원본 스캔본과 눈으로 바로 대조 가능.

인식 민감도는 1~10으로 조절할 수 있습니다. 흐린 마킹은 값을 높이고, 인쇄 잡음이 마킹으로 잡히면 값을 낮추세요. 조명 편차 보정과 다중 테두리 검출을 함께 적용하지만, 애매한 표시는 자동 추측하지 않고 `확인필요`로 남깁니다.

### 소스에서 빌드하기

Python 3.13과 `numpy`, `opencv-python-headless`, `openpyxl`, `Pillow`, `PyMuPDF`, `pyinstaller`가 필요합니다 (tkinter는 표준 라이브러리).

```bash
python omr_grader.py                             # GUI 직접 실행
python -m PyInstaller --onefile --windowed --name omr_grader --splash splash.png omr_grader.py
```

### 제작

조승현 (kaic21@gmail.com)
