# OMR Grader 2.0

Windows에서 스캔한 OMR 답안지를 인식하고 검토·채점·보관하는 데스크톱 애플리케이션입니다. 2.0은 기존 단일 파일 애플리케이션을 PySide6 기반 패키지 구조로 전면 재작성한 메이저 버전입니다.

## 주요 기능

- 이미지 및 다중 페이지 PDF 답안지 가져오기
- 방향 보정, 정규화, 마킹 판독 및 진단 오버레이
- `.omrtemplate` 프로필 가져오기와 기본 프로필 관리
- XLSX/XLSM 정답표와 명단 가져오기
- 자동 채점, 수동 답안 수정 및 재채점
- 시험 대시보드, 학생별 상세 결과 및 문항 분석
- 응답·점수·통합 Excel 결과 생성
- 세션 보존, 휴지통, 백업 및 복원
- 취소·장애 복구와 원자적 파일 저장

> OMR 프로필과 실제 시험 자료는 저장소에 포함하지 않습니다. 사용자가 적법하게 보유한 파일을 직접 가져와야 합니다.

## 요구 사항

- Windows 11
- 소스 실행 시 Python `3.12 이상`
- 최종 사용자는 단일 EXE 배포본 사용 시 Python이 필요하지 않습니다.

## 소스에서 실행

PowerShell에서 다음 명령을 실행합니다.

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python main.py
```

`constraints/windows-py312.lock`은 재현 가능한 오프라인 Windows 릴리스 빌드 전용이며,
일반 소스 실행의 Python 상한을 제한하지 않습니다.

## 검증

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check src tests packaging tools
.venv\Scripts\python -m mypy
```

현재 2.0 소스 후보는 전체 테스트 1,012개 통과, 13개 건너뜀 상태에서 Ruff와 strict mypy 검사를 통과했습니다.

## Windows 실행 파일 빌드

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-onefile.ps1
powershell -ExecutionPolicy Bypass -File tools\run-packaged-tests.ps1
```

재현 가능한 애플리케이션 wheel, 공급망 manifest 및 릴리스 번들 검증 도구는 `tools/`와 `packaging/`에 있습니다. `build/`, `dist/`, EXE 및 로컬 검증 증빙은 Git에 포함하지 않습니다.

## 데이터와 보안

- 실제 답안지, 학생 명단, 채점 결과, 세션, 백업 및 내보내기 파일을 커밋하지 마십시오.
- 인증서, 코드서명 키, 환경 파일과 자격 증명은 저장소 밖에서 관리하십시오.
- `.omrtemplate`과 `OCR100.pdf`는 권리 및 개인정보 보호를 위해 저장소에서 제외합니다.
- 로컬 에이전트 상태, 빌드 결과와 내부 검증 증빙도 `.gitignore`로 제외합니다.

## 2.0 출시 상태

소프트웨어 개발과 자동 검증은 완료됐지만 다음 외부 검증은 아직 남아 있습니다.

- 실제 스캔 OMR 100장 이상의 정확도 검증
- 외부 참고 자료의 사용·재배포 권리 확인
- Windows 실행 파일 코드 서명
- 독립 Windows 11 환경에서의 오프라인·Defender·SmartScreen 검사
- 최종 배포 승인

따라서 현재 소스는 2.0 개발 완료 후보이며, 서명된 정식 배포본으로 간주해서는 안 됩니다.

## 저장소 구성

- `src/omr_grader/`: 제품 소스
- `tests/`: 단위·통합·GUI·장애·보안·성능·패키지 테스트
- `packaging/`: PyInstaller와 릴리스 검증 코드
- `tools/`: 빌드, 공급망 및 검증 명령
- `constraints/`: 재현 가능한 오프라인 Windows 릴리스용 Python 3.12 잠금 의존성
- `requirements/`: 직접 의존성 목록

변경 내역은 [CHANGELOG.md](CHANGELOG.md), 내부 구조는 [DESIGN.md](DESIGN.md)를 참고하십시오.
