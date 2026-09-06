# 2.1.1 승인 후 실행 기록

사용자 승인: 2026-09-06. 원본 main은 fd1e0dd에 유지하며 구현은 별도 worktree에서 진행한다.
승인 범위는 수정·단계별 로컬 커밋·테스트·후보 재빌드다. main 병합·push·Release 게시 제외.

## 역할과 위치

- 통합 브랜치: fix/review-20260906
- 기준 계획 커밋: 04522055460a8542b784e46e407c9f3244d15ec5
- A: Terra high / Archimedes / 01a07510-57f6-78d3-843d-1a4936edeeb7
  - core worktree, R01/R04/R06의 runtime과 새 runtime 회귀 테스트.
- B: Terra high / James / 01a07510-6175-7553-8a76-e8fb44fd9fae
  - release worktree, R02/R03/R05/R07 및 기존 유효 테스트/개발 환경 정리.
- C: Terra high / Maxwell / 01a0754c-561d-7821-b7fe-b00726cd9f48
  - ui worktree. B 통합 후 main_window/scan/grading/theme와 Qt 의존성만 선행한다.
  - A의 detail_page/bootstrap/controller와 해당 테스트는 수정하지 않는다.
- S: Sol high / Singer / 01a07559-c276-7221-a9be-017895bf046b.
  - 호출 도구가 안전성 필터 오류를 반환하여 검토 결과를 받지 못함. 완료로 처리하지 않음.
  - 같은 요청을 다른 모델로 우회하지 않았으며, 독립 검토는 미완료로 남음.
- D: Luna medium / Popper / 01a0756e-22fd-7e41-b2c0-967e8af85c3a.
  - README/AGENTS/릴리즈 노트 및 2.1.1 버전 정리.
- 주 에이전트: 구현 코드는 직접 작성하지 않고 diff 검토·독립 검증·통합 및 최종 판정.

전체 하위 모델 동시 실행은 2개 이하, 추론 high 이하, 고속 설정 미사용.

작업 루트:
`C:\Users\Administrator\AppData\Local\Temp\omr-fix-20260906-1e3b2603`

## 독립 확인 자료

- 이전 검토에서 생성한 fixed20 형식 합성 세션 3종과 백업 2종을 frozen-fixed20에 보존.
- fingerprint: evidence/frozen-fixed20-manifest.json.
- 위 fixture는 구현 담당이 새로 만든 테스트와 별도로 통합 결과의 호환성 확인에 사용한다.
- 실물 100페이지 독립 라벨과 별도 라이선스 계약은 여전히 확보되지 않았으며,
  합성 검증이나 로컬 빌드만으로 실물 정확도/공개 배포 적합성을 단정하지 않는다.

## 현재 진행

- [x] 승인 및 모델 역할 기록
- [x] 독립 worktree와 보존 fixture 준비
- [x] A runtime 구현 검토/통합 (9ab3410까지)
- [x] B build/verifier 구현 검토/통합 (20f9445까지, 통합 a19dd49)
- [x] C UI/의존성 구현 검토/통합 (9e107fd까지)
- [ ] Sol 독립 검토와 지적 사항 해소 — 호출 도구 차단으로 미완료
- [x] Luna 문서/버전 정리
- [ ] 확정 커밋에서 전체 검사·후보 빌드·strict smoke

위 상태는 소스 확정 전 기록이다. 확정 커밋을 새로 복제한 후의 검사 결과와 실제
EXE/ZIP hash는 소스를 다시 변경하지 않고 다음 외부 산출물로 기록한다.

`D:\workspace\omr-grader-release-20260906\verification\FINAL_REPORT.md`

따라서 기술적 빌드·실행 게이트가 통과하더라도 Sol 독립 검토를 포함한 전체 계획이
완료됐다고 단정하지 않는다. main 병합·원격 push·공개 Release는 계속 승인 범위 밖이다.

## 독립 검증 중간 기록

- B: 새 `--no-local` 복제에서 테스트 수집 오류 0. 첫 전체 실행은 1011 passed,
  11 skipped, 구 경로 기대값 1 failed였으며 해당 기대값은 수정했다.
- B: 정상 대조군, ZIP 바이트/내부 receipt 변조, 추가 DLL, sidecar 누락, 중복 ZIP/JSON,
  경로 이탈, 링크, float schema, onedir 구성 누락, 제품 버전 불일치 및 실제 실행 불가
  EXE의 wrapper 실패/정리를 독립 검사했다. 13/13 통과.
  `evidence/release-oracle-d7uu2x2t/results.json`.
- 실제 Windows ACL 거부에서 probe 파일 작성이 PermissionError로 거부되고 앱 권한이
  write_enabled=false이며 파일 추가가 없음을 확인했다. 권한 제거 후 쓰기 성공.
- 실제 suspended Python parent가 child를 만든 뒤 0으로 종료하는 사례에서 job이 남은
  child를 식별하고 강제 정리 후 비어 있음을 확인했다. 이 증거는 EXE 정상 종료의 대체가 아니다.
- B 재실행 전체 테스트가 약 42개 진행 후 장시간 대기해 주 에이전트가 시작한 해당 검사
  프로세스 2개만 식별/종료했다. app_controller 단독 재실행은 33 passed/24.11초였다.
  `evidence/release-B4-controller.xml` 및 `.log`. 전체 통과는 아직 선언하지 않는다.
- A b0a9bb4: frozen fixed20 Excel 2종과 이미지 1종, 새 ScanRuntime 이미지에서
  수정/재채점/백업 복구, 580개 검수 좌표, 초기 채점 후 대형 파일 단일 저장을 확인했다.
  `evidence/independent-runtime-keafvsqy/results.json`.
- 추가 JPEG 갱신 검사를 적용하니 구/새 이미지 모두 수정 후 JPEG가 이전과 같았다.
  `evidence/independent-runtime-ebn4xabl/results.json`. A에 재수정 요청했으며 R01 최종 통합 전 게이트다.
- 통합 worktree에 처음 복사했던 구 combined-book/wheel/supply 테스트 4개는 현재 계약의
  추적 파일이 아니므로 `retired-hydrated-tests/`로 이동 보관했다. 삭제하지 않았고 원본 저장소도 보존했다.
- 화면 변경은 Impeccable의 기존 UI 유지/기능 접근성 원칙을 좁게 적용한다. Windows Qt 앱이므로
  모바일 플랫폼별 디자인 지침을 그대로 적용하지 않으며 OS 배율 변경을 자동화하지 않는다.

## 통합 후 추가 확인

- A 9ab3410의 JPEG 갱신까지 포함한 최신 독립 검사는 4종 모두 통과했다.
  `evidence/independent-runtime-u46h8zgm/results.json`.
- A+B 통합 83332ed에서 1051 passed, 12 skipped, 구 fake lease 계약 1 failed.
  이 fixture는 C가 실제 manifest allowlist를 갖추도록 수정했다. Ruff와 mypy(86개 소스) 통과.
- Qt 4종은 6.11.2로 확인하고 루트 constraints 파일을 단일 기준으로 정리했다.
  새 가상환경의 설치·pip check 및 제3자 고지 생성 검사를 수행했다.
- C의 양수/음수 화면 원점 검사에서 event loop 처리 후에도 native frame이 가용 영역에
  완전히 포함됨을 주 에이전트가 재확인했다. 실제 Windows 배율 변경 검증과는 구별한다.
- 합성 PDF 100페이지: 100 성공/0 실패, 50.787초, peak working set 430485504 bytes.
- 합성 PDF 500페이지: 500 성공/0 실패, 181.704초, peak working set 1722544128 bytes.
- 같은 500페이지 채점: 506.948초. Data는 1276683218→249125799 bytes,
  파일 5518→1521개. 원본 501개 hash 불변, generation 내 대형 이미지/PDF 중복 0.
  개발 중 소스의 단일 합성 실행 측정이며 최종 EXE 성능 인증이나 실물 정확도 증명이 아니다.
- 진단용 onedir(9df43eb, 버전 2.1.0 표기)은 852개 payload 파일의 ZIP 검증과
  `-Smoke Both -StrictShutdown`을 통과했다. 설정 6 저장/재실행 hash 일치, 읽기 전용
  파일 불변/ACL 복원, 3회 실행의 정상 종료가 확인됐다. 최종 2.1.1 후보는 별도로 다시 검사한다.
- 원본 OneDrive 경로가 Cloud reparse 속성이므로 빌드 가드를 완화하지 않고,
  최종 소스·검증·배포 후보는 일반 폴더 `D:\workspace\omr-grader-release-20260906`에 둔다.

## 최종 후보 검증 중 발견한 추가 보완

- 새 환경의 mypy 실행에서 누락된 `types-openpyxl==3.1.5.20260724`를 개발 의존성과
  동일한 루트 제약 파일에 고정했다. 검사 우회나 전역 환경 수정은 하지 않았다.
- 변경된 화면 크기/스크롤 계약을 5개 논리 크기와 실제 테스트 화면에서 검사하도록
  기존 스캔 화면 기대값을 갱신했다. 키보드 활성화와 전체 컨트롤 접근 검사는 유지했다.
- 이전 generation 이동이 실패하면 새 CURRENT를 다시 읽지 못했던 사례를 재현했다.
  e148f59는 유효한 retention 경계를 유지하면서 남아 있는 부모 hash를 계속 검사하도록
  보완했다. 최초/부분 이동 실패, 읽기·백업·재시도와 14개 잘못된 경계/부모 거부 검사를 추가했다.
- fixed21(cdf1c89)은 1064개 테스트와 strict smoke를 통과했으나 실제 GUI 저장 후 재실행에서
  `UI_NON_VALUE_PAYLOAD`와 빈 목록이 관찰됐다. 데이터 자체는 정상적으로 읽혔다.
  이 후보는 `dist/OMR-Grader-fixed21-20260906.REJECTED.md`로 불합격 표시하고 그대로 보존했다.
- a19524c는 경고의 가변 dict를 스레드 경계에 보내지 않고 별도의 불변 경고 값으로 복사한다.
  정상 기록과 경고를 함께 표시하고 실제 경고 코드를 로그에 남긴다. 기존 엄격한 값 전용
  검사와 live object 거부는 유지한다. 시작·새로고침·결과 이동·휴지통 회귀 검사를 포함했다.
- 후속 후보는 기존 산출물을 덮어쓰지 않고 다음 빈 빌드 번호를 사용하며, 일반 기술 smoke 외에
  정상 합성 기록과 의도적으로 불완전한 합성 기록이 함께 있는 실제 GUI 재실행을 검사한다.
