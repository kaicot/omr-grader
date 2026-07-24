# OMR Grader

5지선다 OMR 답안지 스캔본을 채점해 Excel 결과를 만드는 Windows 프로그램입니다. 고정 100문항·8자리 학번 양식(`OCR100.pdf`)을 기본으로 하지만, 정답표 행 수에 맞춰 문항 수를 자동으로 처리합니다.

현재 정식 버전은 **v1.0.0**입니다. 채점 이력, 원본 보존, 재분석·재채점, 수동 검토, 문항 분석, 진단 복구를 제공합니다.

## 한국어

### 주요 기능

1. 이미지 파일 또는 여러 페이지 PDF로 스캔한 답안지를 불러옵니다.
2. xlsx/csv 정답표(문항번호, 정답)를 적용해 채점합니다.
3. `채점결과.xlsx`와 답안지별 `debug/` 오버레이를 생성합니다.
4. 채점 실행 이력과 원본 답안지를 로컬에 보존해 나중에 다시 확인할 수 있습니다.
5. 새 민감도로 재분석하거나 새 정답표로 재채점하기 전에 변경 결과를 미리 봅니다.
6. 학생별 답안을 수동으로 수정하고, 수정 사유와 결과를 기록합니다.
7. 학생 결과와 문항별 정답률·선택지 분포를 확인합니다.

### 실행 방법

`dist/omr_grader.exe`를 더블클릭하면 됩니다. Python 설치 없이 실행할 수 있습니다.

- **답안지 파일 선택(PDF, JPG, PNG)**: 스캔한 답안지 선택
- **정답표 파일 선택(XLSX, CSV)**: 정답표 선택
- **채점 시작(채점결과 저장 폴더 선택)**: 결과 저장 위치 선택

스크린샷 기반 사용법은 [사용법 가이드](docs/guide/사용법-가이드.md) ([PDF](docs/guide/사용법-가이드.pdf))를 참고하세요.

### 정답표 형식

| 문항번호 | 정답 |
|---|---|
| 1 | 3 |
| 2 | 1 |
| ... | ... |

정답은 1~5를 사용하며, 행 수가 채점 문항 수가 됩니다.

### 결과물과 저장 위치

- `채점결과.xlsx`: 학번, 문항별 답안, 점수, 오답 문항, 확인 필요 여부
- `debug/`: 인식된 마킹을 표시한 검토용 이미지
- `%LOCALAPPDATA%\OMRGrader`: 채점 이력, 보존 원본, 진단 로그, 복구 가능한 삭제 보관함

인식 민감도는 1~10으로 조절합니다. 흐린 마킹은 값을 높이고, 인쇄 잡음이 마킹으로 잡히면 값을 낮추세요. 애매한 표시는 자동 추측하지 않고 `확인필요`로 남깁니다.

### 소스에서 빌드하기

Python 3.13과 `numpy`, `opencv-python-headless`, `openpyxl`, `Pillow`, `PyMuPDF`, `pyinstaller`가 필요합니다.

```bash
python omr_grader.py
python -m PyInstaller --onefile --windowed --name omr_grader --splash splash.png omr_grader.py
```

## English

OMR Grader is a Windows application for grading scanned 5-choice OMR sheets and producing Excel results. It supports image files and multi-page PDFs, aligns rotated sheets, detects student IDs and marked answers, and creates debug overlays for uncertain marks.

Version **v1.0.0** adds local grading history, preserved originals, re-analysis and re-grading previews, manual review, item analysis, and diagnostic recovery.

### Run

Double-click `dist/omr_grader.exe`; Python is not required for end users. Select the answer sheets, an xlsx/csv answer key, and an output folder. The result is written as `채점결과.xlsx` with a `debug/` folder.

### Build from source

```bash
python omr_grader.py
python -m PyInstaller --onefile --windowed --name omr_grader --splash splash.png omr_grader.py
```

## 버전 규칙

이 저장소는 Semantic Versioning(semver)을 단순하게 사용합니다.

- **MAJOR** `X.0.0`: 채점·검토의 핵심 작업 흐름, 보존 데이터 모델, 결과 형식이 크게 바뀌는 경우입니다. 예: 채점 이력·원본 보존·재검토 체계의 도입처럼 기존 사용 방식을 대대적으로 확장하는 변경입니다.
- **MINOR** `0.X.0` 또는 `X.Y.0`: 인식·검토 규칙, 보고서·비교 항목, GUI 작업 흐름이 의미 있게 늘어나는 경우입니다. 예: 새 인식 규칙, 새 분석 필드, 새 검토 화면입니다.
- **PATCH** `X.Y.Z`: 문구 보정, 오타 수정, 설명 정리, 작은 기준 보완 또는 호환 가능한 버그 수정입니다.

예: `v1.0.0` 정식 운영판, `v1.1.0` 새 검토·분석 규칙, `v1.1.1` 문서·표시 보정.

## 버전 이력

전체 변경 목록은 [CHANGELOG.md](CHANGELOG.md)를 확인하세요.

| 버전 | 의미 |
|---|---|
| `v1.0.0` | 채점 이력·원본 보존·재분석·재채점·수동 검토·문항 분석·진단 복구를 포함한 정식 운영판 |
| `v0.2.0` | 기본 Windows GUI를 도입한 초기 사용판 |
| `v0.1.0` | OMR 인식, 채점, Excel 출력, 디버그 오버레이를 갖춘 최초 작업판 |

제작: 조승현 (kaic21@gmail.com)
