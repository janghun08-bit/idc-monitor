"""액추에이터(현장 장치) 클래스: RGB LED, 부저, 경보 해제 버튼.

ReceiverThread는 서버 CMD를 받으면 set_led() / set_buzzer()만 부른다.
USE_MOCK=1이면 MockActuator(터미널 출력), 0이면 GpioActuator(실제 핀).
"""
import logging

log = logging.getLogger("agent")

# BCM 핀 번호 (CLAUDE.md 하드웨어 핀 배치)
PIN_LED_R, PIN_LED_G, PIN_LED_B = 16, 20, 21
PIN_BUZZER = 18
PIN_BUTTON = 27

ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴", "blue": "🔵"}


class BaseActuator:
    def set_led(self, color: str):
        raise NotImplementedError

    def set_buzzer(self, on: bool):
        raise NotImplementedError

    def close(self):
        pass


class MockActuator(BaseActuator):
    """부품 대신 터미널에 LED 색과 부저 상태를 출력한다."""

    def __init__(self):
        self.color = None
        self.buzzer = False

    def set_led(self, color: str):
        self.color = color
        log.info("💡 [LED] %s %s", ICON.get(color, "?"), color.upper())

    def set_buzzer(self, on: bool):
        if on == self.buzzer:
            return
        self.buzzer = on
        log.info("🔊 [부저] 삐–삐–삐 ON" if on else "🔇 [부저] OFF")


class GpioActuator(BaseActuator):
    """실제 RGB LED(GPIO16/20/21)와 액티브 부저(GPIO18).
    ReceiverThread(CMD)와 버튼 콜백(gpiozero 쓰레드)이 함께 부르지만, gpiozero 출력 장치는 값 대입만 하므로 안전하다."""

    # (R, G, B) — 노랑은 빨강+초록을 같이 켠다
    COLORS = {"green": (0, 1, 0), "yellow": (1, 1, 0), "red": (1, 0, 0), "blue": (0, 0, 1)}
    BEEP_ON, BEEP_OFF = 0.5, 0.5   # 부저는 계속 울리지 않고 삐–삐– 반복

    def __init__(self, led_common_anode: bool, buzzer_active_high: bool):
        from gpiozero import RGBLED, Buzzer
        # 공통 애노드 LED는 핀을 LOW로 해야 켜진다 → active_high를 뒤집는다
        self.led = RGBLED(PIN_LED_R, PIN_LED_G, PIN_LED_B, active_high=not led_common_anode, pwm=False)
        self.buzzer = Buzzer(PIN_BUZZER, active_high=buzzer_active_high, initial_value=False)
        self.buzzer_on = False
        log.info("🔌 실제 액추에이터 사용: RGB LED(GPIO%d/%d/%d), 부저(GPIO%d)",
                 PIN_LED_R, PIN_LED_G, PIN_LED_B, PIN_BUZZER)

    def set_led(self, color: str):
        rgb = self.COLORS.get(color)
        if rgb is None:
            log.warning("알 수 없는 LED 색: %s", color)
            return
        self.led.value = rgb
        log.info("💡 [LED] %s %s", ICON[color], color.upper())

    def set_buzzer(self, on: bool):
        if on == self.buzzer_on:
            return
        self.buzzer_on = on
        if on:
            # beep()는 gpiozero가 만든 별도 쓰레드에서 켜고 끄기를 반복한다 (background=True)
            self.buzzer.beep(on_time=self.BEEP_ON, off_time=self.BEEP_OFF, background=True)
            log.info("🔊 [부저] 삐–삐–삐 ON")
        else:
            self.buzzer.off()
            log.info("🔇 [부저] OFF")

    def close(self):
        self.buzzer.off()
        self.led.off()
        self.buzzer.close()
        self.led.close()


class GpioButton:
    """경보 해제 버튼 (GPIO27 ↔ GND, 내부 풀업). 눌리면 gpiozero의 이벤트 쓰레드에서 콜백이 불린다."""

    def __init__(self, on_press):
        from gpiozero import Button
        self.button = Button(PIN_BUTTON, pull_up=True, bounce_time=0.1)   # 0.1초 떨림(채터링) 무시
        self.on_press = on_press
        self.button.when_pressed = self._pressed
        log.info("🔌 경보 해제 버튼 사용: GPIO%d (누르면 부저 OFF + ALARM_CLEAR 전송)", PIN_BUTTON)

    def _pressed(self):
        log.info("🔘 버튼 눌림")
        self.on_press()

    def close(self):
        self.button.close()


def create_actuator(use_mock: bool) -> BaseActuator:
    if use_mock:
        return MockActuator()
    from common import config
    return GpioActuator(
        led_common_anode=config.get_bool("LED_COMMON_ANODE", False),
        buzzer_active_high=config.get_bool("BUZZER_ACTIVE_HIGH", True),
    )
