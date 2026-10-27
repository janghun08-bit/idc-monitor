"""AlarmEngine: 측정값으로 상태를 판정하고, '상태가 바뀔 때만' 할 일을 돌려준다.

판정은 두 갈래로 나눈다.
  - 경보 단계(alarm_level): NORMAL / WARN / DANGER  → 바뀌면 STATE_CHANGE 이벤트
  - 출입(access): 거리 < ACCESS_CM                  → 시작될 때 ACCESS 이벤트 1회
LED 색은 둘을 합쳐 정한다: DANGER(빨강) > WARN(노랑) > ACCESS(파랑) > NORMAL(초록)
부저는 DANGER이고, 버튼으로 끄지(silenced) 않았을 때만 켠다.

evaluate()는 (보낼 CMD 목록, 기록할 이벤트 목록)을 돌려줄 뿐 직접 전송하지 않는다.
전송은 ClientHandler가, 이벤트 저장·알림은 3·4단계에서 DB/Slack이 맡는다.
"""
import threading

from common import config, protocol

NORMAL = "NORMAL"
WARN = "WARN"
DANGER = "DANGER"
ACCESS = "ACCESS"

LED_COLOR = {DANGER: "red", WARN: "yellow", ACCESS: "blue", NORMAL: "green"}


class AgentState:
    """에이전트 1대의 현재 상태. '이전 값'을 기억해야 변화를 감지할 수 있다."""

    def __init__(self):
        self.alarm_level = None   # None = 아직 첫 판정 전
        self.access = False
        self.silenced = False     # 버튼으로 부저를 끈 상태
        self.led = None           # 마지막으로 보낸 LED 색
        self.buzzer = None        # 마지막으로 보낸 부저 상태


class AlarmEngine:
    def __init__(self, temp_warn: float, temp_danger: float, humi_warn: float, access_cm: float):
        self.temp_warn = temp_warn
        self.temp_danger = temp_danger
        self.humi_warn = humi_warn
        self.access_cm = access_cm
        self.states = {}               # agent_id → AgentState
        self.lock = threading.Lock()   # 여러 ClientHandler 쓰레드가 states를 함께 쓰므로 보호

    @classmethod
    def from_config(cls):
        return cls(
            temp_warn=config.get_float("TEMP_WARN", 30),
            temp_danger=config.get_float("TEMP_DANGER", 35),
            humi_warn=config.get_float("HUMI_WARN", 70),
            access_cm=config.get_float("ACCESS_CM", 30),
        )

    def describe(self) -> str:
        return (f"온도 경고 {self.temp_warn}℃ / 위험 {self.temp_danger}℃, "
                f"습도 경고 {self.humi_warn}%, 접근 {self.access_cm}cm 미만")

    def reset(self, agent_id: str):
        """새로 접속(HELLO)하면 처음부터 다시 판정해 LED·부저를 맞춘다."""
        with self.lock:
            self.states[agent_id] = AgentState()

    def classify(self, data: dict):
        """측정값 → (경보 단계, 원인 목록, 출입 여부). 센서 값이 None이면 그 항목은 건너뛴다."""
        temp = data.get("temperature")
        humi = data.get("humidity")
        dist = data.get("distance_cm")
        danger, warn = [], []
        if data.get("leak"):
            danger.append("누수 감지")
        if temp is not None and temp >= self.temp_danger:
            danger.append(f"온도 {temp}℃ ≥ {self.temp_danger}")
        elif temp is not None and temp >= self.temp_warn:
            warn.append(f"온도 {temp}℃ ≥ {self.temp_warn}")
        if humi is not None and humi >= self.humi_warn:
            warn.append(f"습도 {humi}% ≥ {self.humi_warn}")

        if danger:
            level, reasons = DANGER, danger + warn
        elif warn:
            level, reasons = WARN, warn
        else:
            level, reasons = NORMAL, []
        access = dist is not None and dist < self.access_cm
        return level, reasons, access

    def evaluate(self, agent_id: str, data: dict):
        level, reasons, access = self.classify(data)
        events = []
        with self.lock:
            st = self.states.setdefault(agent_id, AgentState())

            if level != st.alarm_level:
                prev = st.alarm_level
                # 접속 직후 첫 판정이 NORMAL이면 굳이 '변화'로 기록하지 않는다
                if prev is not None or level != NORMAL:
                    reason = ", ".join(reasons) if reasons else "정상 범위"
                    events.append(_event("STATE_CHANGE", level, f"{prev or '시작'} → {level} ({reason})"))
                if level != DANGER:
                    st.silenced = False   # 위험에서 벗어나면 버튼 해제 상태도 초기화
                st.alarm_level = level

            if access and not st.access:
                events.append(_event("ACCESS", ACCESS, f"출입 감지 (거리 {data.get('distance_cm')}cm)"))
            st.access = access

            commands = self._sync_outputs(agent_id, st)
        return commands, events

    def alarm_clear(self, agent_id: str):
        """버튼: 부저만 끈다. LED는 원인이 남아 있으면 그대로."""
        with self.lock:
            st = self.states.setdefault(agent_id, AgentState())
            st.silenced = True
            events = [_event("ALARM_CLEAR", st.alarm_level, "현장 버튼으로 부저 해제")]
            commands = self._sync_outputs(agent_id, st)
        return commands, events

    def _sync_outputs(self, agent_id: str, st: AgentState) -> list:
        """지금 상태에서 LED·부저가 어때야 하는지 계산해서, 마지막으로 보낸 값과 다를 때만 CMD를 만든다."""
        if st.alarm_level in (DANGER, WARN):
            display = st.alarm_level
        elif st.access:
            display = ACCESS
        else:
            display = NORMAL
        color = LED_COLOR[display]
        buzzer = st.alarm_level == DANGER and not st.silenced

        commands = []
        if color != st.led:
            commands.append(protocol.make_led_cmd(agent_id, color))
            st.led = color
        if buzzer != st.buzzer:
            commands.append(protocol.make_buzzer_cmd(agent_id, buzzer))
            st.buzzer = buzzer
        return commands


def _event(event_type: str, level, message: str) -> dict:
    return {"event_type": event_type, "level": level, "message": message}
