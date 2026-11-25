"""
Central configuration shared by the simulator, the twin engine and the dashboard API.
Every value can be overridden with an environment variable of the same name.
"""
import datetime as _dt
import os
import random as _random


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------- MQTT
MQTT_HOST = _env("MQTT_HOST", "127.0.0.1")          # use "broker.hivemq.com" for Wokwi mode
MQTT_PORT = int(_env("MQTT_PORT", "1883"))
TOPIC_PREFIX = _env("TOPIC_PREFIX", "smartparking")  # make it unique on public brokers


def topic(*parts: str) -> str:
    return "/".join([TOPIC_PREFIX, *parts])


# ---------------------------------------------------------------- Lot layout
ZONES = [z.strip() for z in _env("LOT_ZONES", "A,B").split(",") if z.strip()]  # one ESP32 per zone
SLOTS_PER_ZONE = int(_env("SLOTS_PER_ZONE", "6"))
CAPACITY = len(ZONES) * SLOTS_PER_ZONE

# ---------------------------------------------------------------- Edge sensing (HC-SR04)
EMPTY_DISTANCE_CM = 255.0      # sensor -> back wall when the slot is empty
OCC_ON_CM = 100.0              # filtered distance below this  => occupied
OCC_OFF_CM = 150.0             # filtered distance above this  => free   (hysteresis band)
DEBOUNCE_SAMPLES = 2           # consecutive filtered readings needed to change state
SAMPLES_PER_READ = 5           # median filter window
SENSOR_MIN_CM, SENSOR_MAX_CM = 2.0, 400.0   # HC-SR04 valid range

# ---------------------------------------------------------------- Simulation clock
SIM_SPEED = float(_env("SIM_SPEED", "60"))          # 60 => 1 real second = 1 simulated minute
SIM_START_HOUR = int(_env("SIM_START_HOUR", "6"))   # simulated day starts at 06:00
HISTORY_DAYS = int(_env("HISTORY_DAYS", "14"))      # synthetic history generated for forecasting


def sim_start_epoch() -> float:
    """Today at SIM_START_HOUR (local time) as epoch seconds."""
    override = os.environ.get("SIM_START_EPOCH")
    if override:
        return float(override)
    today = _dt.datetime.now().replace(hour=SIM_START_HOUR, minute=0, second=0, microsecond=0)
    return today.timestamp()


# ---------------------------------------------------------------- RFID whitelist (RC522 at the gate)
_rng = _random.Random(2026)
REGISTERED_CARDS = sorted({"%08X" % _rng.getrandbits(32) for _ in range(80)})

# ---------------------------------------------------------------- Twin engine / API
API_HOST = _env("API_HOST", "0.0.0.0")
API_PORT = int(_env("API_PORT", "8000"))
DB_PATH = _env("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "parking_twin.db"))
NODE_OFFLINE_AFTER_S = 6.0     # real seconds without telemetry => node offline
TWIN_OFFLINE_AFTER_S = 15.0    # gate falls back to local counting if the twin is silent this long
