"""센서 클래스.

모든 센서는 BaseSensor를 상속하고 read()가 dict를 돌려준다.
SensorThread는 센서 종류를 몰라도 read() 결과를 합치기만 하면 된다.
실제 GPIO 센서(DHT22Sensor 등)는 6단계에서 추가한다.

Mock 센서는 MockScenario를 보고 값을 만든다. 키보드로 시나리오를 바꾸면
(KeyboardThread) 다음 측정부터 과열·누수·접근 값이 나온다.
"""
import logging
import random
import sys
import threading

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


def create_sensors(use_mock: bool, scenario: MockScenario = None) -> list:
    if use_mock:
        return [MockDHT22Sensor(scenario), MockLeakSensor(scenario), MockUltrasonicSensor(scenario)]
    raise NotImplementedError("실제 하드웨어 센서는 6단계에서 구현 (지금은 USE_MOCK=1)")
