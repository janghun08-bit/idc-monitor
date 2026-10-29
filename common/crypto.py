"""AES-256-GCM 암복호화.

protocol.py의 send_msg/recv_msg가 항상 이 모듈을 거치므로, 모든 메시지가 자동으로 암호화된다.

    payload = nonce(12바이트) + 암호문 + tag(16바이트)

- 키(AES_KEY): .env에 64자 hex(32바이트). 에이전트와 서버가 같은 키를 쓴다.
- nonce: 메시지마다 새로 뽑는 일회용 번호. 같은 내용도 매번 다른 암호문이 된다.
- tag: 위조 검사용 봉인. 키가 다르거나 내용이 한 글자라도 바뀌면 복호화가 실패한다.
- AES_KEY가 비어 있으면 평문으로 통과시킨다 (디버깅용, 시작할 때 경고).
"""
import logging
import os

from common import config

log = logging.getLogger("crypto")

NONCE_SIZE = 12
TAG_SIZE = 16

_cipher = None
_loaded = False


class CryptoError(ValueError):
    """복호화 실패 (키가 다르거나 내용이 변조됨). 이 연결은 믿을 수 없으므로 끊는다."""


def init():
    """시작할 때 한 번 불러 키를 확인한다. 키 형식이 틀리면 바로 종료되게 예외를 낸다."""
    global _cipher, _loaded
    _loaded = True
    key_hex = config.get("AES_KEY", "").strip()
    if not key_hex:
        _cipher = None
        log.warning("⚠️⚠️ AES_KEY가 비어 있음 → 암호화 없이 평문으로 통신합니다 (시연 때는 꼭 키를 넣으세요)")
        return
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        raise SystemExit("❌ AES_KEY는 64자리 16진수(0-9, a-f)여야 합니다")
    if len(key) != 32:
        raise SystemExit(f"❌ AES_KEY 길이가 {len(key)}바이트입니다 (32바이트 = 64자리 필요)")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _cipher = AESGCM(key)
    log.info("🔒 AES-256-GCM 암호화 사용 (모든 메시지 = nonce 12B + 암호문 + tag 16B)")


def enabled() -> bool:
    if not _loaded:
        init()
    return _cipher is not None


def encrypt(plaintext: bytes) -> bytes:
    if not enabled():
        return plaintext
    nonce = os.urandom(NONCE_SIZE)
    return nonce + _cipher.encrypt(nonce, plaintext, None)


def decrypt(payload: bytes) -> bytes:
    if not enabled():
        return payload
    if len(payload) < NONCE_SIZE + TAG_SIZE:
        raise CryptoError("복호화 실패: 암호문이 너무 짧음 (상대가 암호화를 안 쓰는 중?)")
    from cryptography.exceptions import InvalidTag
    nonce, body = payload[:NONCE_SIZE], payload[NONCE_SIZE:]
    try:
        return _cipher.decrypt(nonce, body, None)
    except InvalidTag:
        raise CryptoError("복호화 실패: 키가 다르거나 내용이 변조됨") from None
