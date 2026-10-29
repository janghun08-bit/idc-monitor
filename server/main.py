"""서버 실행: 프로젝트 루트에서  py -m server.main"""
import logging
import threading

from common import config
from server.alarm import AlarmEngine
from server.db import Database
from server.notifier import SlackNotifier
from server.tcp_server import MonitorServer
from server.watchdog import Watchdog


def main():
    config.load_env()
    config.setup_logging()
    threading.current_thread().name = "Main(accept)"
    log = logging.getLogger("server")

    engine = AlarmEngine.from_config()
    log.info("판정 기준: %s", engine.describe())
    db = Database.from_config()
    db.check()
    notifier = SlackNotifier.from_config()
    notifier.start()

    server = MonitorServer("0.0.0.0", config.get_int("SERVER_PORT", 9000), engine, db, notifier)
    Watchdog(server, timeout=config.get_float("OFFLINE_SEC", 15)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Ctrl+C → 서버 종료")


if __name__ == "__main__":
    main()
