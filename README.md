# OMR Grader 2.1.0

Windows에서 스캔한 OMR 답안지를 인식하고 검토·채점·보관하는 포터블 데스크톱
애플리케이션입니다.

## 주요 기능

- 이미지 및 다중 페이지 PDF 답안지 가져오기
- 방향 보정, 마킹 판독과 진단 이미지 생성
- `.omrtemplate` 프로필과 Excel 명단·정답표 가져오기
- 자동 채점, 수동 수정과 재채점
- 시험 관리, 학생별 상세 결과와 문항 분석
- 응답 결과와 채점 결과 Excel 생성
- 시험 백업·복구와 휴지통 관리

## 포터블 EXE 사용

Python 설치는 필요하지 않습니다. 쓰기 가능한 전용 폴더를 만들고 EXE를 그 안에
두는 방식을 권장합니다.

```text
D:\OMR-Grader\
└─ OMR Grader.exe
```

`C:\Program Files`처럼 쓰기가 제한될 수 있는 위치나 읽기 전용 저장장치는 피하세요.
프로그램은 EXE가 있는 폴더를 포터블 루트로 사용하므로 EXE만 따로 옮기면 기존 설정과
시험 기록이 따라가지 않습니다.

## 자동 생성되는 파일과 폴더

최초 실행 후 포터블 루트에 다음 항목이 생성됩니다.

```text
D:\OMR-Grader\
├─ OMR Grader.exe
├─ config.json
├─ Profiles\
├─ Data\
│  ├─ dashboard_index.json
│  ├─ <시험별 세션 폴더>\
│  │  ├─ 01원본스캔\
│  │  ├─ 02채점결과이미지\
│  │  ├─ 정답표원본\
│  │  └─ generations\
│  └─ _휴지통\
├─ logs\
└─ .locks\
```

- `config.json`: 프로그램 설정
- `Profiles/`: 가져온 OMR 프로필
- `Data/`: 시험 세션, 응답과 채점 결과
- `Data/<시험별 세션 폴더>/01원본스캔/`: 원본 PDF와 정규화 스캔 이미지
- `Data/<시험별 세션 폴더>/02채점결과이미지/`: 수동 검토용 채점 이미지
- `Data/<시험별 세션 폴더>/정답표원본/`: 보존된 원본 정답표가 있는 경우
- `Data/_휴지통/`: 삭제한 시험 세션
- `logs/`: 날짜별 프로그램 로그
- `.locks/`: 안전한 동시 실행과 저장을 위한 내부 잠금

내부 JSON과 잠금 파일은 프로그램이 관리하므로 직접 수정하거나 삭제하지 마세요.
백업 파일(`*.omrbak`)과 내보낸 결과(`*.xlsx`)는 저장 창에서 사용자가 지정한 위치에
생성됩니다.

## 다른 PC나 폴더로 이동

1. 프로그램을 완전히 종료합니다.
2. `OMR Grader.exe`가 들어 있는 포터블 루트 폴더 전체를 복사합니다.
3. 새 PC의 쓰기 가능한 폴더에 붙여넣습니다.
4. 복사한 폴더 안의 EXE를 실행합니다.

설정과 시험 기록을 유지하려면 `config.json`, `Profiles/`, `Data/`, `logs/`를 EXE와
함께 옮겨야 합니다. 개별 시험은 프로그램의 **백업하기**와 **백업 복구하기** 기능을
사용할 수 있습니다.

## 소스에서 실행

요구 사항은 Windows 11과 Python 3.12 이상입니다.

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python main.py
```

소스 실행 시에는 `main.py`가 있는 저장소 루트가 포터블 루트가 됩니다.

## 포터블 릴리즈 빌드와 검증

PyInstaller onedir 빌드와 검증은 같은 포터블 계약을 사용합니다. 빌드 결과의
`OMR Grader\` 폴더 전체(`OMR Grader.exe`와 `_internal\`)를 배포 단위로 취급하며,
EXE만 따로 복사하지 않습니다.

```powershell
& .\tools\build-portable-folder.ps1
& .\tools\verify-portable-folder.ps1 `
    -ReleaseRoot .\dist\OMR-Grader-fixed14-YYYYMMDD `
    -Smoke Both
```

빌드 폴더에는 `release-receipt.json`이 생성됩니다. 영수증은 제품 버전, Git HEAD,
포함 파일 목록과 각 파일의 SHA-256을 묶으며, 검증기는 ZIP 내용과 실제 포터블 실행을
함께 확인합니다. `packaging\verify_release.py`의 단일 EXE 릴리즈 계약과는 별개로,
현재 제품의 공식 검증 기준은 이 onedir 검증 명령입니다. 정상 종료까지 엄격하게
확인하려면 `-StrictShutdown`을 추가합니다. 기본 검증에서는 창 표시와 임시 프로세스
정리 결과를 기록하고, 종료 신호의 불확실성은 별도 진단으로 남깁니다.

## LLM에게 도움 요청하기

설치, 이동, 백업 또는 복구 방법을 LLM에게 질문할 때는 저장소의
[`AGENTS.md`](AGENTS.md)를 먼저 읽고 답하도록 요청하세요. 포터블 경로 구조와
안전한 안내 원칙이 정리되어 있습니다.

## 저장소 구성

- `src/omr_grader/`: 제품 소스
- `main.py`: 소스 실행 진입점
- `pyproject.toml`: 런타임 의존성과 설치 메타데이터
- `AGENTS.md`: LLM용 설치·운영 지침
