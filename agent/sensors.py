"""센서 클래스.

모든 센서는 BaseSensor를 상속하고 read()가 dict를 돌려준다.
SensorThread는 센서 종류를 몰라도 read() 결과를 합치기만 하면 된다.
실제 GPIO 센서(DHT22Sensor 등)는 6단계에서 추가한다.
"""
import random


class BaseSensor:
    name = "base"

    def read(self) -> dict:
        raise NotImplementedError


class MockDHT22Sensor(BaseSensor):
    """온습도 센서 흉내: 온도 24~26℃, 습도 40~50%."""
    name = "dht22(mock)"

    def read(self) -> dict:
        return {
            "temperature": round(random.uniform(24.0, 26.0), 1),
            "humidity": round(random.uniform(40.0, 50.0), 1),
        }


class MockLeakSensor(BaseSensor):
    """누수 센서 흉내: 기본은 누수 없음."""
    name = "leak(mock)"

    def read(self) -> dict:
        return {"leak": False}


class MockUltrasonicSensor(BaseSensor):
    """초음파 거리 센서 흉내: 100~200cm (아무도 없음)."""
    name = "ultrasonic(mock)"

    def read(self) -> dict:
        return {"distance_cm": round(random.uniform(100.0, 200.0), 1)}


def create_sensors(use_mock: bool) -> list:
    if use_mock:
        return [MockDHT22Sensor(), MockLeakSensor(), MockUltrasonicSensor()]
    raise NotImplementedError("실제 하드웨어 센서는 6단계에서 구현 (지금은 USE_MOCK=1)")
