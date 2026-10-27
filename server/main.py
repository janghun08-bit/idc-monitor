"""서버 실행: 프로젝트 루트에서  py -m server.main"""
import logging
import threading

from common import config
from server.tcp_server import MonitorServer


def main():
    config.load_env()
    config.setup_logging()
    threading.current_thread().name = "Main(accept)"

    server = MonitorServer("0.0.0.0", config.get_int("SERVER_PORT", 9000))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger("server").info("Ctrl+C → 서버 종료")


if __name__ == "__main__":
    main()
