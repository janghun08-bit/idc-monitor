# 서버실(IDC) 환경·보안 관제 시스템 — 설계서

인터넷 통신 설계 수업 설계 프로젝트. 라즈베리파이(현장 에이전트)가 센서 값을 TCP로 서버(AWS EC2)에 보내고,
서버가 판단해서 현장 장치(경광등, 부저)를 원격 제어한다. 데이터는 MySQL에 저장하고 Grafana로 시각화, 이상 시 Slack 알림.

## 채점 기준 (설계 판단 시 참고)
- 점수 항목: 쓰레드, 클래스 구조, 서버의 MySQL 운용, 사용자 UI(Grafana), 데이터 파싱, AES 암호화, 작품 완성도
- 감점(각 20점): 라즈베리파이 미사용, TCP 미사용, 센서 미사용 → 셋 다 반드시 실제로 쓰여야 함
- 11/6에 동작 영상 제출. 단계마다 TCP와 쓰레드 동작을 직접 설명할 수 있어야 함 (조교 질문 대비)

## 개발 원칙
- **센서/액추에이터는 반드시 Mock 구현을 함께 만든다.** `USE_MOCK=1`이면 PC에서 부품 없이 전체 시스템이 돌아가야 한다.
- 단계별로 구현하고, 각 단계가 끝나면 로컬에서 서버+에이전트를 실행해 동작을 확인한다.
- 비밀값(AES 키, DB 비밀번호, Slack Webhook)은 `.env`에만 두고 코드에 쓰지 않는다. `.env`는 git에 올리지 않는다.
- Python 3, 표준 `socket` + `threading` 사용 (asyncio 쓰지 않음 — 수업 요건이 TCP·쓰레드).
- 클래스 단위로 역할을 나눈다 (채점 항목).
- 코드 주석과 로그는 한국어로, 쓰레드·TCP 동작이 로그로 보이게 한다 (시연 영상용).

## 폴더 구조
```
idc-monitor/
├─ CLAUDE.md
├─ .env.example          # 키 목록만, 값 없음
├─ .gitignore            # .env, __pycache__, buffer 파일 등
├─ common/
│  ├─ protocol.py        # 프레이밍(send_msg/recv_msg), 메시지 생성 함수
│  └─ crypto.py          # AES-GCM 암복호화 (5단계 전까지는 평문 통과)
├─ agent/                # 라즈베리파이에서 실행
│  ├─ main.py
│  ├─ client.py          # AgentClient: 연결/재연결, 송신·수신 쓰레드
│  ├─ sensors.py         # BaseSensor, Mock*, DHT22Sensor, LeakSensor, UltrasonicSensor
│  ├─ actuators.py       # BaseActuator, MockActuator, GpioActuator (RGB LED, 부저, 버튼)
│  └─ requirements.txt
├─ server/               # EC2에서 실행
│  ├─ main.py
│  ├─ tcp_server.py      # MonitorServer(accept), ClientHandler(접속별 쓰레드)
│  ├─ alarm.py           # AlarmEngine: 상태 판정, 상태 변화 감지
│  ├─ db.py              # Database: MySQL 저장
│  ├─ notifier.py        # SlackNotifier
│  ├─ watchdog.py        # 하트비트 감시 쓰레드
│  └─ requirements.txt
├─ docker/
│  ├─ docker-compose.yml # mysql, grafana, server(profile "server", EC2용)
│  ├─ server.Dockerfile
│  ├─ mysql/init.sql
│  └─ grafana/provisioning/  # datasource 자동 등록
├─ docs/devlog.md        # 개발 일지 (단계별 한 일, 문제와 해결 — 쉬운 말로)
└─ README.md             # 프로젝트 소개 (교수님·포트폴리오용, 비유로 쉽게)
```

## TCP 프로토콜
- 연결: 에이전트 = 클라이언트, 서버 = `0.0.0.0:9000` 대기. **하나의 연결로 양방향** 송수신.
- 프레임: `[4바이트 길이(big-endian)] + [payload]`
  - payload = UTF-8 JSON (5단계부터 AES-GCM: `nonce(12B) + ciphertext+tag`)
  - `recv_msg`는 정확히 N바이트를 다 받을 때까지 반복 수신 (recv_exact).
- 공통 필드: `type`, `agent_id`, `ts`(epoch 초), 필요 시 `seq`

| type | 방향 | 내용 |
|---|---|---|
| HELLO | 에이전트→서버 | 접속 직후 1회. `agent_id` 등록 |
| WELCOME | 서버→에이전트 | HELLO 응답. `last_seq` = DB에 저장된 이 에이전트의 마지막 seq (모르면 null). 에이전트는 이보다 작으면 `last_seq+1`부터 이어서 번호를 붙인다 (재시작 시 seq가 1로 돌아가 옛 기록과 중복 처리되는 문제 방지). 에이전트는 WELCOME을 받은 뒤에 송수신 쓰레드를 시작한다 |
| DATA | 에이전트→서버 | `seq`, `data: {temperature, humidity, leak, distance_cm}` |
| ACK | 서버→에이전트 | `seq` — 해당 DATA 저장 완료 |
| HEARTBEAT | 에이전트→서버 | 5초마다 |
| EVENT | 에이전트→서버 | `event: "ALARM_CLEAR"` (버튼) |
| CMD | 서버→에이전트 | `action: "SET_LED", color: green/yellow/red/blue` / `action: "BUZZER", on: true/false` |

## 판정 규칙 (AlarmEngine)
| 항목 | 조건 | 상태 | 현장 대응 |
|---|---|---|---|
| 누수 | `leak == true` | DANGER | 빨간 LED + 부저 ON |
| 온도 | ≥ 35℃ | DANGER | 빨간 LED + 부저 ON |
| 온도 | ≥ 30℃ | WARN | 노란 LED |
| 습도 | ≥ 70% | WARN | 노란 LED |
| 접근 | `distance_cm < 30` | ACCESS | 파란 LED + 출입 기록 |
| 그 외 | — | NORMAL | 초록 LED, 부저 OFF |

- LED 우선순위: DANGER(빨강) > WARN(노랑) > ACCESS(파랑) > NORMAL(초록)
- **상태가 바뀔 때만** CMD 전송, events 테이블 기록, Slack 알림 (매 수신마다 보내지 않음).
- 접근(ACCESS)은 감지 시작 시 1회만 기록, 30cm 밖으로 나가면 해제.
- 버튼(ALARM_CLEAR): 부저만 즉시 OFF. 원인이 남아 있으면 LED는 유지, 정상으로 돌아오면 자동 해제.
  - 에이전트가 부저를 현장에서 바로 끄고 EVENT를 보낸다 (연결이 끊겨 있어도 부저는 꺼짐). 서버는 DANGER를 벗어날 때까지 부저를 다시 켜지 않는다.
- 경보 단계(NORMAL/WARN/DANGER) 변화 → `STATE_CHANGE`, 출입 시작 → `ACCESS`로 따로 기록한다. 접속 직후 첫 판정이 NORMAL이면 기록하지 않는다.
- 에이전트가 새로 접속(HELLO)하면 서버는 그 에이전트 상태를 초기화하고, 첫 DATA에서 LED·부저 CMD를 다시 보낸다.
- 임계값은 `.env`로 바꿀 수 있게 (시연 때 30℃ 대신 낮은 값으로 조정 가능).

## 신뢰성
- **ACK + 재전송(store-and-forward):** 에이전트는 보낸 DATA를 `pending`(seq → 메시지)에 보관, ACK 받으면 삭제.
  연결이 끊긴 동안 측정값은 버퍼(deque, 최대 1000개)에 쌓고, 재연결 후 순서대로 재전송.
  서버는 같은 seq를 중복 저장하지 않는다 (agent_id + seq 유니크).
  - 구현: `DataBuffer`(대기함, 아직 안 보낸 측정값) → 전송 직전 seq 부여 + `PendingStore`(확인 대기함)에 **먼저 넣고** 전송 → ACK 시 삭제.
    seq는 측정 시가 아니라 전송 시 붙인다 (재시작 직후 WELCOME 전에 측정한 값도 이어지는 번호를 받도록).
  - 재연결하면 pending 전부를 seq 순서대로 먼저 재전송, 그다음 대기함. WELCOME의 last_seq로 pending을 지우지는 않는다 (중간 번호 저장 실패 시 잃을 수 있어서, 서버 중복 처리에 맡김).
  - 연결 중에도 ACK가 10초(`ACK_TIMEOUT`) 없으면 재전송 (DB 고장 대비). pending도 최대 1000개.
  - 대기함은 메모리에만 있어서 에이전트를 껐다 켜면 사라진다 (설계 범위 밖).
- **재연결:** 연결 실패 시 1→2→4→…최대 10초 백오프로 재시도. 연결에 성공하면 1초로 되돌린다.
- **AES-256-GCM:** `common/crypto.py` 한 곳에서 처리 (send_msg/recv_msg가 자동으로 거침). 메시지마다 새 nonce.
  - 복호화 실패(`CryptoError`, 키 불일치·변조·평문) → 그 연결을 끊는다. 에이전트는 백오프로 재시도.
  - `AES_KEY`가 비어 있으면 평문 + 시작 시 경고. 형식이 틀리면 시작하지 않는다. 에이전트도 `cryptography` 필요.
  - 시연용으로 HELLO의 평문과 실제 전송 바이트(길이·nonce·암호문)를 로그에 한 번 보여준다.
  - 같은 암호문을 그대로 다시 보내는 재생 공격은 막지 않는다 (DATA는 seq 중복 처리로 무시됨).
- **하트비트 감시:** 서버의 Watchdog 쓰레드가 1초마다 확인, 15초간 아무 메시지도 없으면 `AGENT_OFFLINE` 이벤트 + Slack.
  다시 메시지가 오면 `AGENT_ONLINE`. (OFFLINE 레벨은 WARN, ONLINE은 OFFLINE 이후에만 기록. 시간은 `OFFLINE_SEC`로 조정)
  - 서버는 연결이 끊겨도 에이전트의 마지막 수신 시각(`liveness`)을 남겨 둬서 Watchdog이 감시를 이어 간다.
- 이벤트는 `MonitorServer.record_event()` 한 곳에서 로그 → DB → Slack 순으로 처리한다. 판정 결과는 **CMD를 먼저 보내고** 이벤트를 기록한다 (DB·Slack 지연이 현장 대응을 늦추지 않게).
- 서버 포트: Windows에서는 `SO_EXCLUSIVEADDRUSE`(같은 포트에 서버 두 개가 뜨는 것 방지), Linux에서는 `SO_REUSEADDR`.
- 소켓에는 `settimeout`을 걸고, 모든 네트워크 코드는 예외 처리.

## 쓰레드 구성
- 에이전트: `SensorThread`(2초마다 측정 → 대기함), `SenderThread`(pending 재전송 → 대기함 전송, 하트비트, ACK 10초 초과분 재전송), `ReceiverThread`(CMD·ACK 수신 → 액추에이터 제어), 메인 쓰레드(연결 관리, 버튼 콜백)
- 서버: 메인(accept 루프), `ClientHandler`(접속별 쓰레드), `Watchdog`, `SlackNotifier`(큐에서 꺼내 HTTP 전송 — Slack이 느려도 경보 처리가 멈추지 않게)
- 공유 상태(pending, 클라이언트 목록)는 `threading.Lock`으로 보호.

## DB 스키마 (MySQL)
```sql
CREATE TABLE sensor_readings (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  agent_id VARCHAR(32) NOT NULL,
  seq INT NOT NULL,
  ts DATETIME NOT NULL,
  temperature FLOAT, humidity FLOAT,
  leak BOOLEAN, distance_cm FLOAT,
  UNIQUE KEY uq_agent_seq (agent_id, seq)
);
CREATE TABLE events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  agent_id VARCHAR(32) NOT NULL,
  ts DATETIME NOT NULL,
  event_type VARCHAR(32) NOT NULL,   -- STATE_CHANGE, ACCESS, ALARM_CLEAR, AGENT_OFFLINE, AGENT_ONLINE
  level VARCHAR(16),                 -- NORMAL, WARN, DANGER, ACCESS
  message VARCHAR(255)
);
```
Grafana: MySQL datasource 자동 등록, 패널 = 온도/습도 시계열, 누수·접근 상태, 최근 events 표.
- `ts`는 UTC로 저장 (Grafana가 브라우저 시간대로 표시). DATA의 `ts`는 보낸 시각이 아니라 **측정 시각**.
- 서버 DATA 처리 순서: DB 저장 → ACK → 판정. **저장에 실패하면 ACK를 보내지 않는다.** 중복 seq는 ACK만 보내고 판정하지 않는다.
- DB가 죽어도 서버와 경보는 계속 동작한다. 연결 실패 후 10초간은 접속을 시도하지 않고 바로 실패 처리 (경보 지연 방지).
- `DB_USER`가 비어 있으면 `NullDatabase`(저장 생략)로 실행 → Docker 없이도 1~2단계처럼 돌릴 수 있다.
- docker-compose는 `name: idc-monitor`로 고정 (이 PC에 다른 docker 프로젝트가 있으므로 이름·볼륨이 겹치지 않게). 호스트 포트는 `DB_PORT`, `GRAFANA_PORT`로 변경 가능.

## 하드웨어 핀 배치 (BCM 번호)
| 부품 | 핀 | 비고 |
|---|---|---|
| DHT22 DATA | GPIO4 | 3핀 모듈, VCC는 3.3V |
| 누수 센서 D0 | GPIO17 | VCC 3.3V, 물 감지 시 LOW인 모듈이 많음 → 설정으로 반전 가능하게 |
| HC-SR04 Trig | GPIO23 | VCC 5V |
| HC-SR04 Echo | GPIO24 | **1kΩ/2kΩ 분압 필수** (5V → 3.3V) |
| RGB LED R / G / B | GPIO16 / 20 / 21 | 모듈에 저항 내장 |
| 부저 | GPIO18 | 액티브 부저, 동작 레벨(High/Low)은 설정값으로 |
| 버튼 | GPIO27 | 내부 풀업, GND와 연결. 실물은 HW-504 조이스틱의 SW 핀(누르면 GND)만 사용, VRx/VRy 미사용 |

- 실물 부품(2026-10-29 구매, 10/31 도착): 빗물 센서 = 감지판 + MH-RD(LM393) 기판(DO 사용, AO 미사용), DHT22(AM2302) 3핀 모듈. 나머지(초음파, RGB, 부저, HW-504, 브레드보드)는 보유.
- 하드웨어 연결·실기 시험은 2026-11-04(수) 사용자 시험 끝난 뒤 진행 예정 (영상 제출 11/6).

라이브러리: `gpiozero`(LED·부저·버튼·DistanceSensor·DigitalInputDevice), `adafruit-circuitpython-dht`(DHT22, 읽기 실패 시 재시도).
GPIO 관련 import는 실제 구현 클래스 안에서만 해서, PC(Mock 모드)에서 import 에러가 나지 않게 한다.
- DHT22: 실패 시 2.1초 뒤 1회 재시도, 그래도 실패면 10초 이내 직전값, 아니면 None (서버는 None 항목을 판정에서 건너뜀).
- 초음파: `partial=True`(echo가 없을 때 read가 멈추지 않게). 2cm 미만(=echo 없음, gpiozero가 0을 줌)은 None으로 보낸다 — 0을 보내면 '접근'으로 오판.
- 부저는 `beep()`(gpiozero 백그라운드 쓰레드)로 0.5초 간격 삐–삐. 버튼 콜백은 gpiozero 이벤트 쓰레드에서 실행됨. 종료 시 LED·부저 끄고 핀 해제.
- PC 검증은 `GPIOZERO_PIN_FACTORY=mock`(gpiozero MockFactory)과 가짜 DHT 모듈로 했다. 실제 Pi에서 동작은 아직 미확인.

## .env 키 목록
```
SERVER_HOST=127.0.0.1
SERVER_PORT=9000
AGENT_ID=pi-01
USE_MOCK=1
AES_KEY=            # 32바이트 hex(64자), 에이전트·서버 동일
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=
DB_PASSWORD=
DB_NAME=idc_monitor
MYSQL_ROOT_PASSWORD=   # docker MySQL 컨테이너용
GRAFANA_ADMIN_PASSWORD=
GRAFANA_PORT=3000
SLACK_WEBHOOK_URL=
TEMP_WARN=30
TEMP_DANGER=35
HUMI_WARN=70
ACCESS_CM=30
OFFLINE_SEC=15
LEAK_ACTIVE_LOW=1      # 누수 모듈이 물 감지 시 LOW면 1
LED_COMMON_ANODE=0     # 공통 애노드 RGB LED면 1 (핀 LOW일 때 켜짐)
BUZZER_ACTIVE_HIGH=1   # 부저가 HIGH에서 울리면 1
```

## Mock 센서 동작 (시연 시나리오 재현용)
- 기본: 온도 24~26℃, 습도 40~50%, 누수 없음, 거리 100~200cm 사이 랜덤
- 키보드 입력으로 상황 주입 (글자 + Enter, `KeyboardThread`): `t` 과열(36~38℃), `w` 고온 경고(31~33℃), `l` 누수 토글, `a` 접근 토글, `b` 버튼(경보 해제), `n` 정상 복귀
- Mock 액추에이터는 LED 색과 부저 상태를 터미널에 출력

## 구현 단계 (순서대로, 단계마다 실행 확인)
1. **프로토콜 + 기본 통신:** `common/protocol.py`, 서버 accept/ClientHandler, 에이전트 연결·DATA 송신 (Mock 센서). 서버 로그에 수신값 출력.
2. **양방향 + 판정:** AlarmEngine, CMD 전송, 에이전트 ReceiverThread + Mock 액추에이터, 버튼(EVENT).
3. **DB + Grafana:** docker-compose(MySQL, Grafana), init.sql, Database 클래스로 저장, ACK 응답.
4. **알림 + 감시:** SlackNotifier(타임아웃 3초, 실패해도 서버 계속 동작), 하트비트, Watchdog.
5. **신뢰성 + 보안:** pending/버퍼 재전송, 재연결 백오프, AES-GCM 적용.
6. **실제 하드웨어 클래스:** DHT22/누수/초음파/RGB/부저/버튼 GPIO 구현 (`USE_MOCK=0`).
7. **배포:** server도 docker-compose에 추가, README(아키텍처 그림, 실행법, 시연 시나리오).

## 작업 방식 (Claude Code 진행 규칙)
- 내가 "다음 단계"라고 하면 구현 단계 중 다음 하나만 진행한다. 여러 단계를 한꺼번에 하지 않는다.
- 각 단계가 끝나면 반드시 알려줄 것:
  1. 실행 명령어 (서버, 에이전트 각각)
  2. 정상 동작 시 보여야 하는 로그
  3. 테스트 시나리오
  4. 이 단계에서 TCP와 쓰레드가 어떻게 동작하는지 짧은 설명
- 5단계(재전송 버퍼, AES)는 구현 전에 계획을 먼저 설명하고 내 확인을 받는다.
- 내가 "확인했어"라고 하면 커밋하고, 아래 "진행 기록"에 한 줄 추가한다.
  - 같이 `docs/devlog.md`에 그 단계 일지(목표, 한 일, 쉽게 이해하기, 부딪힌 문제→원인→해결, 결과)를 쓰고, README의 진행 상황 체크를 갱신한 뒤 GitHub에 push한다.
- 에러가 나면 원인을 먼저 설명하고 수정한다.
- 설계를 바꾸면 이 CLAUDE.md도 같이 수정한다.
- 비밀값(.env 내용, 키, Webhook URL)은 출력하거나 커밋하지 않는다.

## 진행 기록
- 2026-10-27 1단계 완료: 길이 프레이밍 프로토콜, 서버 accept + ClientHandler 쓰레드, 에이전트 SensorThread→Queue→SenderThread, Mock 센서, 3초 고정 재접속 (실행은 `py -m server.main` / `py -m agent.main`)
- 2026-10-27 2단계 완료: AlarmEngine(상태 변화 시에만 CMD), 에이전트 ReceiverThread + MockActuator, KeyboardThread 상황 주입, 버튼 ALARM_CLEAR EVENT, send_lock
- 2026-10-27 3단계 완료: docker-compose(MySQL 8.4 + Grafana 11.4, 프로젝트명 idc-monitor), init.sql, 대시보드 자동 등록, Database(Lock + 실패 시 10초 재시도 보류), 저장 후 ACK, 측정 시각 ts. 실제 MySQL/Grafana에서 과열 표시 확인
- 2026-10-29 4단계 완료: SlackNotifier 쓰레드(큐 + 3초 타임아웃), 하트비트 5초, Watchdog(15초 OFFLINE/ONLINE), record_event 일원화, CMD 우선 전송, Windows 포트 독점. 재시작 시 seq 중복 버그 → HELLO/WELCOME으로 마지막 seq 동기화. 실제 Slack 알림 확인
- 2026-10-29 5단계 완료: DataBuffer(대기함)+PendingStore(확인 대기함, 전송 전 보관)로 재전송, ACK 10초 초과 재전송, 1→10초 지수 백오프, AES-256-GCM(crypto.py), 키 불일치 시 연결 끊음. 서버 재시작·DB 고장 시험에서 seq 누락 없음
- 2026-10-29 작업 방식 변경: 사용자 요청으로 6·7단계는 단계별 설명·확인 없이 연속 진행 (일지는 계속 작성)
- 2026-10-29 6단계 완료: DHT22Sensor(재시도+직전값), LeakSensor(극성 설정), UltrasonicSensor(echo 없음→None), GpioActuator(RGB·부저 beep), GpioButton. gpiozero MockFactory로 PC 검증 (실기 미확인)
- 2026-10-29 7단계 완료: server.Dockerfile, compose에 server 서비스(profile "server", DB_HOST=mysql), .dockerignore, README에 배선·Pi 설치·EC2 배포·시연 영상 순서. 이미지 빌드·EC2·실기는 미확인
- 2026-10-29 EC2 배포: 3.34.253.134(t3.small, 서울)에서 3개 컨테이너 실행, PC 에이전트 연결 확인. 학교망은 9000 아웃바운드 차단 → 핫스팟 사용
