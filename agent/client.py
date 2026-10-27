"""에이전트 클라이언트.

쓰레드 구성 (2단계):
  - SensorThread   : 2초마다 센서를 읽어 queue에 넣는다 (연결 상태와 무관하게 계속 측정)
  - SenderThread   : queue에서 꺼내 seq를 붙여 DATA로 전송한다
  - ReceiverThread : 서버가 보내는 CMD를 받아 액추에이터(LED·부저)를 움직인다
  - 메인 쓰레드    : AgentClient.run() — 서버 연결, HELLO 전송, 끊기면 재접속
연결은 하나만 쓰고, 그 하나로 보내기(Sender)와 받기(Receiver)를 동시에 한다.
"""
import logging
import queue
import socket
import threading

from common import protocol

log = logging.getLogger("agent")

SENSOR_INTERVAL = 2.0   # 측정 주기(초)
RECONNECT_DELAY = 3.0   # 2단계까지는 고정 대기, 5단계에서 지수 백오프로 바꾼다


class SensorThread(threading.Thread):
    def __init__(self, sensors: list, out_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(name="SensorThread", daemon=True)
        self.sensors = sensors
        self.out_queue = out_queue
        self.stop_event = stop_event

    def run(self):
        log.info("🌡️ SensorThread 시작 (%.0f초 주기, 센서: %s)",
                 SENSOR_INTERVAL, ", ".join(s.name for s in self.sensors))
        while not self.stop_event.is_set():
            reading = {}
            for sensor in self.sensors:
                try:
                    reading.update(sensor.read())
                except Exception as e:
                    log.warning("센서 %s 읽기 실패: %s", sensor.name, e)
            self.out_queue.put(reading)
            log.info("측정 → 큐 (대기 %d개): %s", self.out_queue.qsize(), reading)
            # wait()은 stop_event가 켜지면 바로 깨어나므로 sleep보다 종료가 빠르다
            self.stop_event.wait(SENSOR_INTERVAL)


class SenderThread(threading.Thread):
    """연결 1개당 하나씩 생긴다. 전송 실패 시 쓰레드가 끝나고, 메인 쓰레드가 재접속한다."""

    def __init__(self, client: "AgentClient", conn_alive: threading.Event):
        super().__init__(name="SenderThread", daemon=True)
        self.client = client
        self.conn_alive = conn_alive

    def run(self):
        log.info("📤 SenderThread 시작")
        try:
            while self.conn_alive.is_set():
                try:
                    reading = self.client.data_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                seq = self.client.next_seq()
                self.client.send(protocol.make_data(self.client.agent_id, seq, reading))
                log.info("📤 DATA 전송 seq=%d", seq)
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
        self.data_queue = queue.Queue()
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
        protocol.send_msg(sock, protocol.make_hello(self.agent_id))
        log.info("👋 HELLO 전송: agent_id=%s", self.agent_id)
        return sock

    def run(self):
        SensorThread(self.sensors, self.data_queue, self.stop_event).start()
        while not self.stop_event.is_set():
            try:
                sock = self.connect()
            except OSError as e:
                log.warning("연결 실패: %s → %.0f초 후 재시도", e, RECONNECT_DELAY)
                self.stop_event.wait(RECONNECT_DELAY)
                continue

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
                log.warning("연결 끊김 → %.0f초 후 재접속", RECONNECT_DELAY)
                self.stop_event.wait(RECONNECT_DELAY)

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
