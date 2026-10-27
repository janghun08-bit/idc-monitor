"""TCP 서버: 메인 쓰레드는 accept만 하고, 접속마다 ClientHandler 쓰레드를 하나씩 만든다."""
import logging
import socket
import threading

from common import protocol

log = logging.getLogger("server")

CLIENT_TIMEOUT = 60  # 이 시간 동안 아무것도 안 오면 연결이 죽은 것으로 보고 끊는다


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
        elif msg_type == protocol.DATA:
            d = msg["data"]
            log.info(
                "📥 DATA seq=%d | 온도 %s℃  습도 %s%%  누수 %s  거리 %scm",
                msg["seq"], _fmt(d.get("temperature")), _fmt(d.get("humidity")),
                "있음" if d.get("leak") else "없음", _fmt(d.get("distance_cm")),
            )
            self.apply(*engine.evaluate(self.agent_id, d))
        elif msg_type == protocol.EVENT:
            log.info("🔔 EVENT 수신: %s", msg.get("event"))
            if msg.get("event") == protocol.ALARM_CLEAR:
                self.apply(*engine.alarm_clear(self.agent_id))
        else:
            log.warning("알 수 없는 메시지 type: %s", msg_type)

    def apply(self, commands: list, events: list):
        """판정 결과 처리: 이벤트는 로그로 남기고(3단계부터 DB), CMD는 같은 연결로 에이전트에 보낸다."""
        for ev in events:
            log.info("🚨 이벤트 [%s/%s] %s", ev["event_type"], ev["level"], ev["message"])
        for cmd in commands:
            protocol.send_msg(self.conn, cmd)
            detail = cmd["color"] if cmd["action"] == protocol.SET_LED else ("ON" if cmd["on"] else "OFF")
            log.info("📤 CMD 전송: %s %s", cmd["action"], detail)


class MonitorServer:
    """0.0.0.0:port 에서 접속을 기다리는 서버. 접속한 에이전트 목록을 Lock으로 보호한다."""

    def __init__(self, host: str, port: int, engine):
        self.host = host
        self.port = port
        self.engine = engine         # 모든 ClientHandler가 함께 쓰는 AlarmEngine
        self.clients = {}            # agent_id → ClientHandler
        self.lock = threading.Lock() # clients는 여러 Handler 쓰레드가 동시에 바꾸므로 보호

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

    def serve_forever(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
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
