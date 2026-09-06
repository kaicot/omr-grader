# OMR Grader 2.1.0 재검토 보고서

검토일: 2026-09-06 (Asia/Seoul)  
기준: `main`, `fd1e0ddedda49c824344904aabb70ad3af5ac657`  
대상 배포본: `OMR-Grader-fixed20-20260810`  
판정: **수정 필요 — 기능 구현과 배포 검증을 모두 완료한 상태로 보기 어려움**

## 1. 결론

채점의 기본 수치 계산, 학번 앞자리 0 보존, 동점 석차, 새 정답표를 이용한 재채점,
시험한 백업·복구 경로는 정상 동작했다. 실제 EXE에서도 합성 응답 4명을 가져와
채점하고 학생별 결과를 확인했다.

그러나 **OMR 인식·채점 후 수동 수정에 필요한 자료가 생성되지 않아 수정 서비스를
사용할 수 없는 문제**를 재현했다. 또한 **새 체크아웃에서 빌드할 수 없고,
검증기가 배포 ZIP의 내용 불일치를 통과시키는 문제**가 확인됐다.
Windows 쓰기 권한이 거부된 폴더를 쓰기 가능으로 판단하는 문제도 있다.

따라서 이전의 일괄적인 개발 완료 판단은 정정해야 한다. 기본 기능 상당 부분과
기존 배포 파일의 무결성은 확인됐지만, 수동 보정과 배포 검증의 신뢰성에 보완이 필요하다.
프로그램 소스·공식 검증기·운영 정책은 이번 검토 중 수정하지 않았다.

## 2. 실행 환경과 범위

- Windows 11 `10.0.26200`, Python `3.12.13`, PowerShell `7.6.5`.
- 로컬 개발 도구: PyInstaller `6.14.1`, pytest `8.4.1`, pytest-qt `4.4.0`,
  Ruff `0.11.13`, mypy `1.16.1`.
- Git 원격 `origin/main`도 위 기준 커밋과 일치함을 검토 종료 시 확인했다.
- 동기화 경로 밖의 Git 새 체크아웃과 새 Python 가상환경을 만들었다.
- 시험 데이터는 별도로 만든 합성 답안·정답표와 기존 합성 테스트 fixture를 사용했다.
- 실제 EXE 검토는 ZIP 해제 사본에서 수행했다. 읽기 전용 검사는 기존 관리 폴더와
  설정이 있는 배포 폴더 사본에 재귀 쓰기 거부 ACL을 적용한 경우다.
- GUI 자동 테스트는 `QT_QPA_PLATFORM=offscreen`으로 수행했다. 실제 EXE 창 조작은
  별도로 수행했으며 두 결과를 구분했다.
- Impeccable의 접근성·대비·크기 적응 항목을 참고했다. 모바일 전용 native 점수표를
  Windows 앱에 그대로 적용하지 않았다.

검토 증거: [`artifacts/review-20260906/attempt-01/`](../../artifacts/review-20260906/attempt-01/).
이 문서에서 아래 파일명을 언급하는 증거는 모두 해당 디렉터리에 있다.

## 3. 우선 수정해야 할 결함

### R01 · P1 — 채점 후 OMR 답안 수동 수정이 실패함

**영향:** 인식 결과를 확인하고 잘못 읽힌 답안을 수정하는 핵심 흐름이 막힌다.
Excel 입력 사례뿐 아니라 이미지 인식으로 만든 세션에서도 재현했다.

재현 절차:

1. 합성 `.omrtemplate`와 PNG를 `ScanRuntime`으로 인식하여 세션 revision 1을 만든다.
2. 실제 정답표 Excel로 채점하여 revision 2를 만든다.
3. `DetailRepository.read_correction_snapshot(session_id, 2)` 또는
   `CorrectionApplicationService.preview_corrections()`를 요청한다.

실제 결과:

```text
scan_succeeded = true
grade_succeeded = true
review_geometry_exists = true
review_jpeg_count = 1
projection_exists = false
CORRECTION_MATERIALIZATION_INVALID
underlying error: required correction artifacts are absent
```

원인: `GradingUseCase.regrade()`는 mutation의 `projection_request`를 `None`으로 보낸다.
`GenerationMaterializer`는 projection이 있을 때만 `projection_request.json`을 만든다.
반면 수동 수정 reader는 이 파일을 필수로 요구한다. 저장 용량을 줄인 뒤의 데이터
계약과 수정 reader의 계약이 맞지 않는다. 처음 채점한 revision 2와 재채점한
revision 3 양쪽에서 확인했다.

- 위치: `src/omr_grader/application/grading_use_case.py:94`,
  `src/omr_grader/infrastructure/generation_materializer.py:213`,
  `src/omr_grader/infrastructure/detail_repository.py:228`.
- 증거: `omr-image-correction-results.json`, `storage-followup-results.json`,
  `stage34-results.json`; 재현 코드 `review_omr_correction.py`.
- 권장 수정: 채점·압축 후에도 수정에 필요한 확정 응답·좌표·이력을 읽는 계약을
  통일한다. 기존 fixed20 세션 호환성을 포함해 수정 → 저장 → 재열기 → 재채점 →
  백업·복구까지 연결한 회귀 검사를 추가한다.

### R02 · P1 — Git 새 체크아웃만으로 배포 빌드·검증을 재현할 수 없음

새 가상환경에서 README의 `pip install -e .`는 성공했다. 그 상태로 빌드하면
`No module named PyInstaller`가 발생한다. PyInstaller가 설치된 기존 interpreter를
명시해 한 단계 더 진행해도 다음 오류가 발생한다.

```text
ERROR: Spec file ".../checkout/packaging/OMR_Grader.spec" not found!
```

`build-portable-folder.ps1`은 위 spec을 필수로 참조하지만 `packaging/`은 Git에서
제외되어 있다. 개발·빌드 의존성 설치 방법도 제공되지 않는다. Git에 포함된 배포
테스트도 미추적 `run-packaged-tests.ps1`, `smoke-release.ps1`에 의존한다.

- 새 체크아웃의 추적 테스트 실행: **3 failed, 2 passed, 1 skipped**.
- 이 테스트 실행에는 pytest가 설치된 기존 interpreter를 명시적으로 제공했다.
  새 가상환경의 README 설치만으로 개발 도구까지 준비된다는 뜻은 아니다.
- 여기서 2개 성공은 잘못된 manifest에 대해 비정상 종료를 기대하는 사례다.
  누락된 스크립트의 실행 실패도 그 조건을 만족하므로 정상 배포 검증의 근거가 아니다.
- 위치: `tools/build-portable-folder.ps1:19`, `pyproject.toml:10`, `.gitignore:68`,
  `tests/packaged/test_release_orchestration_scripts.py:143` 및 `:225`.
- 증거: `fresh-install.log`, `fresh-build-default.log`,
  `fresh-build-supplied-pyinstaller.log`, `fresh-tracked-tests.xml`.
- 권장 수정: 필요한 spec·검증 도구·개발 의존성·합성 fixture를 Git에서 함께
  관리하고, 새 가상환경에서 README 명령을 그대로 검증한다.

### R03 · P1 — 검증한 폴더와 배포 ZIP의 내용이 달라도 PASS

정상 통제 fixture와 변형 fixture에 현재 공식 verifier를 실행했다. 정상 폴더의
파일·receipt는 그대로 두고 ZIP 안의 EXE 바이트만 바꾸고 sidecar를 새 ZIP에 맞췄을 때도
exit 0/PASS가 나왔다. ZIP 내부 receipt를 `{}`로 바꾸어도 마찬가지였다.

| 통제 조건 | verifier의 실제 결과 | 해석 |
| --- | --- | --- |
| 기준 fixture | PASS | 정상 대조군 |
| 디스크 파일 바이트 변조 | 거부 | 정상 검출 |
| 디스크 파일 누락 | 거부 | 정상 검출 |
| 잘못된 HEAD / 버전 / sidecar hash | 각각 거부 | 정상 검출 |
| `_internal/`에 미등록 DLL 추가 | PASS | 실제 전체 파일 목록 대조 누락 |
| sidecar 파일 제거 | PASS | sidecar 누락을 허용 |
| ZIP의 EXE 내용 변경, 파일명 유지 | PASS | ZIP 내용과 payload hash를 연결하지 않음 |
| ZIP 내부 receipt 불일치 | PASS | 외부·내부 receipt 내용 대조 누락 |
| receipt의 build script/spec hash 불일치 | PASS | 기록된 빌드 hash를 검증하지 않음 |
| 별도 executable record의 hash/size 불일치 | PASS | 별도 record 내부 일관성 검증 누락 |
| 같은 파일명을 receipt·ZIP에 중복 기재 | PASS | 중복을 거부하지 않음 |
| receipt에 `../../parent-sentinel.txt` 기재 | PASS | payload 밖의 통제 파일을 읽는 상대 경로를 허용 |

이 시험의 파일과 부모 경로 sentinel은 모두 검토 전용 임시 디렉터리 안에 만들었다.
가짜 EXE를 실행하지 않았으며 구조 검증(`-Smoke None`)의 거부 성능을 시험한 것이다.

- 위치: `tools/verify-portable-folder.ps1:59`, `:74`, `:84`.
- 증거: `stage12-results.json`, `fixture-*.log`; 재현 코드 `review_stage12.py`.
- 권장 수정: 엄격한 경로·중복 검증, 실제 파일 목록의 완전 대조, ZIP 각 항목의
  내용·CRC·hash 및 내부 receipt 대조를 수행한다. 기록되는 모든 hash의 검증 용도를
  명시하고, 무결성 검증에 필수인 증거 누락은 별도로 거부한다.

### R04 · P2 — Windows ACL 쓰기 거부를 쓰기 가능으로 오판

실행 사본에 읽기·실행은 허용하고 파일 쓰기를 거부하는 ACL을 적용했다.
독립적인 파일 생성 probe가 `UnauthorizedAccessException`으로 실패했지만,
`probe_root_capability()`는 `write_enabled=True`를 반환했다.

실제 EXE에서도 응답 가져오기 등 변경 버튼이 활성화되어 있었다. 읽기 전용 안내
대신 다른 작업이 같은 세션을 사용한다는 메시지가 나타났다.

- 원인: `is_path_writable()`이 `os.access(path, os.W_OK | os.X_OK)`만 사용하고
  이 시험에서 실제 ACL 거부를 반영하지 못했다.
- Windows ACL 자체는 작동하여 파일 변경을 막았다. 전후 **870개 파일 내용 hash가
  동일**했으므로 권한 우회나 데이터 손실을 확인한 것은 아니다.
- 위치: `src/omr_grader/infrastructure/paths.py:213`,
  `src/omr_grader/infrastructure/capabilities.py:69`.
- 증거: `readonly-environment.json`, `readonly-capability.json`, `readonly-after.json`,
  `ui-observations.json`; ACL은 검사 후 원복했다.
- 권장 수정: 실제 Windows 권한을 반영하는 판정과 실패 시 UI의 쓰기 권한 해제를
  일치시킨다. 기존 config/Data가 있는 경우도 테스트한다.

### R05 · P2 — read-only smoke가 실행 실패와 파일 변경을 놓침

현재 smoke 함수를 격리한 unit mock에서 다음 세 상황이 모두 성공으로 처리됐다.

1. 프로세스 생성이 `PermissionError`로 거부됨.
2. 프로그램이 exit code 17로 즉시 종료됨.
3. 기존 `config.json`의 내용을 바꾸되 파일 이름은 유지함.

함수가 시작 성공·종료코드·파일 내용 대신 최상위 이름만 비교하기 때문이다.
또한 writable smoke의 기본 모드에서는 종료 타임아웃 후 강제 종료해도 상위 verifier가
PASS를 출력한다. 이 동작은 현재 AGENTS의 선택적 종료 진단 정책이지만,
그 PASS를 정상 종료 또는 실제 저장 성공의 증명으로 설명해서는 안 된다.

- 위치: `tools/smoke-portable-onedir.py:120`, `:154`,
  `tools/verify-portable-folder.ps1:115`.
- 증거: `stage12-results.json`의 `readonly-mock-*`, `strict-shutdown.log`.
- 권장 수정: 초기화 완료와 정상적인 읽기 전용 화면을 확인하고, 전후 하위 파일
  내용도 비교한다. 구조·실행·저장·정상 종료·강제 정리 판정을 각각 출력한다.

### R06 · P2 — 정답표 검증과 원본 보존 사이 파일 변경을 감지하지 못함

정답표 loader가 정상 검증을 마친 직후 파일을 다른 합성 정답표로 바꾸는 상황을
주입했다. 검증 결과의 Q1은 1, 보존될 원본의 Q1은 2로 달랐지만 서비스는 Ok를 반환했다.
snapshot의 원본 SHA-256과 `source_bytes`의 SHA-256도 서로 달랐다.

- 위치: `src/omr_grader/application/answer_key_use_case.py:23`, `:28`,
  `src/omr_grader/application/dto.py:755`.
- 영향: 같은 파일을 다른 프로그램에서 수정하는 짧은 경쟁 구간에서, 채점에 사용한
  정답과 보존한 원본 정답표가 달라질 수 있다. 이 경쟁 구간을 실제 외부 편집기로
  발생시킨 것은 아니며 loader 사이의 변경으로 결정적으로 재현했다.
- 증거: `additional-results.json`의 `answer-key-change-between-reads`.
- 권장 수정: 한 번 읽어 고정한 바이트를 검증과 원본 보존에 함께 사용하거나
  hash 불일치를 명시적으로 거부한다.

### R07 · P2 — 빌드 실패 전에 지정한 WorkRoot의 기존 파일을 삭제

검토용 `owned-build-scratch/keep.txt` 하나만 만든 뒤, 누락된 spec을 가진 새
체크아웃의 빌드 명령에 그 WorkRoot를 전달했다. 빌드는 실패했지만 sentinel은 삭제됐다.
코드에는 WorkRoot 소유 여부·허용 범위를 확인하는 단계가 없다.

- 위치: `tools/build-portable-folder.ps1:57`.
- 증거: `workroot-probe.json`, `workroot-probe.log`.
- 권장 수정: 필수 입력을 먼저 확인하고, 빌드 시도에 속하는 검증된 경로만 정리한다.
  기존 폴더를 잘못 지정했을 때 파일을 잃지 않도록 보호한다.
- 삭제된 것은 이 재현을 위해 만든 sentinel뿐이며 사용자 파일은 삭제하지 않았다.

### R08 · P2 — 작은 화면·높은 배율에서 최소 창 크기가 화면보다 큼

MainWindow의 최소 클라이언트 크기가 논리 픽셀 `1280×800`으로 고정되어 있다.
offscreen Qt resize 시험에서 다음을 확인했다.

| 가정한 화면 / 배율 | 요청한 논리 크기 | 실제 창 크기 | 창틀을 빼고도 적합한가 |
| --- | --- | --- | --- |
| 1366×768 / 100% | 1366×768 | 1366×800 | 아니오 |
| 1920×1080 / 100% | 1920×1080 | 1920×1080 | 예 |
| 1920×1080 / 125% | 1536×864 | 1536×864 | 예 |
| 1920×1080 / 150% | 1280×720 | 1280×800 | 아니오 |
| 1920×1080 / 200% | 960×540 | 1280×800 | 아니오 |

- 위치: `src/omr_grader/ui/main_window.py:76`.
- 증거: `additional-results.json`의 `layout`.
- 이 표는 논리 크기 resize 시험이다. 실제 Windows 배율을 바꿔가며 측정한 것은
  아니며, 창틀·작업 표시줄까지 고려하면 가용 영역은 더 작다.
- 권장 수정: 화면의 가용 논리 크기를 기준으로 초기·최소 크기를 정하고, 핵심 버튼을
  스크롤 또는 적응형 배치로 접근 가능하게 만든다.

## 4. 검증·운영 자료의 추가 정비 항목

### 테스트와 구현의 시점 불일치

로컬 81개 테스트 파일 중 Git 추적은 2개다. 로컬 테스트를 실행하면 `Data` 대신
이전 `OMR_Grader` 경로를 기대하는 사례, 이전 이미지 폴더명을 기대하는 사례,
제거된 `DashboardUseCase`를 import하는 사례가 남아 있다.
이 세 항목은 현재 운영 문서와 의도된 기능 변경을 기준으로 보면 테스트 갱신 문제다.
프로그램을 이전 구조로 되돌려 통과시킬 항목이 아니다.

수동 보정 결함은 다수 테스트가 통과해도 놓칠 수 있었다. 인식·채점·저장 압축과
수동 보정을 실제 서비스로 연속 실행하는 테스트가 필요하다.

### PowerShell 버전 안내

현 스크립트가 사용하는 `[Convert]::ToHexString()`은 이 호스트의 Windows
PowerShell `5.1.26100.9168`에는 없었다. 직접 메서드 목록 조회로 확인했다.
PowerShell 7.6.5에서는 구조 검증이 실행됐다.

5.1의 스크립트 전체 실행은 호스트 실행 정책에서도 차단되었다. 시스템 정책은
변경하지 않았다. 문서에 필요한 PowerShell 버전을 명시하거나 5.1 호환 구현을 제공해야 한다.
증거: `windows-powershell51.log`, `windows-powershell51-api.log`.

### 의존성 보안 공지와 배포 고지

배포된 Qt6Svg DLL의 파일 버전은 `6.9.1.0`이다. Qt는 이 버전이 포함된 범위에 대해
SVG marker 처리의 서비스 거부 취약점 `CVE-2026-6210`을 공지했다.
앱에서 관찰한 SVG 사용은 번들 아이콘이며, 신뢰할 수 없는 SVG가 해당 코드에 도달하는
공격 경로는 이번에 입증하지 않았다. 적용 가능성과 패치 계획을 확인할 유지보수 항목이다.
[Qt 공식 보안 공지](https://www.qt.io/blog/security-advisory-type-confusion-and-heap-buffer-overflow-vulnerability-in-qt-svg-marker-handling).

receipt 파일명 목록에서 라이선스·COPYING·NOTICE 검색에 잡힌 것은 NumPy LICENSE뿐이었다.
PyMuPDF는 AGPL 또는 상용 라이선스를 제공하므로, 프로젝트에서 선택한 조건과 배포
고지·소스 제공 방식을 확인해야 한다. 상용 계약 보유 여부는 확인하지 않았고,
법적 위반 여부를 판정한 것은 아니다.
[PyMuPDF 공식 라이선스 안내](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright).

## 5. 통과한 검증과 제한

### 자동 테스트

초기 실행은 `test_combined_book.py`의 수집 오류로 중단됐다. 이후
`--continue-on-collection-errors`로 같은 오류를 유지하며 나머지 검사를 실행했다.
실패 테스트를 수정하거나 제외하지 않았다.

| 그룹 | 통과 | 실패 | 건너뜀 | 수집 오류 |
| --- | ---: | ---: | ---: | ---: |
| 단위 | 669 | 2 | 7 | 0 |
| 통합 | 88 | 0 | 1 | 1 |
| GUI / offscreen | 143 | 0 | 0 | 0 |
| 보안 | 76 | 0 | 4 | 0 |
| 저장 실패 주입 | 18 | 0 | 0 | 0 |
| 패키지 | 29 | 0 | 2 | 0 |
| 소계 | **1023** | **2** | **14** | **1** |
| 성능 하네스 별도 실행 | 36 | 0 | 0 | 0 |

Ruff는 통과했고 mypy는 84개 소스 파일에서 오류가 없었다. 성능 하네스 36개 통과는
측정 도구·프로세스 정리 등의 검사 결과이며 실제 대량 채점 성능을 뜻하지 않는다.
skip 사유에는 Windows에서 지원되지 않는 POSIX descriptor 검사, 심볼릭 링크 생성
권한 부족, OneDrive reparse 경로, release 환경변수 미지정 등이 있다.
자세한 사유는 `additional-results.json`에 기록했다.

### 독립 합성 자료와 실제 EXE

- 100문항, 합성 응답 4명: **100 / 90 / 90 / 0점**, 석차 **1 / 2 / 2 / 4**.
- 실제 EXE의 대시보드: 4명, 평균 70점, 최고 100점, 최저 0점. 학생별 화면과 일치했다.
- 학번 `00000001` 등 앞자리 0을 보존했다.
- Q1의 정답만 변경한 재채점: **99 / 89 / 89 / 1점**, 독립 기대값과 일치했다.
- `0.1 + 0.2 = 0.3`의 소수 배점, 복수 정답의 정확한 집합 일치,
  모두 정답·미출제·불확실 답 처리의 선정 사례가 기대값과 일치했다.
- 오래된 revision으로 재채점 요청은 거부됐고 CURRENT가 유지됐다.
- 채점 완료 세션의 백업·복구, 복구 후 재채점, 충돌 복구 거부와 기존 점수 보존을 확인했다.
- 합성 PNG를 실제 ScanRuntime으로 인식·채점하여 JPEG 검수 이미지를 만들었고,
  그 세션의 백업·복구도 성공했다. 이 세션의 수동 수정은 R01로 실패했다.

증거: `stage34-results.json`, `storage-followup-results.json`,
`omr-image-correction-results.json`, `ui-observations.json`.

### 기존 산출물 무결성

공식 구조 검사와 별도로 실제 payload 816개의 hash, ZIP 각 payload의 hash,
ZIP CRC, ZIP 내부·외부 receipt의 바이트 일치를 확인했다. 불일치는 없었다.

```text
EXE SHA-256
77e63337e4d4f05814cb291ff1176394cbae06059a8f885a5f02f91ce3f63594

ZIP SHA-256
a7fa784a1566a3ef1e45b347afcb271d02356d79b2bbb66f72c9e4ca8e5fc2a7
```

검증 도구에 허점이 있다는 사실과 기존 fixed20 파일이 변조됐다는 주장은 다르다.
이번에 검사한 fixed20의 기록된 payload와 ZIP 내용은 일치했다.

### 종료·권한·화면

- 준비가 끝난 메인 창 및 깨끗한 상세 결과 화면에서 Alt+F4로 종료하고,
  이후 OMR 프로세스가 남지 않았음을 확인했다.
- 공식 `-Smoke Writable -StrictShutdown`은 **30초 close timeout으로 실패**했다.
  창 준비·스플래시 구분과 종료 요청 시점 문제는 가설이며 원인을 확정하지 않았다.
- 읽기 전용 ACL 사본의 실제 실행은 성공했으나 모드·버튼 판정은 R04처럼 잘못됐다.
  파일 전후 무변경은 별도 hash 비교로 확인했다.
- 실제 화면에서 라이트·다크 테마와 접근성 이름을 확인했다. 주요 토큰의 텍스트 대비는
  라이트 4.97~14.10, 다크 8.44~12.32였다. 모든 화면·상태의 접근성 인증은 아니다.

### 정량 인식 정확도와 부하

정규화된 1000×1000 합성 격자 한 패턴에 대한 100문항과 학번 판독은 기대값과 일치했다.
이를 반복해 측정한 격자 판독 시간은 다음과 같다.

| 반복 페이지 수 | 시간 | 기대값 불일치 |
| --- | ---: | ---: |
| 10 | 1.5635초 | 0 |
| 100 | 10.9409초 | 0 |
| 500 | 54.2299초 | 0 |

이 측정은 PDF 렌더링·방향 보정·디스크 저장·실제 EXE·서로 다른 실물 답안지를 포함하지
않는 **단일 합성 격자 반복 측정**이다. 전체 채점 처리량이나 실물 정확도로 일반화할 수 없다.

## 6. 미확정·미실행 항목

| 항목 | 상태 | 이유 / 필요한 다음 증거 |
| --- | --- | --- |
| 실물 답안지의 공식 정확도 gate | INCONCLUSIVE | 검색한 로컬 자료에서 독립 확정 100페이지 라벨·예측·승인 세트를 확보하지 못함 |
| 정상 종료 타임아웃의 근본 원인 | INCONCLUSIVE | 실제 사용자 종료는 성공, 즉시 종료 smoke는 실패; 초기화 단계별 추적 필요 |
| 수동 수정 후 저장·재채점·백업의 전체 연결 | FAIL / 이후 단계 미실행 | R01이 수정 단계 자체를 막음 |
| 실제 EXE 대량 PDF 처리와 최대 RSS·용량 증가 | NOT RUN | 이번 부하 측정은 격자 판독만 포함; 별도 다페이지 기준 자료로 측정 필요 |
| 실제 Windows 125/150/200% 배율별 화면 조작 | NOT RUN | OS 배율을 바꾸지 않았으며 논리 크기 resize만 측정 |
| 별도 일반 사용자 계정·다른 PC에서 실행 | NOT RUN | 현재 계정·현재 호스트에 한정; 새 가상환경·다른 드라이브의 사본 실행은 수행 |
| 취약 라이브러리에 대한 실제 공격 도달성 | INCONCLUSIVE | 보안 공지·버전 대조까지만 수행 |
| 라이선스 계약과 최종 배포 고지 충족 | INCONCLUSIVE | 적용 계약·배포 고지 묶음을 확인해야 함 |

현재 AGENTS의 기본 종료 진단 정책은 변경하지 않았다. 이 보고서는 공식 verifier의
PASS와 별도로 실제 확인된 동작·실패·제한을 설명한다.

## 7. 권장 수정 순서

1. **수동 수정 데이터 계약 복구:** 인식 → 채점 → 수정 → 저장 → 재채점의 실제 서비스
   연결을 먼저 고치고 기존 fixed20 세션을 포함해 확인한다.
2. **배포 검증과 재현성 정리:** spec·개발 의존성·필요 테스트를 보존하고, ZIP 내용·
   전체 inventory·경로·receipt를 검증한다. 빌드 scratch 보호도 함께 정리한다.
3. **Windows 권한 판정과 smoke 개선:** ACL 거부 시 UI 상태와 오류 안내를 일치시키고,
   실행 실패를 통과시키는 smoke와 합계 PASS 표현을 고친다.
4. **원본 정답표 일관성·작은 화면 대응:** R06·R08을 보완하고 독립 재현 시험을 유지한다.
5. **의존성·고지 정비 후 재검증:** 관련 보안 패치·라이선스 고지를 검토하고,
   확정 라벨과 실제 PDF 작업량으로 남은 정확도·성능 검사를 수행한다.
6. 수정본의 테스트·새 체크아웃 빌드·실제 EXE 검증이 끝나면 그 커밋에 연결된 새
   receipt와 검토 결과로 배포 여부를 다시 판단한다.

## 8. 증거와 작업 보존 상태

- 제품 소스, 공식 build/verify/smoke 스크립트, README, AGENTS 및 Git 커밋은 변경하지 않았다.
- 검토 계획·보고서와 ignored 검토 증거/재현 코드를 추가했다. 커밋·push·Release 업로드는 하지 않았다.
- 사용자 원본 세션 및 배포본을 삭제하거나 내부 수정하지 않았다.
- 시험에 사용한 읽기 전용 ACL은 복원했고 검토용 OMR 프로세스는 종료했다.
- 재현을 위한 새 체크아웃·가상환경·합성 입력·실행 사본은 다음 임시 경로에 보존했다.

```text
C:\Users\Administrator\AppData\Local\Temp\omr-review-20260906-385097a1\
```

재현 코드는 해당 실행 시도의 경로를 고정해 두었다. 다시 실행할 때에는 새로운
검토용 RUN/OUT 경로를 할당하여 기존 증거와 섞이지 않게 해야 한다.

주요 증거 파일:

```text
stage12-results.json                 빌드 재현성·verifier 변형 시험·독립 무결성 검사
full-tests-continuing.xml            1023 passed / 2 failed / 14 skipped / 1 collection error
performance-tests.xml               성능 하네스 36 passed
stage34-results.json                합성 채점·격자 판독·제한적 부하
storage-followup-results.json        수정 실패 추적·채점 세션 백업/복구
omr-image-correction-results.json    이미지 인식 경로에서의 수정 실패
additional-results.json              정답표 경쟁 조건·논리 크기·대비·skip 목록
readonly-environment.json            실제 쓰기 거부 확인
readonly-capability.json             프로그램의 잘못된 쓰기 가능 판정
readonly-after.json                  파일 무변경·ACL 복원
ui-observations.json                 실제 EXE 사용 흐름 관찰
strict-shutdown.log                  종료 추가 진단 실패
```
