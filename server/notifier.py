"""SlackNotifier: 이벤트를 Slack Incoming Webhook으로 보낸다.

Slack이 느리거나 죽어 있어도 경보 처리가 멈추면 안 되므로,
ClientHandler는 notify()로 큐에 넣기만 하고 실제 HTTP 전송은 이 쓰레드가 따로 한다.
전송 타임아웃 3초, 실패하면 로그만 남기고 넘어간다.

SLACK_WEBHOOK_URL이 비어 있으면 보낼 내용을 로그로만 보여준다.
Webhook URL은 비밀값이므로 로그에 절대 출력하지 않는다.
"""
import json
import logging
import queue
import threading
import urllib.error
import urllib.request

from common import config

log = logging.getLogger("slack")

ICON = {
    "DANGER": "🔴", "WARN": "🟡", "ACCESS": "🔵", "NORMAL": "🟢",
    "AGENT_OFFLINE": "⚠️", "AGENT_ONLINE": "✅", "ALARM_CLEAR": "🔕",
}


def format_event(agent_id: str, event_type: str, level, message: str) -> str:
    icon = ICON.get(event_type) or ICON.get(level, "ℹ️")
    return f"{icon} [{agent_id}] {event_type} ({level}) — {message}"


class SlackNotifier(threading.Thread):
    TIMEOUT = 3

    def __init__(self, webhook_url: str):
        super().__init__(name="SlackNotifier", daemon=True)
        self.webhook_url = webhook_url
        self.queue = queue.Queue(maxsize=100)   # Slack이 오래 죽어 있어도 메모리를 다 쓰지 않게

    @classmethod
    def from_config(cls):
        url = config.get("SLACK_WEBHOOK_URL", "")
        if not url:
            log.warning("SLACK_WEBHOOK_URL이 비어 있음 → 알림 내용은 로그로만 표시")
        return cls(url)

    def notify(self, text: str):
        """다른 쓰레드에서 부른다. 큐에 넣고 바로 돌아오므로 호출한 쪽은 기다리지 않는다."""
        if not self.webhook_url:
            log.info("💬 (Slack 미설정) %s", text)
            return
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            log.warning("Slack 대기열이 가득 참 → 알림 버림: %s", text)

    def run(self):
        log.info("💬 SlackNotifier 쓰레드 시작")
        while True:
            self._post(self.queue.get())

    def _post(self, text: str):
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(self.webhook_url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT):
                pass
            log.info("💬 Slack 전송 완료: %s", text)
        except (urllib.error.URLError, OSError) as e:
            # URLError에는 주소가 들어가지 않는다. 실패해도 서버는 계속 동작
            log.warning("💬 Slack 전송 실패 (서버는 계속 동작): %s", getattr(e, "reason", e))
