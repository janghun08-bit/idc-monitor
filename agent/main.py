"""에이전트 실행: 프로젝트 루트에서  py -m agent.main"""
import logging
import threading

from agent.actuators import create_actuator
from agent.client import AgentClient
from agent.sensors import KeyboardThread, MockScenario, create_sensors
from common import config, crypto


def main():
    config.load_env()
    config.setup_logging()
    threading.current_thread().name = "Main(conn)"
    crypto.init()

    use_mock = config.get_bool("USE_MOCK", True)
    scenario = MockScenario() if use_mock else None
    client = AgentClient(
        agent_id=config.get("AGENT_ID", "pi-01"),
        host=config.get("SERVER_HOST", "127.0.0.1"),
        port=config.get_int("SERVER_PORT", 9000),
        sensors=create_sensors(use_mock, scenario),
        actuator=create_actuator(use_mock),
    )
    if use_mock:
        KeyboardThread(scenario, client.on_button).start()
    try:
        client.run()
    except KeyboardInterrupt:
        logging.getLogger("agent").info("Ctrl+C → 에이전트 종료")
        client.stop()


if __name__ == "__main__":
    main()
