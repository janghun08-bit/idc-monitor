"""Watchdog: 1초마다 에이전트별 '마지막 수신 시각'을 보고, 너무 오래 조용하면 OFFLINE 처리한다.

TCP 연결이 살아 있어도 에이전트 프로그램이 멈추거나(무한 대기 등)
네트워크가 조용히 끊기면 소켓 쪽에서는 한참 동안 알 수 없다.
그래서 '메시지가 실제로 오고 있는지'를 따로 감시한다. 에이전트는 5초마다 HEARTBEAT를 보낸다.
"""
import logging
import threading
import time

log = logging.getLogger("server")


class Watchdog(threading.Thread):
    CHECK_INTERVAL = 1.0

    def __init__(self, server, timeout: float = 15.0):
        super().__init__(name="Watchdog", daemon=True)
        self.server = server
        self.timeout = timeout

    def run(self):
        log.info("🐕 Watchdog 쓰레드 시작 (%.0f초 무응답 시 OFFLINE)", self.timeout)
        while True:
            time.sleep(self.CHECK_INTERVAL)
            for agent_id, silent in self.server.find_silent_agents(self.timeout):
                self.server.record_event(agent_id, "AGENT_OFFLINE", "WARN",
                                         f"{silent:.0f}초 동안 메시지 없음")
