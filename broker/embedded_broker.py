"""
Embedded MQTT broker (pure Python, amqtt) so the project runs with only `pip install`.
If you already have Mosquitto (or use docker-compose), you can skip this process.

Run:  python -m broker.embedded_broker
"""
import asyncio
import logging

from amqtt.broker import Broker

import config

BROKER_CONFIG = {
    "listeners": {
        "default": {"type": "tcp", "bind": f"0.0.0.0:{config.MQTT_PORT}"},
    },
    "sys_interval": 0,
    "auth": {"allow-anonymous": True},
    "topic-check": {"enabled": False},
}


async def main() -> None:
    broker = Broker(BROKER_CONFIG)
    await broker.start()
    print(f"[broker] MQTT broker listening on 0.0.0.0:{config.MQTT_PORT}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("amqtt").setLevel(logging.ERROR)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
