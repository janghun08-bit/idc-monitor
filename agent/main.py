"""에이전트 실행: 프로젝트 루트에서  py -m agent.main"""
import logging
import threading

from agent.client import AgentClient
from agent.sensors import create_sensors
from common import config


def main():
    config.load_env()
    config.setup_logging()
    threading.current_thread().name = "Main(conn)"

    client = AgentClient(
        agent_id=config.get("AGENT_ID", "pi-01"),
        host=config.get("SERVER_HOST", "127.0.0.1"),
        port=config.get_int("SERVER_PORT", 9000),
        sensors=create_sensors(config.get_bool("USE_MOCK", True)),
    )
    try:
        client.run()
    except KeyboardInterrupt:
        logging.getLogger("agent").info("Ctrl+C → 에이전트 종료")
        client.stop()


if __name__ == "__main__":
    main()
