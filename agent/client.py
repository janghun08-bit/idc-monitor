"""에이전트 클라이언트.

쓰레드 구성 (1단계):
  - SensorThread : 2초마다 센서를 읽어 queue에 넣는다 (연결 상태와 무관하게 계속 측정)
  - SenderThread : queue에서 꺼내 seq를 붙여 DATA로 전송한다
  - 메인 쓰레드  : AgentClient.run() — 서버 연결, HELLO 전송, 끊기면 재접속
ReceiverThread(서버 → 에이전트 수신)는 2단계에서 추가한다.
"""
import logging
import queue
import socket
import threading
import time

from common import protocol

log = logging.getLogger("agent")

SENSOR_INTERVAL = 2.0   # 측정 주기(초)
RECONNECT_DELAY = 3.0   # 1단계는 고정 대기, 5단계에서 지수 백오프로 바꾼다


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

    def __init__(self, client: "AgentClient", sock: socket.socket):
        super().__init__(name="SenderThread", daemon=True)
        self.client = client
        self.sock = sock

    def run(self):
        log.info("📤 SenderThread 시작")
        try:
            while not self.client.stop_event.is_set():
                try:
                    reading = self.client.data_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                seq = self.client.next_seq()
                protocol.send_msg(self.sock, protocol.make_data(self.client.agent_id, seq, reading))
                log.info("📤 DATA 전송 seq=%d", seq)
        except OSError as e:
            log.warning("전송 실패: %s", e)
        log.info("📤 SenderThread 종료")


class AgentClient:
    def __init__(self, agent_id: str, host: str, port: int, sensors: list):
        self.agent_id = agent_id
        self.host = host
        self.port = port
        self.sensors = sensors
        self.data_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.seq = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def connect(self) -> socket.socket:
        log.info("서버 연결 시도: %s:%d", self.host, self.port)
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(10)  # 이후 send가 무한정 멈추지 않도록
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

            sender = SenderThread(self, sock)
            sender.start()
            # SenderThread가 살아있는 동안 = 연결이 살아있는 동안. 짧게 끊어 기다려야 Ctrl+C가 먹힌다
            while sender.is_alive():
                sender.join(timeout=0.5)
            sock.close()
            if not self.stop_event.is_set():
                log.warning("연결 끊김 → %.0f초 후 재접속", RECONNECT_DELAY)
                self.stop_event.wait(RECONNECT_DELAY)

    def stop(self):
        self.stop_event.set()
