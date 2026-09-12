# 운영 대시보드 구현 기록

설계 기준: `astra_handoff_2026-09-12.zip`의 `02_spec.md` 및
`03_implementation_guide.md`, `05_verification_and_reporting.md`.
별도로 전달된 `2026-09-12_dashboard_design_spec.md`는 패키지 스펙과
바이트 단위로 동일함을 확인했다. 구현 지시는 대화의 사용자 요청을 우선한다.

추가 시각 참고: 사용자가 제공한 Apple 디자인 분석을
[`Design.md`](Design.md)에 정리했다. M3–M5에서 기존 계약 안의
여백·위계·표면·마감에 적용하며, 앞서 열린 계약 질문의 답변으로
간주하지 않는다.

작업 브랜치: `HOSEONGI/COSHOW_Sim:dashboard`, 시작 커밋
`2a464d4007edfed09760f42c0391de6f38cb5f9a`.

## 마일스톤

- [x] M1: ROS 관측 발행기, AI Deck 초기화 수정, 레인 포팅 및 Docker 증거
- [ ] M2: 상태 집계, WebSocket, mock, 체크리스트 및 ROS 프로토콜 증거
- [ ] M3: 동봉 폰트·three.js, 디자인 토큰, 참관자 화면 FHD·4K 검증
- [ ] M4: 트윈·카메라·내러티브와 상태별 화면 검증
- [ ] M5: 운영 화면·프로세스 관리·비상착륙과 Docker 안전 순서 검증
- [ ] M6: 설치·실행·키오스크 스크립트, runbook, Docker 통합 검증

각 완료 조건은 독립 검토, 직접 실행한 증거, `REPORTS/M<n>.md`,
`dashboard: M<n> ...` 커밋 및 해당 브랜치로의 푸시다.

## 계약 해석과 열린 질문

M1에서 적용하는 해석:

- §4.1 `search_progress`는 상세 주의와 원본 Search에 따라 드론별 정수
  인덱스다. 중첩 객체 예시보다 명시적 상세 규칙을 따른다.
- `_phase`는 누락·preflight 게이트를 먼저 평가하고, 구조 완료·목표 발견·
  미션 발견의 진행 사실을 따른다. 따라서 복귀 중인 기체가 홈 반경을
  벗어났다고 후기 단계를 `handover`로 되돌리지 않는다. 기존 판정 헬퍼와
  구조 도착 반경을 그대로 읽으며 blackboard를 수정하지 않는다.

사용자 확인 요청 중(M1과 독립적):

1. §5.5 외부 preflight/BT 검사는 대시보드가 소유한 프로세스를 허용하고
   추가 외부 프로세스만 차단하는 것으로 해석할지.
2. §5.5 preflight 사망 시 살아 있는 BT에는 전체 ①~⑥ 착륙 순서를
   적용할지. BT 사망용 축약 경로는 살아 있는 BT를 멈추지 못한다.
3. §5.6 mock에서 사건 시각의 대략적 범위를 유지하면서 phase·색은
   §4.1 및 원본 BT 순서를 우선할지.
4. §6.4의 IDLE·mission age 우선 규칙 때문에 외부 BT 관찰 시
   내러티브가 준비 문구에 가려지고, 종료한 BT의 DONE 문구가 3초 후
   사라지는 충돌. 화면 규칙을 변경하기 전 별도 결정이 필요하다.

그 외 상세 규칙의 우선 적용:

- 참관자 원시 이름은 §5.4·display 설정의 명시적 금지에 따라 숨긴다.
- `bt_visualiser.enabled=false` 정적 검사도 §4.5·§7에 따라 포함한다.
- 배터리 주의 구간은 `block <= V < warn`이다.
- 카메라 타일 신선도는 설정값을 쓰며 체크리스트의 blocking 기준은
  §5.5 표에 명시된 `stream_ok && fps >= camera_min_fps`를 유지한다.

## 검증 범위

로컬 mock과 제공된 ROS Humble Docker 하네스만 직접 실행한다.
실제 Webots·기체·AI Deck UDP에는 연결하지 않는다. 기존 시나리오
설정·XML·서버·fake_limo·월드는 수정하지 않는다.
