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
- S: 통합 후보에서 Sol high 독립 안전성 검토 예정.
- D: 구현 확정 후 Luna medium 문서/버전 정리 예정.
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
- [ ] A runtime 구현 검토/통합
- [x] B build/verifier 구현 검토/통합 (20f9445까지, 통합 a19dd49)
- [ ] C UI/의존성 구현 검토/통합
- [ ] Sol 독립 검토와 지적 사항 해소
- [ ] Luna 문서/버전 정리
- [ ] 확정 커밋에서 전체 검사·후보 빌드·strict smoke

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
