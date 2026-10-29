"""에이전트 클라이언트.

쓰레드 구성:
  - SensorThread   : 2초마다 센서를 읽어 대기함(buffer)에 넣는다 (연결 상태와 무관하게 계속 측정)
  - SenderThread   : 대기함에서 꺼내 seq를 붙여 DATA로 전송하고, 5초마다 HEARTBEAT를 보낸다
  - ReceiverThread : 서버가 보내는 CMD를 받아 액추에이터(LED·부저)를 움직이고, ACK가 오면 확인 대기함에서 지운다
  - 메인 쓰레드    : AgentClient.run() — 서버 연결, HELLO/WELCOME, 끊기면 1→2→4…10초 간격으로 재접속
연결은 하나만 쓰고, 그 하나로 보내기(Sender)와 받기(Receiver)를 동시에 한다.

보고(DATA)를 잃어버리지 않는 구조 (store-and-forward):
  측정 → [대기함 buffer] → 전송 → [확인 대기함 pending] → ACK 받으면 삭제
  - 끊긴 동안에는 대기함에 쌓인다 (최대 1000개, 넘치면 가장 오래된 것부터 버림)
  - 재연결하면 확인 대기함의 것을 먼저 번호 순서대로 다시 보내고, 그다음 대기함 것을 보낸다
  - 연결 중이라도 10초 동안 ACK가 없으면 다시 보낸다 (서버 DB가 잠깐 죽은 경우)
  - 같은 번호가 두 번 가도 서버가 중복 저장하지 않는다
"""
import collections
import json
import logging
import socket
import threading
import time

from common import crypto, protocol

log = logging.getLogger("agent")

SENSOR_INTERVAL = 2.0   # 측정 주기(초)
HEARTBEAT_INTERVAL = 5.0
BUFFER_MAX = 1000       # 대기함·확인 대기함 각각의 최대 개수 (2초 주기면 약 33분치)
ACK_TIMEOUT = 10.0      # 보낸 뒤 이 시간 안에 ACK가 없으면 재전송
BACKOFF_START = 1.0     # 재접속 간격: 1 → 2 → 4 → 8 → 10 → 10 …
BACKOFF_MAX = 10.0


class DataBuffer:
    """대기함: 측정했지만 아직 안 보낸 값. SensorThread가 넣고 SenderThread가 꺼낸다."""

    def __init__(self, maxlen: int):
        self.items = collections.deque()
        self.maxlen = maxlen
        self.cond = threading.Condition()   # Lock + '들어왔어요' 신호 (꺼내는 쪽이 기다릴 수 있게)

    def put(self, item):
        with self.cond:
            if len(self.items) >= self.maxlen:
                self.items.popleft()
                log.warning("⚠️ 대기함이 가득 참(%d개) → 가장 오래된 측정값 1개 버림", self.maxlen)
            self.items.append(item)
            self.cond.notify()
            return len(self.items)

    def get(self, timeout: float):
        """하나 꺼낸다. timeout 동안 없으면 None."""
        with self.cond:
            if not self.items:
                self.cond.wait(timeout)
            return self.items.popleft() if self.items else None

    def __len__(self):
        with self.cond:
            return len(self.items)


class PendingStore:
    """확인 대기함: 보냈지만 ACK를 아직 못 받은 DATA (seq → 메시지).
    SenderThread가 넣고, ReceiverThread가 ACK를 받으면 지운다 → 두 쓰레드가 같이 쓰므로 Lock."""

    def __init__(self, maxlen: int):
        self.items = collections.OrderedDict()   # seq → [msg, 마지막 전송 시각]
        self.maxlen = maxlen
        self.lock = threading.Lock()

    def add(self, msg: dict):
        with self.lock:
            if len(self.items) >= self.maxlen:
                old_seq, _ = self.items.popitem(last=False)
                log.warning("⚠️ 확인 대기함이 가득 참 → seq=%d 포기", old_seq)
            self.items[msg["seq"]] = [msg, time.monotonic()]

    def ack(self, seq: int):
        """ACK 받은 seq 삭제. 남은 개수를 돌려준다 (없던 seq면 None)."""
        with self.lock:
            if self.items.pop(seq, None) is None:
                return None
            return len(self.items)

    def take_all(self) -> list:
        """재연결 직후: 전부 seq 순서대로 (전송 시각은 지금으로 갱신)."""
        return self._take(lambda sent_at: True)

    def take_overdue(self, timeout: float) -> list:
        """보낸 지 timeout이 넘었는데 ACK가 없는 것들."""
        limit = time.monotonic() - timeout
        return self._take(lambda sent_at: sent_at < limit)

    def _take(self, pick) -> list:
        now = time.monotonic()
        with self.lock:
            picked = [entry for entry in self.items.values() if pick(entry[1])]
            for entry in picked:
                entry[1] = now
            return sorted((entry[0] for entry in picked), key=lambda m: m["seq"])

    def __len__(self):
        with self.lock:
            return len(self.items)


class SensorThread(threading.Thread):
    def __init__(self, sensors: list, buffer: DataBuffer, stop_event: threading.Event):
        super().__init__(name="SensorThread", daemon=True)
        self.sensors = sensors
        self.buffer = buffer
        self.stop_event = stop_event

    def run(self):
        log.info("🌡️ SensorThread 시작 (%.0f초 주기, 센서: %s)",
                 SENSOR_INTERVAL, ", ".join(s.name for s in self.sensors))
        while not self.stop_event.is_set():
            measured_at = time.time()
            reading = {}
            for sensor in self.sensors:
                try:
                    reading.update(sensor.read())
                except Exception as e:
                    log.warning("센서 %s 읽기 실패: %s", sensor.name, e)
            count = self.buffer.put((measured_at, reading))
            log.info("측정 → 대기함 (대기 %d개): %s", count, reading)
            # wait()은 stop_event가 켜지면 바로 깨어나므로 sleep보다 종료가 빠르다
            self.stop_event.wait(SENSOR_INTERVAL)


class SenderThread(threading.Thread):
    """연결 1개당 하나씩 생긴다. 전송 실패 시 쓰레드가 끝나고, 메인 쓰레드가 재접속한다.
    보내기 전에 확인 대기함에 먼저 넣으므로, 보내다 끊겨도 그 보고는 남아 있다."""

    def __init__(self, client: "AgentClient", conn_alive: threading.Event):
        super().__init__(name="SenderThread", daemon=True)
        self.client = client
        self.conn_alive = conn_alive

    def run(self):
        log.info("📤 SenderThread 시작")
        client = self.client
        last_heartbeat = time.monotonic()
        try:
            # ① 지난 연결에서 ACK를 못 받은 것부터 번호 순서대로 다시 보낸다
            resend = client.pending.take_all()
            if resend:
                log.info("🔁 재연결 → ACK 못 받은 %d개 재전송 (seq %d~%d)",
                         len(resend), resend[0]["seq"], resend[-1]["seq"])
            for msg in resend:
                client.send(msg)
                log.info("🔁 재전송 seq=%d", msg["seq"])
            backlog = len(client.buffer)
            if backlog > 1:
                log.info("📦 끊긴 동안 쌓인 측정값 %d개를 순서대로 전송", backlog)

            while self.conn_alive.is_set():
                # 측정값과 상관없이 5초마다 '살아 있음' 신호 (서버 Watchdog이 감시)
                if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL:
                    client.send(protocol.make_heartbeat(client.agent_id))
                    log.info("💓 HEARTBEAT 전송")
                    last_heartbeat = time.monotonic()
                # ② 연결은 살아 있는데 ACK가 안 오는 것 (예: 서버 DB 고장) → 다시 보낸다
                for msg in client.pending.take_overdue(ACK_TIMEOUT):
                    client.send(msg)
                    log.info("🔁 ACK 없음(%.0f초) → 재전송 seq=%d", ACK_TIMEOUT, msg["seq"])
                # ③ 새 측정값 전송
                item = client.buffer.get(timeout=1.0)
                if item is None:
                    continue
                measured_at, reading = item
                msg = protocol.make_data(client.agent_id, client.next_seq(), reading, ts=measured_at)
                client.pending.add(msg)   # 보내기 '전에' 넣어야 보내다 끊겨도 잃지 않는다
                client.send(msg)
                log.info("📤 DATA 전송 seq=%d (확인 대기 %d개)", msg["seq"], len(client.pending))
        except OSError as e:
            log.warning("전송 실패: %s", e)
        log.info("📤 SenderThread 종료")


class ReceiverThread(threading.Thread):
    """연결 1개당 하나씩 생긴다. recv에서 기다리다가 서버 메시지가 오면 처리한다."""

    def __init__(self, client: "AgentClient", sock: socket.socket, conn_alive: threading.Event):
        super().__init__(name="ReceiverThread", daemon=True)
        self.client = client
        self.sock = sock
        self.conn_alive = conn_alive

    def run(self):
        log.info("📥 ReceiverThread 시작")
        try:
            while self.conn_alive.is_set():
                try:
                    msg = protocol.recv_msg(self.sock)
                except socket.timeout:
                    continue   # 서버는 상태가 바뀔 때만 보내므로 한동안 조용한 게 정상
                self.client.handle_server_msg(msg)
        except protocol.ConnectionClosed:
            log.warning("서버가 연결을 닫음")
        except crypto.CryptoError as e:
            log.error("🔒 %s → 연결 끊음", e)
        except (OSError, ValueError) as e:
            if self.conn_alive.is_set():
                log.warning("수신 실패: %s", e)
        log.info("📥 ReceiverThread 종료")


class AgentClient:
    def __init__(self, agent_id: str, host: str, port: int, sensors: list, actuator):
        self.agent_id = agent_id
        self.host = host
        self.port = port
        self.sensors = sensors
        self.actuator = actuator
        self.buffer = DataBuffer(BUFFER_MAX)
        self.pending = PendingStore(BUFFER_MAX)
        self.stop_event = threading.Event()
        self.seq = 0
        self.sock = None
        # SenderThread와 버튼(다른 쓰레드)이 같은 소켓에 동시에 쓰면 프레임이 섞이므로 보호
        self.send_lock = threading.Lock()

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def send(self, msg: dict):
        with self.send_lock:
            if self.sock is None:
                raise OSError("서버와 연결되어 있지 않음")
            protocol.send_msg(self.sock, msg)

    def handle_server_msg(self, msg: dict):
        if msg.get("type") == protocol.ACK:
            # 서버가 DB에 저장을 끝냈다는 뜻 → 확인 대기함에서 지운다
            left = self.pending.ack(msg["seq"])
            if left is None:
                log.info("✅ ACK 수신 seq=%d (이미 확인된 번호)", msg["seq"])
            else:
                log.info("✅ ACK 수신 seq=%d → 확인 대기함에서 삭제 (남은 %d개)", msg["seq"], left)
            return
        if msg.get("type") != protocol.CMD:
            log.warning("알 수 없는 메시지 type: %s", msg.get("type"))
            return
        action = msg.get("action")
        if action == protocol.SET_LED:
            log.info("📥 CMD 수신: LED → %s", msg["color"])
            self.actuator.set_led(msg["color"])
        elif action == protocol.BUZZER:
            log.info("📥 CMD 수신: 부저 → %s", "ON" if msg["on"] else "OFF")
            self.actuator.set_buzzer(msg["on"])
        else:
            log.warning("알 수 없는 CMD action: %s", action)

    def on_button(self):
        """경보 해제 버튼: 부저는 현장에서 바로 끄고, 서버에도 알린다."""
        self.actuator.set_buzzer(False)
        try:
            self.send(protocol.make_event(self.agent_id, protocol.ALARM_CLEAR))
            log.info("📤 EVENT 전송: ALARM_CLEAR")
        except OSError as e:
            log.warning("EVENT 전송 실패: %s", e)

    def connect(self) -> socket.socket:
        log.info("서버 연결 시도: %s:%d", self.host, self.port)
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(10)  # send가 무한정 멈추지 않도록, recv는 10초마다 한 번씩 깨어나도록
        log.info("✅ TCP 연결 성공 (로컬 %s:%s)", *sock.getsockname())
        try:
            hello = protocol.make_hello(self.agent_id)
            frame = protocol.encode_frame(hello)
            self._show_encryption(hello, frame)
            sock.sendall(frame)
            log.info("👋 HELLO 전송: agent_id=%s", self.agent_id)
            # 송수신 쓰레드를 띄우기 전에, 서버의 WELCOME을 여기서 직접 한 번 받는다
            welcome = protocol.recv_msg(sock)
        except (OSError, ValueError, protocol.ConnectionClosed) as e:
            sock.close()
            raise OSError(f"HELLO/WELCOME 실패: {e}") from e
        if welcome.get("type") != protocol.WELCOME:
            sock.close()
            raise OSError(f"WELCOME 대신 {welcome.get('type')} 수신")
        self.sync_seq(welcome.get("last_seq"))
        return sock

    @staticmethod
    def _show_encryption(msg: dict, frame: bytes):
        """시연용: 같은 메시지가 실제로 어떤 바이트로 나가는지 보여준다."""
        if not crypto.enabled():
            return
        log.info("🔒 평문  : %s", json.dumps(msg, ensure_ascii=False))
        log.info("🔒 실제 전송: 길이 %s | nonce %s | 암호문+tag %s… (총 %d바이트, 매번 다름)",
                 frame[:4].hex(" "), frame[4:16].hex(" "), frame[16:28].hex(" "), len(frame))

    def sync_seq(self, last_seq):
        """서버가 알려준 마지막 저장 번호보다 내 번호가 작으면, 그 다음 번호부터 이어서 쓴다."""
        if last_seq is None:
            log.info("📥 WELCOME 수신: 서버가 마지막 번호를 모름 → 내 번호(%d) 그대로 사용", self.seq)
        elif last_seq > self.seq:
            log.info("📥 WELCOME 수신: 서버 마지막 저장 seq=%d → %d번부터 이어서 보냄", last_seq, last_seq + 1)
            self.seq = last_seq
        else:
            log.info("📥 WELCOME 수신: 서버 마지막 저장 seq=%d, 내 번호(%d)가 같거나 큼 → 그대로 사용", last_seq, self.seq)

    def run(self):
        SensorThread(self.sensors, self.buffer, self.stop_event).start()
        delay = BACKOFF_START
        while not self.stop_event.is_set():
            try:
                sock = self.connect()
            except OSError as e:
                log.warning("연결 실패: %s → %.0f초 후 재시도 (대기함 %d개)", e, delay, len(self.buffer))
                self.stop_event.wait(delay)
                delay = min(delay * 2, BACKOFF_MAX)   # 실패할수록 간격을 두 배로 (최대 10초)
                continue
            delay = BACKOFF_START   # 연결에 성공하면 간격을 처음으로

            with self.send_lock:
                self.sock = sock
            conn_alive = threading.Event()
            conn_alive.set()
            sender = SenderThread(self, conn_alive)
            receiver = ReceiverThread(self, sock, conn_alive)
            sender.start()
            receiver.start()

            # 둘 중 하나라도 끝나면 연결에 문제가 생긴 것. 짧게 끊어 기다려야 Ctrl+C가 먹힌다
            while sender.is_alive() and receiver.is_alive():
                sender.join(timeout=0.5)
            self._close_connection(sock, conn_alive)
            sender.join()
            receiver.join()

            if not self.stop_event.is_set():
                log.warning("연결 끊김 → %.0f초 후 재접속 (확인 대기 %d개는 재연결 후 다시 보냄)",
                            delay, len(self.pending))
                self.stop_event.wait(delay)
                delay = min(delay * 2, BACKOFF_MAX)

    def _close_connection(self, sock: socket.socket, conn_alive: threading.Event):
        conn_alive.clear()
        with self.send_lock:
            self.sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)  # recv에서 기다리던 ReceiverThread를 깨운다
        except OSError:
            pass
        sock.close()

    def stop(self):
        self.stop_event.set()
