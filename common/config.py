"""설정 로더: 프로젝트 루트의 .env 파일을 읽어 환경변수로 등록한다.

외부 라이브러리(python-dotenv) 없이 동작하도록 직접 파싱한다.
이미 OS 환경변수로 설정된 값이 있으면 그 값을 우선한다.
"""
import logging
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_env(path=None):
    env_path = Path(path) if path else ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def get(key, default=None):
    return os.environ.get(key, default)


def get_int(key, default):
    return int(os.environ.get(key, default))


def get_float(key, default):
    return float(os.environ.get(key, default))


def get_bool(key, default=False):
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def setup_logging():
    # Windows에서 출력을 파일/파이프로 돌리면 cp949가 되어 이모지·한글 로그가 깨지므로 UTF-8로 고정
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # 로그에 쓰레드 이름이 보이도록 해서 어느 쓰레드가 일하는지 시연 때 확인할 수 있게 한다
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(threadName)-14s] %(message)s",
        datefmt="%H:%M:%S",
    )
