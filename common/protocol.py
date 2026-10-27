"""TCP 메시지 프레이밍과 메시지 생성 함수.

TCP는 '바이트 스트림'이라서 send 한 번이 recv 한 번으로 그대로 오지 않는다.
(여러 메시지가 붙어서 오거나, 한 메시지가 잘려서 올 수 있음)
그래서 메시지마다 앞에 길이를 붙여 경계를 구분한다.

    프레임 = [4바이트 길이(big-endian)] + [payload]
    payload = UTF-8 JSON  (5단계부터 AES-GCM으로 암호화)
"""
import json
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


class ConnectionClosed(Exception):
    """상대방이 연결을 닫았을 때 발생."""


def recv_exact(sock, n: int) -> bytes:
    """정확히 n바이트를 받을 때까지 반복해서 recv 한다."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:  # recv가 빈 바이트를 돌려주면 상대가 연결을 닫은 것
            raise ConnectionClosed("상대방이 연결을 종료함")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock, msg: dict) -> None:
    payload = crypto.encrypt(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
    sock.sendall(HEADER.pack(len(payload)) + payload)


def recv_msg(sock) -> dict:
    (length,) = HEADER.unpack(recv_exact(sock, HEADER.size))
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"프레임 크기 초과: {length} bytes")
    payload = crypto.decrypt(recv_exact(sock, length))
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
