"""액추에이터(현장 장치) 클래스: RGB LED, 부저.

ReceiverThread는 서버 CMD를 받으면 set_led() / set_buzzer()만 부른다.
실제 GPIO 구현(GpioActuator, 버튼 포함)은 6단계에서 추가한다.
"""
import logging

log = logging.getLogger("agent")


class BaseActuator:
    def set_led(self, color: str):
        raise NotImplementedError

    def set_buzzer(self, on: bool):
        raise NotImplementedError


class MockActuator(BaseActuator):
    """부품 대신 터미널에 LED 색과 부저 상태를 출력한다."""
    ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴", "blue": "🔵"}

    def __init__(self):
        self.color = None
        self.buzzer = False

    def set_led(self, color: str):
        self.color = color
        log.info("💡 [LED] %s %s", self.ICON.get(color, "?"), color.upper())

    def set_buzzer(self, on: bool):
        if on == self.buzzer:
            return
        self.buzzer = on
        log.info("🔊 [부저] 삐–삐–삐 ON" if on else "🔇 [부저] OFF")


def create_actuator(use_mock: bool) -> BaseActuator:
    if use_mock:
        return MockActuator()
    raise NotImplementedError("실제 하드웨어 액추에이터는 6단계에서 구현 (지금은 USE_MOCK=1)")
