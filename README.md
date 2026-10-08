# OMR Grader 4.2.0

Windows에서 스캔한 OMR 답안지를 인식하고 검토·채점·보관하는 포터블 데스크톱
애플리케이션입니다.

## 주요 기능

- 이미지 및 다중 페이지 PDF 답안지 가져오기
- 답안지 양식 자동 인식과 양식 기억(템플릿 파일 없이 스캔만 고르면 됨)
- 인쇄된 동그라미에 맞춘 장별 정렬(기울기·크기·위치·90°/180° 회전)과 마킹 판독
- 맞힌 답은 파란 원, 틀린 답은 빨간 원으로 표시한 채점 이미지
- 양식 확인, 판독, 채점의 단계별 진행 표시
- `.omrtemplate` 프로필과 Excel 명단·정답표 가져오기
- 자동 채점과 재채점(응답 결과 Excel을 고쳐 다시 가져와 채점 가능)
- 시험 관리와 학생별 상세 결과 보기
- 채점결과 Excel 하나에 채점결과(순번·이름순, 고른 답에 틀린 답 분홍·확인 필요
  노랑), 결과OX, 문항 분석(정답률·선택지 분포·변별도), 응답원본, 판독 근거(칸별
  판독 여유), 정답표, 색 설명 탭
- 여러 파트 시험의 합산 성적표: 학번 기준 합산, 통합 석차, 과목 구성 엑셀로 과목별
  점수와 합격 판정(선택). 과목 구성을 쓰면 첫 탭 '과목별 합격'에 과목별 만점·합격 최소
  점수, 미달 점수(주황), 판정(합격 초록·불합격 빨강), 미달 과목, 과목 통과 인원·평균
- 시험 관리 대시보드: 상태(채점 전·완료·확정, 확인 필요 장 수), 요약, 새로고침,
  채점하기, 결과 엑셀·폴더 열기
- 시험 백업·복구와 휴지통 관리
- 목차와 검색이 있는 도움말(F1, 지금 화면의 장부터 열림)
- 새 버전 안내(하루 한 번, 버전 번호만 확인)와 이전 버전 폴더에서 자료 가져오기

## 스캔 장수와 나눠 채점

한 번에 넣는 스캔은 200장 이하를 권장합니다. 스캔 한 장마다 메모리를 약 15 MiB씩 쓰므로
그보다 많으면 PDF를 나눠 따로 채점한 뒤 시험 관리에서 **합산 성적표**로 합치세요. 50명 안팎
한 반은 한 번에 넣어도 문제없습니다.

## 포터블 EXE 사용

Python 설치는 필요하지 않습니다. 쓰기 가능한 전용 폴더를 만들고 배포 폴더 전체를
그 안에 두는 방식을 권장합니다.

공식 배포 단위는 PyInstaller onedir 폴더 전체입니다. EXE와 `_internal\`을 함께
보관하고 이동해야 합니다.

```text
D:\OMR-Grader\
├─ OMR Grader.exe
└─ _internal\
```

`C:\Program Files`처럼 쓰기가 제한될 수 있는 위치나 읽기 전용 저장장치는 피하세요.
프로그램은 EXE가 있는 폴더를 포터블 루트로 사용하므로 EXE만 따로 옮기면 기존 설정과
시험 기록이 따라가지 않습니다.

## 자동 생성되는 파일과 폴더

최초 실행 후 포터블 루트에 다음 항목이 생성됩니다.

```text
D:\OMR-Grader\
├─ OMR Grader.exe
├─ _internal\
├─ config.json
├─ Profiles\
├─ Data\
│  ├─ dashboard_index.json
│  ├─ <시험별 세션 폴더>\
│  │  ├─ 01원본스캔\
│  │  ├─ 02채점결과이미지\
│  │  ├─ 정답표원본\
│  │  └─ generations\      (숨김 폴더)
│  └─ _휴지통\
├─ logs\
└─ .locks\
```

- `config.json`: 프로그램 설정
- `Profiles/`: 가져온 OMR 프로필과 양식 자동 인식으로 저장한 프로필
  (`자동양식_객관식<문항 수>문항_<6자리>.omrtemplate`)
- `Data/`: 시험 세션, 응답과 채점 결과
- `Data/<시험별 세션 폴더>/01원본스캔/`: 원본 PDF와 기울기·회전을 바로잡은 페이지
  이미지(4.0.2부터 흑백 JPEG)
- `Data/<시험별 세션 폴더>/<날짜>_<시각>_채점결과_<시험명>.xlsx`: 채점 후 결과 Excel.
  채점결과, 결과OX, 응답원본(다시 가져올 수 있는 형식), 정답표, 색 설명 탭이 있습니다.
  채점 전에는 `<날짜>_<시각>_응답결과_<시험명>.xlsx`가 있습니다. 날짜·시각은 시험을
  처음 만든(스캔한) 때라 재채점해도 파일명이 바뀌지 않습니다
- `Data/<시험별 세션 폴더>/02채점결과이미지/`: 수동 검토용 채점 이미지.
  `<순번>_<학번>_<이름>.jpg`이며 순번은 채점결과 탭과 같습니다
- `Data/<시험별 세션 폴더>/정답표원본/`: 보존된 원본 정답표가 있는 경우
- `Data/<시험별 세션 폴더>/generations/`: 채점할 때마다 그 시점 결과를 확정해 두는
  내부 기록입니다. 숨김 폴더이며 열거나 지우지 마세요. 시험 폴더 바로 아래의
  결과 Excel은 이 기록의 파일과 같은 파일(하드 링크)이라 공간을 두 번 쓰지 않습니다.
- `Data/_휴지통/세션/`: 삭제한 시험. 원래 시험 폴더 이름 그대로 들어갑니다
- 시험 폴더 이름은 `<yymmdd>_<HHMMSS>_<시험명>`(예: `261007_165225_26_1졸업고사p1`)이라
  날짜순으로 정렬됩니다
- `logs/`: 날짜별 프로그램 로그
- `.locks/`: 안전한 동시 실행과 저장을 위한 내부 잠금

내부 JSON과 잠금 파일은 프로그램이 관리하므로 직접 수정하거나 삭제하지 마세요.
백업 파일(`*.omrbak`)과 내보낸 결과(`*.xlsx`)는 저장 창에서 사용자가 지정한 위치에
생성됩니다.

## 다른 PC나 폴더로 이동

1. 프로그램을 완전히 종료합니다.
2. EXE와 `_internal\`을 포함한 포터블 루트 폴더 전체를 복사합니다.
3. 새 PC의 쓰기 가능한 폴더에 붙여넣습니다.
4. 복사한 폴더 안의 EXE를 실행합니다.

설정과 시험 기록을 유지하려면 `config.json`, `Profiles/`, `Data/`, `logs/`를 EXE와
함께 옮겨야 합니다. 개별 시험은 프로그램의 **백업하기**와 **백업 복구하기** 기능을
사용할 수 있습니다.

## 소스에서 실행

개발·빌드 환경은 Windows x64, Python 3.12(`>=3.12,<3.13`), PowerShell 7.2 이상입니다.
배포 EXE 실행에는 Python이 필요하지 않습니다.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,build]" -c constraints\windows-py312.lock
.venv\Scripts\python.exe main.py
```

`py -3.12`를 사용할 수 없거나 실행에 실패하는 호스트에서는 `uv`를 대체 경로로
사용합니다. 호스트 전역 Python 등록이나 런처를 수정하지 않습니다.

```powershell
uv venv --python 3.12 --seed .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,build]" -c constraints\windows-py312.lock
```

소스 실행 시에는 `main.py`가 있는 저장소 루트가 포터블 루트가 됩니다.

테스트와 정적 검사는 다음과 같이 실행합니다.

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check src tools
.venv\Scripts\python.exe -m mypy src
```

제약 파일은 루트 `constraints\windows-py312.lock`을 사용합니다. 프로젝트 메타데이터의
직접 버전 고정과 이 제약 파일은 해시 잠금이나 오프라인 재현성 증명이 아닙니다.

## 포터블 릴리즈 빌드와 검증

PyInstaller onedir 빌드와 검증은 같은 포터블 계약을 사용합니다. 빌드 결과의
`OMR Grader\` 폴더 전체(`OMR Grader.exe`와 `_internal\`)를 배포 단위로 취급하며,
EXE만 따로 복사하지 않습니다.

```powershell
& .\tools\build-portable-folder.ps1 -BuildNumber 21
& .\tools\verify-portable-folder.ps1 `
    -ReleaseRoot .\dist\OMR-Grader-fixed21-YYYYMMDD `
    -Smoke Both -StrictShutdown
```

OneDrive의 동기화 루트와 `dist`는 Windows Cloud reparse 경로일 수 있습니다. 빌드
가드는 모든 reparse ancestor를 거부하므로 이를 완화하지 말고, 저장소 체크아웃을
일반 쓰기 가능 폴더로 복사한 뒤 빌드합니다. 일반 저장소에서는 기본 `.\dist`가
유효합니다. 별도 출력 위치가 필요하면 기존 폴더를 지우지 않는 일반 부모를 지정합니다.

```powershell
$releaseRoot = 'D:\workspace\omr-grader-release'
& .\tools\build-portable-folder.ps1 `
    -DistRoot "$releaseRoot\dist" -WorkRoot "$releaseRoot\build" -BuildNumber 21
```

`WorkRoot`는 빌드가 새 고유 하위 폴더만 만드는 부모 경로입니다. 빌드 스크립트는
기존 파일과 부모 폴더를 지우지 않으며, 이전 산출물과 충돌하면 중단합니다.

빌드 산출물은 다음 3계층입니다. `release-receipt.json`은 EXE payload 밖의 외부
release 폴더에 두며, ZIP과 SHA-256 sidecar는 `dist` 바로 아래에서 그 release 폴더와
나란히 보존합니다.

```text
dist\
├─ OMR-Grader-fixed21-YYYYMMDD\
│  ├─ release-receipt.json
│  └─ OMR Grader\
│     ├─ OMR Grader.exe
│     └─ _internal\...
├─ OMR-Grader-fixed21-YYYYMMDD.zip
└─ OMR-Grader-fixed21-YYYYMMDD.zip.sha256
```

receipt 형식 2는 현재 Git HEAD, 빌드 입력, 제품 버전, 도구 버전과 payload 파일을
연결합니다. 이전 형식 1 receipt는 `LEGACY_AUDIT_ONLY`로만 판정하며 새 표준 receipt로
고쳐 쓰지 않습니다. `-Smoke None`은 구조 검사일 뿐이고,
`Writable`·`ReadOnly` 단독 또는 비엄격 검사는 전체 승인 판정이 아닙니다. 최종 기술
게이트는 `-ReleaseRoot <dir> -Smoke Both -StrictShutdown`입니다.

빌드·구조 검증 통과만으로 실제 앱의 초기 화면 준비, 설정 저장·재열기, 읽기 전용
동작과 정상 종료가 모두 입증되는 것은 아닙니다. 이 앱 검증과 strict close 결과는
별도 기록으로 확인해야 합니다.

## LLM에게 도움 요청하기

설치, 이동, 백업 또는 복구 방법을 LLM에게 질문할 때는 저장소의
[`AGENTS.md`](AGENTS.md)를 먼저 읽고 답하도록 요청하세요. 포터블 경로 구조와
안전한 안내 원칙이 정리되어 있습니다.

## 저장소 구성

- `src/omr_grader/`: 제품 소스
- `main.py`: 소스 실행 진입점
- `pyproject.toml`: 런타임 의존성과 설치 메타데이터
- `AGENTS.md`: LLM용 설치·운영 지침
