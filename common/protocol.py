"""TCP 메시지 프레이밍과 메시지 생성 함수.

TCP는 '바이트 스트림'이라서 send 한 번이 recv 한 번으로 그대로 오지 않는다.
(여러 메시지가 붙어서 오거나, 한 메시지가 잘려서 올 수 있음)
그래서 메시지마다 앞에 길이를 붙여 경계를 구분한다.

    프레임 = [4바이트 길이(big-endian)] + [payload]
    payload = UTF-8 JSON  (5단계부터 AES-GCM으로 암호화)
"""
import json
import socket
import struct
import time

from common import crypto

HEADER = struct.Struct(">I")      # 부호 없는 4바이트 정수, big-endian
MAX_FRAME_SIZE = 1024 * 1024      # 비정상 길이 값으로 메모리를 다 쓰는 것을 막는다

# 메시지 type
HELLO = "HELLO"
DATA = "DATA"
ACK = "ACK"
HEARTBEAT = "HEARTBEAT"
EVENT = "EVENT"
CMD = "CMD"

# CMD action / EVENT 종류
SET_LED = "SET_LED"
BUZZER = "BUZZER"
ALARM_CLEAR = "ALARM_CLEAR"


class ConnectionClosed(Exception):
    """상대방이 연결을 닫았을 때 발생."""


def recv_exact(sock, n: int, allow_timeout: bool = True) -> bytes:
    """정확히 n바이트를 받을 때까지 반복해서 recv 한다.

    소켓 타임아웃이 아무것도 받기 전에 나면 그대로 올려 보낸다 (호출한 쪽이 판단).
    하지만 프레임을 받는 도중이면 버리면 안 되므로 끝까지 계속 기다린다.
    """
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            if allow_timeout and not buf:
                raise
            continue
        if not chunk:  # recv가 빈 바이트를 돌려주면 상대가 연결을 닫은 것
            raise ConnectionClosed("상대방이 연결을 종료함")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock, msg: dict) -> None:
    payload = crypto.encrypt(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
    sock.sendall(HEADER.pack(len(payload)) + payload)


def recv_msg(sock) -> dict:
    # 타임아웃은 '프레임과 프레임 사이'에서만 허용한다 (길이 헤더를 읽기 시작하기 전)
    (length,) = HEADER.unpack(recv_exact(sock, HEADER.size))
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"프레임 크기 초과: {length} bytes")
    payload = crypto.decrypt(recv_exact(sock, length, allow_timeout=False))
    return json.loads(payload.decode("utf-8"))


# ---- 메시지 생성 함수 ----

def _base(msg_type: str, agent_id: str) -> dict:
    return {"type": msg_type, "agent_id": agent_id, "ts": time.time()}


def make_hello(agent_id: str) -> dict:
    return _base(HELLO, agent_id)


def make_data(agent_id: str, seq: int, data: dict) -> dict:
    msg = _base(DATA, agent_id)
    msg["seq"] = seq
    msg["data"] = data
    return msg


def make_event(agent_id: str, event: str) -> dict:
    msg = _base(EVENT, agent_id)
    msg["event"] = event
    return msg


def make_led_cmd(agent_id: str, color: str) -> dict:
    msg = _base(CMD, agent_id)
    msg["action"] = SET_LED
    msg["color"] = color
    return msg


def make_buzzer_cmd(agent_id: str, on: bool) -> dict:
    msg = _base(CMD, agent_id)
    msg["action"] = BUZZER
    msg["on"] = on
    return msg
