"""AES-GCM 암복호화.

5단계 전까지는 평문을 그대로 통과시킨다.
protocol.py는 항상 이 모듈을 거쳐 송수신하므로, 5단계에서는 이 파일만 바꾸면 된다.
"""


def encrypt(plaintext: bytes) -> bytes:
    return plaintext


def decrypt(payload: bytes) -> bytes:
    return payload
