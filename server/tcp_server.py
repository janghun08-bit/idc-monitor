"""TCP 서버: 메인 쓰레드는 accept만 하고, 접속마다 ClientHandler 쓰레드를 하나씩 만든다."""
import logging
import socket
import threading
import time

from common import protocol
from server.db import DatabaseError

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
            self.handle_data(msg)
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
        """판정 결과 처리: 이벤트는 DB에 기록하고, CMD는 같은 연결로 에이전트에 보낸다."""
        for ev in events:
            log.info("🚨 이벤트 [%s/%s] %s", ev["event_type"], ev["level"], ev["message"])
            try:
                self.server.db.save_event(self.agent_id, time.time(), ev["event_type"], ev["level"], ev["message"])
            except DatabaseError as e:
                log.error("🗄️ 이벤트 저장 실패: %s", e)
        for cmd in commands:
            protocol.send_msg(self.conn, cmd)
            detail = cmd["color"] if cmd["action"] == protocol.SET_LED else ("ON" if cmd["on"] else "OFF")
            log.info("📤 CMD 전송: %s %s", cmd["action"], detail)


class MonitorServer:
    """0.0.0.0:port 에서 접속을 기다리는 서버. 접속한 에이전트 목록을 Lock으로 보호한다."""

    def __init__(self, host: str, port: int, engine, db):
        self.host = host
        self.port = port
        self.engine = engine         # 모든 ClientHandler가 함께 쓰는 AlarmEngine
        self.db = db                 # 모든 ClientHandler가 함께 쓰는 Database
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
