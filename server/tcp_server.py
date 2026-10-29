"""TCP 서버: 메인 쓰레드는 accept만 하고, 접속마다 ClientHandler 쓰레드를 하나씩 만든다."""
import logging
import socket
import sys
import threading
import time

from common import protocol
from server.db import DatabaseError
from server.notifier import format_event

log = logging.getLogger("server")

CLIENT_TIMEOUT = 30  # 이 시간 동안 아무것도 안 오면 연결이 죽은 것으로 보고 끊는다 (하트비트는 5초마다)


def _fmt(value) -> str:
    # 센서 읽기에 실패하면 None이 올 수 있다
    return "--" if value is None else f"{value:.1f}"


class ClientHandler(threading.Thread):
    """에이전트 1대와의 연결을 담당하는 쓰레드. recv 루프를 돌며 메시지를 처리한다."""

    def __init__(self, conn: socket.socket, addr, server: "MonitorServer"):
        super().__init__(name=f"Handler-{addr[1]}", daemon=True)
        self.conn = conn
        self.addr = addr
        self.server = server
        self.agent_id = None

    def run(self):
        log.info("🔌 ClientHandler 쓰레드 시작: %s:%s", *self.addr)
        self.conn.settimeout(CLIENT_TIMEOUT)
        try:
            while True:
                msg = protocol.recv_msg(self.conn)
                self.handle(msg)
                # 어떤 메시지든 오면 '살아 있음'. OFFLINE이었다면 ONLINE으로 되돌린다
                if self.agent_id and self.server.touch(self.agent_id):
                    self.server.record_event(self.agent_id, "AGENT_ONLINE", "NORMAL", "다시 응답함")
        except protocol.ConnectionClosed:
            log.info("연결 종료됨 (agent=%s)", self.agent_id)
        except socket.timeout:
            log.warning("%d초 동안 수신 없음 → 연결 끊음 (agent=%s)", CLIENT_TIMEOUT, self.agent_id)
        except (OSError, ValueError) as e:
            log.warning("연결 오류: %s (agent=%s)", e, self.agent_id)
        finally:
            self.conn.close()
            self.server.unregister(self)
            log.info("🔌 ClientHandler 쓰레드 종료")

    def handle(self, msg: dict):
        msg_type = msg.get("type")
        engine = self.server.engine
        if msg_type == protocol.HELLO:
            self.agent_id = msg["agent_id"]
            self.name = f"Handler-{self.agent_id}"  # 로그에서 어느 에이전트 쓰레드인지 보이게
            self.server.register(self)
            engine.reset(self.agent_id)
            log.info("👋 HELLO 수신: agent_id=%s 등록", self.agent_id)
            # 에이전트가 다시 켜지면 seq가 1부터 시작해 DB의 옛 기록과 겹친다 → 마지막 번호를 알려준다
            try:
                last_seq = self.server.db.last_seq(self.agent_id)
            except DatabaseError as e:
                log.error("🗄️ 마지막 seq 조회 실패: %s", e)
                last_seq = None
            protocol.send_msg(self.conn, protocol.make_welcome(self.agent_id, last_seq))
            log.info("📤 WELCOME 전송: 마지막 저장 seq=%s", last_seq)
        elif msg_type == protocol.DATA:
            self.handle_data(msg)
        elif msg_type == protocol.HEARTBEAT:
            log.info("💓 HEARTBEAT 수신")
        elif msg_type == protocol.EVENT:
            log.info("🔔 EVENT 수신: %s", msg.get("event"))
            if msg.get("event") == protocol.ALARM_CLEAR:
                self.apply(*engine.alarm_clear(self.agent_id))
        else:
            log.warning("알 수 없는 메시지 type: %s", msg_type)

    def handle_data(self, msg: dict):
        """DATA 처리 순서: ① DB 저장 → ② ACK 응답 → ③ 상태 판정."""
        seq, d = msg["seq"], msg["data"]
        log.info(
            "📥 DATA seq=%d | 온도 %s℃  습도 %s%%  누수 %s  거리 %scm",
            seq, _fmt(d.get("temperature")), _fmt(d.get("humidity")),
            "있음" if d.get("leak") else "없음", _fmt(d.get("distance_cm")),
        )
        try:
            is_new = self.server.db.save_reading(self.agent_id, seq, msg["ts"], d)
        except DatabaseError as e:
            # 저장이 안 됐으니 ACK를 보내지 않는다 → 에이전트가 나중에 다시 보낼 수 있다(5단계)
            log.error("🗄️ DB 저장 실패 seq=%d: %s → ACK 안 보냄", seq, e)
            is_new = None
        else:
            if is_new:
                log.info("🗄️ DB 저장 완료 seq=%d", seq)
            else:
                log.info("🗄️ 이미 저장된 seq=%d (중복) → 저장 생략", seq)
            protocol.send_msg(self.conn, protocol.make_ack(self.agent_id, seq))
            log.info("📤 ACK 전송 seq=%d", seq)

        # 중복(재전송된 옛날 값)은 이미 판정했으므로 다시 판정하지 않는다.
        # DB가 고장 나도 경보는 울려야 하므로 저장 실패(None)일 때는 판정한다.
        if is_new is not False:
            self.apply(*self.server.engine.evaluate(self.agent_id, d))

    def apply(self, commands: list, events: list):
        """판정 결과 처리: CMD는 같은 연결로 에이전트에 먼저 보내고, 이벤트는 기록·알림한다."""
        for cmd in commands:
            protocol.send_msg(self.conn, cmd)
            detail = cmd["color"] if cmd["action"] == protocol.SET_LED else ("ON" if cmd["on"] else "OFF")
            log.info("📤 CMD 전송: %s %s", cmd["action"], detail)
        for ev in events:
            self.server.record_event(self.agent_id, ev["event_type"], ev["level"], ev["message"])


class MonitorServer:
    """0.0.0.0:port 에서 접속을 기다리는 서버.
    여러 쓰레드(ClientHandler들, Watchdog)가 함께 쓰는 목록은 Lock으로 보호한다."""

    def __init__(self, host: str, port: int, engine, db, notifier):
        self.host = host
        self.port = port
        self.engine = engine         # 모든 ClientHandler가 함께 쓰는 AlarmEngine
        self.db = db                 # 모든 ClientHandler가 함께 쓰는 Database
        self.notifier = notifier     # SlackNotifier
        self.clients = {}            # agent_id → ClientHandler (지금 연결된 것만)
        self.liveness = {}           # agent_id → {"last_seen", "online"} (연결이 끊겨도 남겨서 Watchdog이 감시)
        self.lock = threading.Lock()

    def register(self, handler: ClientHandler):
        with self.lock:
            self.clients[handler.agent_id] = handler
            count = len(self.clients)
        log.info("현재 접속 에이전트 수: %d", count)

    def unregister(self, handler: ClientHandler):
        with self.lock:
            if self.clients.get(handler.agent_id) is handler:
                del self.clients[handler.agent_id]
            count = len(self.clients)
        log.info("현재 접속 에이전트 수: %d", count)

    def touch(self, agent_id: str) -> bool:
        """마지막 수신 시각 갱신. OFFLINE이었다가 돌아온 경우 True."""
        with self.lock:
            st = self.liveness.get(agent_id)
            was_offline = st is not None and not st["online"]
            self.liveness[agent_id] = {"last_seen": time.monotonic(), "online": True}
        return was_offline

    def find_silent_agents(self, timeout: float) -> list:
        """Watchdog이 부른다. timeout보다 오래 조용한 ONLINE 에이전트를 OFFLINE으로 바꾸고 돌려준다."""
        now = time.monotonic()
        silent = []
        with self.lock:
            for agent_id, st in self.liveness.items():
                elapsed = now - st["last_seen"]
                if st["online"] and elapsed > timeout:
                    st["online"] = False
                    silent.append((agent_id, elapsed))
        return silent

    def record_event(self, agent_id: str, event_type: str, level, message: str):
        """이벤트 한 곳에서 처리: 로그 → DB 저장 → Slack 알림. ClientHandler와 Watchdog이 함께 쓴다."""
        log.info("🚨 이벤트 [%s/%s] %s", event_type, level, message)
        try:
            self.db.save_event(agent_id, time.time(), event_type, level, message)
        except DatabaseError as e:
            log.error("🗄️ 이벤트 저장 실패: %s", e)
        self.notifier.notify(format_event(agent_id, event_type, level, message))

    def serve_forever(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # Windows의 SO_REUSEADDR은 '이미 쓰는 포트'에 또 bind를 허용해 버린다 → 독점 사용으로 막는다
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Linux: 서버를 껐다 바로 켤 때 TIME_WAIT 때문에 bind가 실패하는 것을 막는다
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as e:
            log.error("❌ 포트 %d를 열 수 없음: %s (이미 다른 서버가 켜져 있는지 확인)", self.port, e)
            raise SystemExit(1)
        sock.listen()
        # accept에 타임아웃을 걸어 두어야 Ctrl+C가 바로 먹힌다 (특히 Windows)
        sock.settimeout(1.0)
        log.info("🟢 서버 대기 중: %s:%d (accept 루프 시작)", self.host, self.port)
        try:
            while True:
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                log.info("📞 새 연결 수락: %s:%s → 전용 쓰레드 생성", *addr)
                ClientHandler(conn, addr, self).start()
        finally:
            sock.close()
            log.info("서버 소켓 닫힘")
