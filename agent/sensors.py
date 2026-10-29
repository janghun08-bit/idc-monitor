"""센서 클래스.

모든 센서는 BaseSensor를 상속하고 read()가 dict를 돌려준다.
SensorThread는 센서 종류를 몰라도 read() 결과를 합치기만 하면 된다.
USE_MOCK=0이면 실제 GPIO 센서(DHT22Sensor, LeakSensor, UltrasonicSensor)를 쓴다.

Mock 센서는 MockScenario를 보고 값을 만든다. 키보드로 시나리오를 바꾸면
(KeyboardThread) 다음 측정부터 과열·누수·접근 값이 나온다.
"""
import logging
import random
import sys
import threading
import time

log = logging.getLogger("agent")


class MockScenario:
    """시연용 상황 설정. KeyboardThread가 바꾸고 Mock 센서들이 읽는다.
    값 하나를 대입/읽기만 하므로(파이썬에서 원자적) 별도 Lock은 쓰지 않는다."""

    def __init__(self):
        self.temp_mode = "normal"   # normal / warn / danger
        self.leak = False
        self.access = False

    def describe(self) -> str:
        return f"온도={self.temp_mode}, 누수={'ON' if self.leak else 'OFF'}, 접근={'ON' if self.access else 'OFF'}"


class BaseSensor:
    name = "base"

    def read(self) -> dict:
        raise NotImplementedError


class MockDHT22Sensor(BaseSensor):
    """온습도 센서 흉내: 평소 24~26℃ / 경고 31~33℃ / 과열 36~38℃, 습도 40~50%."""
    name = "dht22(mock)"
    TEMP_RANGE = {"normal": (24.0, 26.0), "warn": (31.0, 33.0), "danger": (36.0, 38.0)}

    def __init__(self, scenario: MockScenario):
        self.scenario = scenario

    def read(self) -> dict:
        low, high = self.TEMP_RANGE[self.scenario.temp_mode]
        return {
            "temperature": round(random.uniform(low, high), 1),
            "humidity": round(random.uniform(40.0, 50.0), 1),
        }


class MockLeakSensor(BaseSensor):
    name = "leak(mock)"

    def __init__(self, scenario: MockScenario):
        self.scenario = scenario

    def read(self) -> dict:
        return {"leak": self.scenario.leak}


class MockUltrasonicSensor(BaseSensor):
    """초음파 거리 센서 흉내: 평소 100~200cm, 접근 시 10~25cm."""
    name = "ultrasonic(mock)"

    def __init__(self, scenario: MockScenario):
        self.scenario = scenario

    def read(self) -> dict:
        low, high = (10.0, 25.0) if self.scenario.access else (100.0, 200.0)
        return {"distance_cm": round(random.uniform(low, high), 1)}


class KeyboardThread(threading.Thread):
    """키보드로 상황 주입 (글자 입력 후 Enter).
    t 과열 / w 고온 경고 / l 누수 토글 / a 접근 토글 / b 버튼(경보 해제) / n 정상 복귀"""

    HELP = "⌨️ 키 입력 후 Enter: t 과열 | w 고온경고 | l 누수 | a 접근 | b 버튼 | n 정상"

    def __init__(self, scenario: MockScenario, on_button):
        super().__init__(name="KeyboardThread", daemon=True)
        self.scenario = scenario
        self.on_button = on_button

    def run(self):
        log.info(self.HELP)
        for line in sys.stdin:   # 입력이 끝나면(EOF) 루프가 끝나고 쓰레드 종료
            key = line.strip().lower()
            sc = self.scenario
            if key == "t":
                sc.temp_mode = "danger"
            elif key == "w":
                sc.temp_mode = "warn"
            elif key == "l":
                sc.leak = not sc.leak
            elif key == "a":
                sc.access = not sc.access
            elif key == "n":
                sc.temp_mode, sc.leak, sc.access = "normal", False, False
            elif key == "b":
                log.info("⌨️ [b] 버튼 눌림")
                self.on_button()
                continue
            else:
                if key:
                    log.info(self.HELP)
                continue
            log.info("⌨️ [%s] 시나리오 변경 → %s", key, sc.describe())


# ---- 실제 하드웨어 센서 (라즈베리파이, USE_MOCK=0) ----
# GPIO 라이브러리는 PC에 없으므로 클래스 안에서만 import 한다 (Mock 모드에서 import 에러가 나지 않게)

class DHT22Sensor(BaseSensor):
    """DHT22 온습도 센서 (DATA → GPIO4). 신호가 예민해서 읽기가 자주 실패하므로 재시도한다."""
    name = "dht22"
    RETRIES = 2
    RETRY_DELAY = 2.1   # DHT22는 2초에 한 번만 새로 측정할 수 있다
    STALE_SEC = 10      # 실패가 이어져도 이 시간 안의 마지막 정상값은 대신 쓴다

    def __init__(self, pin: int):
        import adafruit_dht
        import board
        self.device = adafruit_dht.DHT22(getattr(board, f"D{pin}"), use_pulseio=False)
        self.last = None
        self.last_at = 0.0

    def read(self) -> dict:
        error = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                temp, humi = self.device.temperature, self.device.humidity
                if temp is not None and humi is not None:
                    self.last = {"temperature": round(temp, 1), "humidity": round(humi, 1)}
                    self.last_at = time.monotonic()
                    return dict(self.last)
                error = "값 없음"
            except RuntimeError as e:   # 체크섬 오류, 응답 없음 등 (DHT에서는 흔함)
                error = e
            if attempt < self.RETRIES:
                time.sleep(self.RETRY_DELAY)
        if self.last and time.monotonic() - self.last_at < self.STALE_SEC:
            log.warning("DHT22 읽기 실패(%s) → 직전 정상값 사용", error)
            return dict(self.last)
        log.warning("DHT22 읽기 실패(%s) → 온습도 없음으로 보냄", error)
        return {"temperature": None, "humidity": None}


class LeakSensor(BaseSensor):
    """누수 센서 디지털 출력 D0 (→ GPIO17). 물 감지 시 LOW가 되는 모듈이 많아서 active_low로 뒤집을 수 있다."""
    name = "leak"

    def __init__(self, pin: int, active_low: bool):
        from gpiozero import DigitalInputDevice
        # 모듈에 풀업 저항이 있으므로 내부 풀업은 쓰지 않고(pull_up=None), '감지' 레벨만 지정한다
        self.device = DigitalInputDevice(pin, pull_up=None, active_state=not active_low)

    def read(self) -> dict:
        return {"leak": bool(self.device.is_active)}


class UltrasonicSensor(BaseSensor):
    """HC-SR04 초음파 거리 센서 (Trig → GPIO23, Echo → GPIO24, Echo는 반드시 분압)."""
    name = "ultrasonic"
    MAX_DISTANCE_M = 4.0

    def __init__(self, trigger: int, echo: int):
        from gpiozero import DistanceSensor
        # partial=True: 측정 평균을 낼 값이 다 모이기 전에도 바로 돌려준다.
        # 이게 없으면 배선이 빠져 echo가 안 올 때 read()가 한참 멈춰 SensorThread 전체가 서 버린다.
        self.device = DistanceSensor(echo=echo, trigger=trigger, max_distance=self.MAX_DISTANCE_M,
                                     queue_len=3, partial=True)

    MIN_CM = 2.0   # HC-SR04가 잴 수 있는 최소 거리. 이보다 작으면 echo를 못 받은 것

    def read(self) -> dict:
        # gpiozero는 m 단위, 너무 멀면 max_distance, echo가 안 오면 0을 돌려준다
        cm = round(self.device.distance * 100, 1)
        if cm < self.MIN_CM:
            # 0cm를 그대로 보내면 서버가 '사람 접근'으로 판정하므로, 값 없음으로 보낸다
            log.warning("초음파 응답 없음(%.1fcm) → 거리 없음으로 보냄 (Trig/Echo 배선·분압 확인)", cm)
            return {"distance_cm": None}
        return {"distance_cm": cm}


# BCM 핀 번호 (CLAUDE.md 하드웨어 핀 배치)
PIN_DHT22 = 4
PIN_LEAK = 17
PIN_TRIG = 23
PIN_ECHO = 24


def create_sensors(use_mock: bool, scenario: MockScenario = None) -> list:
    if use_mock:
        return [MockDHT22Sensor(scenario), MockLeakSensor(scenario), MockUltrasonicSensor(scenario)]
    from common import config
    sensors = [
        DHT22Sensor(PIN_DHT22),
        LeakSensor(PIN_LEAK, active_low=config.get_bool("LEAK_ACTIVE_LOW", True)),
        UltrasonicSensor(PIN_TRIG, PIN_ECHO),
    ]
    log.info("🔌 실제 센서 사용: DHT22(GPIO%d), 누수(GPIO%d), 초음파(Trig GPIO%d / Echo GPIO%d)",
             PIN_DHT22, PIN_LEAK, PIN_TRIG, PIN_ECHO)
    return sensors
