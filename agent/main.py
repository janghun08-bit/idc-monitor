"""에이전트 실행: 프로젝트 루트에서  py -m agent.main"""
import logging
import threading

from agent.actuators import GpioButton, create_actuator
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
    button = None
    if use_mock:
        KeyboardThread(scenario, client.on_button).start()
    else:
        button = GpioButton(client.on_button)
    try:
        client.run()
    except KeyboardInterrupt:
        logging.getLogger("agent").info("Ctrl+C → 에이전트 종료")
        client.stop()
    finally:
        # 끝낼 때 부저·LED를 끄고 핀을 돌려놓는다 (안 하면 부저가 계속 울릴 수 있음)
        client.actuator.close()
        if button:
            button.close()


if __name__ == "__main__":
    main()
