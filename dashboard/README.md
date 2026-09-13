# CO-SHOW 운영 대시보드

현재 **M3 참관자 레이아웃**까지 구현했다. 3D 장면과 카메라 디코딩은 M4,
실제 실행 제어와 비상착륙은 M5, 로스터 편집·스택 기동은 M6에서 추가한다.
실제 ROS 모드는 관찰 전용이며 수신한 제어 명령을 거부하고 이벤트에 남긴다.

## 실행

Python 3.10 이상에서 실행하되 배포·검증 기준은 Ubuntu 22.04 / ROS 2
Humble / Python 3.10이다. Python 의존성은 `aiohttp`, `PyYAML`뿐이다.
ROS 모드는 기존 ROS 환경을 source한 터미널에서 실행한다.

```bash
python3 dashboard/server.py --mock
python3 dashboard/tests/probe_m2.py --url http://127.0.0.1:8080 --start-mock
```

서버 실행 후 브라우저에서 `http://127.0.0.1:8080/visitor.html`을 연다.
두 번째 명령은 별도 터미널에서 mock 준비·시작을 요청한다. 약 29초에 수색
단계가 된다. 조작 UI는 M5에서 추가한다. 화면의 빈 캔버스와 **샘플 영상**은
M3 자리 표시자이며 실제 카메라 영상이 아니다. 글꼴과 three.js는 라이선스와
함께 [static/VENDOR.md](static/VENDOR.md)에 버전을 고정해 동봉했다.
`/visitor.html?fx=low`는 유리 blur를 끄고, OS의 모션 감소 설정도 따른다.

`--mock-fail`을 함께 주면 stage 3 위치 오차와 ping 실패로 시작이 막힌다.
mock은 파일을 바꾸지 않고 메모리 안에서 로스터·라디오·미설정 IP·BT 안전
플래그를 정상 fixture 값으로 채운다. 실제 기체 설정 검증은 ROS 모드에서 한다.
ROS·카메라 장치·UDP 수신기 없이 동봉된 JPEG를 사용한다.

```bash
python3 dashboard/server.py --check-config
python3 dashboard/server.py
```

기본 바인딩은 `127.0.0.1:8080`. `/`는 현재 모드 JSON, `/ws?role=visitor`와
`/ws?role=admin`은 프로토콜 엔드포인트다. 정적 파일은 `/static/`에서 제공한다.
관리자 소켓은 로컬 주소만 허용한다. `--port`, `--host`, `--config`, `--field`
옵션을 지원한다. 참관자 화면은 `hello` 역할 순서와 `state`를 사용하고
서버의 `run.elapsed_s`를 표시한다. 연결 중단은 마지막 상태 수신 후 2초에
표시하며 재접속한다.

## 설정 변경

외부 이름·IP는 `config/dashboard.yaml`, 메시지·서비스·액션 타입은 같은
파일의 `types.topics/services/actions`에 있다. 타입 키는 기존 이름/템플릿
키와 일치한다. LIMO 상태의 실제 타입은 미확인이라 `null`이다. 해당 데이터는
정보 없음으로 남고 다른 채널은 계속 동작한다. LIMO 전압 임계값이 확정되면
`limo_battery_v: {block: ..., warn: ...}`를 추가할 수 있다(non-blocking).

이름·타입 변경 뒤 `--check-config`를 실행한다. 진단은 자기 구독이나
클라이언트를 정상 서버로 세지 않고 실제 publisher/service/action server를
검사한다. 종료 코드는 치명 설정 오류나 확인 가능한 인터페이스/노드의 FAIL이 있으면 1, 그 외에는 0이다.
명시적 null 타입은 SKIP, 스페어 URI/IP 미기입은 WARN이며 종료 코드에서 제외한다.

파일 경로는 `dashboard/` 기준으로 해석하고 `~`를 확장한다. BT `--config`
경로만 `commands.bt_cwd` 기준이다. 관측점·베이스·탐색 레인·구조 대기 시간은
기존 BT YAML에서 읽는다. 필드 영역·표시 마커 위치는 `config/field.yaml`이다.

| BT YAML 키 | 사용처 |
|---|---|
| `coshow.drones.*.base`, `coshow.limos.*.base` | hello 베이스, 위치 설정 검사 |
| `coshow.searchers`, `search.zones/lane_spacing/lane_axis` | 탐색 레인 |
| `coshow.drones.*.altitudes`, `coshow.altitudes` | 역할별 고도 우선 적용 |
| `coshow.observe_drone/observe_point/rescue_sec` | 관측점·공유 mock·구조 대기 |
| `coshow.tolerances`, `coshow.durations` | 허용오차·착륙 시간 검사, M5 실행 제어 |
| `coshow.preflight.required`, `coshow.emergency_land_on_exit` | 안전 플래그 검사 |
| `bt_runner.bt_visualiser.enabled` | 운영 창 설정 경고(비차단) |

키가 없으면 해당 항목만 설정 없음으로 표시한다.

플릿의 CF01–CF10 / LIMO1–LIMO4는 인벤토리 슬롯이다. 실제 확인되지 않은
추가 URI/IP는 `null`이며 입력이 필요하다. 저장된 `run/roster.yaml`이 있으면
그 역할 배정을 읽고, 없으면 기존 crazyflies 템플릿의 enabled URI로 초기
관측 배정을 추론한다. M2에서는 이 초기 배정을 저장하거나 스택을 재기동하지 않는다.
현재 템플릿은 역할 하나만 enabled라 기본 실모드에는 플릿 14행 외에 미배정
역할 슬롯 3행이 보이고 시작 조건은 실패한다. 물리 기체가 17대라는 뜻이 아니다.
정상 4역할 로스터와 mock에서는 플릿 14행이다. 로스터 편집·생성 파일은 M6 범위다.

## 직접 검증

Docker 하네스는 제공된 `ros:humble` 기반 이미지와 인터페이스 빌드 볼륨을 쓴다.

```bash
bash dashboard/docker/run.sh python3 -m pytest dashboard/tests -q
docker run --rm -e ROS_DOMAIN_ID=82 -e ROS_LOCALHOST_ONLY=1 \
  -v "$PWD":/ws -v coshow_build:/ws_build coshow-humble-dev \
  python3 dashboard/tests/verify_m2_ros.py
```

통합 스크립트는 `dashboard/run/m2_fixture/`에 명시적인 가짜 IP·URI·로스터와
BT 플래그 사본을 만들고, 공유 `mock.scenario()`의 토픽을 발행한다.
정상/오타 `--check-config`, QoS, 두 소켓, 스페어 링크 실패를 검사한다.
fixture 서비스는 어떤 명령도 실행하지 않으며 제어 요청 0건을 확인한다.
`run/`은 gitignore 대상이고 실제 협업자 YAML은 수정하지 않는다.

설치·키오스크·현장 runbook은 M7에서, Webots·실기체 리허설 지원은 M8에서
다룬다. 각 단계의 증거와 제한은 [M1 보고서](REPORTS/M1.md),
[M2 보고서](REPORTS/M2.md)에 기록한다.

M3 화면 증거·레이아웃 해석·라벨 대비는 [M3 보고서](REPORTS/M3.md)에 있다.
개발용 화면 검증은 `node dashboard/tests/verify_m3_ui.mjs`로 실행한다.
별도 `--mock --port 8088` 서버와 개발 환경의 `playwright`, `sharp`가 필요하다.
`DASHBOARD_QA_URL`로 서버 주소, `DASHBOARD_QA_NODE_MODULES`로 개발 모듈
디렉터리, `CHROMIUM_EXECUTABLE`로 headless Chromium 경로를 지정할 수 있다.
배포 Python 의존성이나 프론트 런타임에 Node 패키지를 추가하지 않는다.
스크린샷은 실제 mock 타임라인에서, 대수 변경·재접속은 별도 브라우저
프로토콜 fixture에서 확인한다.
