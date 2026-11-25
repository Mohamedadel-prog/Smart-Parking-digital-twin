"""
PHYSICAL LAYER SIMULATOR  (replaces the real hardware)
=====================================================

Simulates exactly what the hardware in the catalogue would do and talks the same MQTT
protocol the ESP32 firmware uses (firmware/esp32_parking_node), so the twin engine cannot
tell the difference between this simulator and real boards.

Simulated devices
  * ESP32 zone node per zone  (6 x HC-SR04 ultrasonic sensors + LED indicators)
      - samples each sensor 5 times, median filter, hysteresis + debounce, fault detection
  * ESP32 gate node
      - RC522 RFID reader (whitelist), SG90 servo barrier, IR beam under the barrier,
        exit-lane IR sensor, 16x2 I2C LCD
      - local safety logic: never lowers the barrier while the IR beam is blocked,
        keeps working (local counting) if the twin / network is down
  * Traffic: cars arrive with a time-of-day Poisson process, queue, follow (or ignore)
    the twin's guidance, park for a log-normal duration and leave.

Time: demand and parking durations run on an accelerated clock (SIM_SPEED, default 60x =>
1 real second = 1 simulated minute). Gate mechanics run in real time so the barrier
animation is visible.

Run:  python -m simulator.physical_sim  [--speed 60] [--zones A,B] [--seed 7]
"""
import argparse
import json
import random
import statistics
import time

import paho.mqtt.client as mqtt

import config
from common.layout import build_layout
from common.model import arrival_rate, poisson, sample_patience_min, sample_stay_min

T = config.topic


# ======================================================================= helpers
class SimClock:
    def __init__(self, start_epoch: float, speed: float):
        self.t = start_epoch
        self.speed = speed
        self._last = time.monotonic()
        self.paused = False

    def tick(self):
        now = time.monotonic()
        real_dt = now - self._last
        self._last = now
        sim_dt = 0.0 if self.paused else real_dt * self.speed
        self.t += sim_dt
        return real_dt, sim_dt


class Car:
    _next = 1

    def __init__(self, card: str, rng: random.Random, now: float):
        self.id = Car._next
        Car._next += 1
        self.card = card
        self.state = "queue"            # queue -> entering -> to_slot -> parked -> to_exit -> gone
        self.arrived = now
        self.give_up_at = now + sample_patience_min(rng) * 60
        self.echo_cm = rng.uniform(28, 65)  # distance the ultrasonic sees when this car is parked
        self.follows_guidance = rng.random() < 0.85
        self.target = None
        self.slot = None
        self.timer = 0.0                # generic countdown (sim seconds)
        self.depart_at = None


# ======================================================================= zone node
class ZoneNode:
    """Virtual ESP32 reading 6 HC-SR04 sensors."""

    def __init__(self, zone: str, slots: list, rng: random.Random):
        self.zone = zone
        self.node_id = f"esp32-zone-{zone}"
        self.rng = rng
        self.boot = time.monotonic()
        self.slots = {}
        for s in slots:
            self.slots[s["id"]] = {
                "id": s["id"], "car": None, "mode": "normal", "fault": False,
                "occ": 0, "pending": 0, "raw": config.EMPTY_DISTANCE_CM, "dist": config.EMPTY_DISTANCE_CM,
                "valid_ratio": 1.0, "wall": config.EMPTY_DISTANCE_CM + rng.uniform(-6, 6),
            }
        self.changed = False

    # --- one ultrasonic ping -------------------------------------------------
    def _ping(self, s) -> float:
        if s["fault"]:
            # broken / disconnected sensor: echo timeout (pulseIn returns 0) most of the time
            return 0.0 if self.rng.random() < 0.9 else self.rng.uniform(2, 400)
        true = s["car"].echo_cm if s["car"] else s["wall"]
        if self.rng.random() < 0.03:                       # spurious reflection / missed echo
            return self.rng.choice([0.0, self.rng.uniform(2, 400)])
        return max(0.0, self.rng.gauss(true, 1.5))

    # --- sampling + filtering (this is the edge logic that runs on the ESP32) -
    def sample(self):
        for s in self.slots.values():
            readings = [self._ping(s) for _ in range(config.SAMPLES_PER_READ)]
            valid = [r for r in readings if config.SENSOR_MIN_CM <= r <= config.SENSOR_MAX_CM]
            s["raw"] = round(readings[-1], 1)
            s["valid_ratio"] = len(valid) / len(readings)
            if len(valid) < 3:
                s["sensor_fault"] = True            # keep the last known state, flag the fault
                continue
            s["sensor_fault"] = False
            d = statistics.median(valid)
            s["dist"] = round(d, 1)
            want = s["occ"]
            if s["occ"] == 0 and d < config.OCC_ON_CM:
                want = 1
            elif s["occ"] == 1 and d > config.OCC_OFF_CM:
                want = 0
            if want != s["occ"]:
                s["pending"] += 1
                if s["pending"] >= config.DEBOUNCE_SAMPLES:
                    s["occ"], s["pending"] = want, 0
                    self.changed = True
            else:
                s["pending"] = 0

    def led(self, s) -> str:
        if s.get("sensor_fault"):
            return "amber_blink"
        if s["mode"] == "reserved":
            return "yellow"
        if s["mode"] == "maintenance":
            return "blue"
        return "red" if s["occ"] else "green"

    def telemetry(self, sim_t: float) -> dict:
        return {
            "node": self.node_id, "zone": self.zone, "ts": round(sim_t, 1),
            "sent_ms": int(time.time() * 1000),
            "uptime_s": int(time.monotonic() - self.boot),
            "rssi": int(-48 - self.rng.random() * 12),
            "slots": [{
                "id": s["id"], "raw": s["raw"], "dist": s["dist"], "occ": s["occ"],
                "fault": int(bool(s.get("sensor_fault"))), "mode": s["mode"], "led": self.led(s),
            } for s in self.slots.values()],
        }


# ======================================================================= gate node
class GateNode:
    """Virtual ESP32 with RC522 RFID, SG90 barrier servo, IR beam, exit IR and 16x2 LCD."""
    SERVO_DEG_PER_S = 120.0     # real-time servo speed
    PASS_REAL_S = 1.4           # car needs this long (real time) under the barrier
    HOLD_REAL_S = 0.6

    def __init__(self, capacity: int):
        self.node_id = "esp32-gate"
        self.capacity = capacity
        self.mode = "auto"           # auto | open | closed   (remote command from the twin)
        self.angle = 0.0             # 0 closed, 90 open
        self.target_angle = 0.0
        self.queue: list[Car] = []
        self.current: Car | None = None
        self.pass_left = 0.0
        self.hold_left = 0.0
        self.entries = 0
        self.exits = 0
        self.availability = None      # last message from the twin
        self.avail_rx = 0.0           # monotonic time it arrived
        self.lcd = ["Smart Parking", "Booting..."]
        self.events: list[dict] = []

    # --- availability as seen by the edge (twin + local fallback) --------------
    def twin_online(self) -> bool:
        return self.availability is not None and time.monotonic() - self.avail_rx < config.TWIN_OFFLINE_AFTER_S

    def free_estimate(self) -> int:
        if self.twin_online():
            pending = self.entries - int(self.availability.get("entries_seen", self.entries))
            return int(self.availability.get("free", 0)) - max(0, pending)
        # fallback: local inventory counting (edge autonomy when the network/twin is down)
        return self.capacity - (self.entries - self.exits)

    @property
    def ir_blocked(self) -> bool:
        return self.current is not None and self.pass_left > 0 and self.current.state == "entering"

    @property
    def barrier_state(self) -> str:
        if self.angle >= 89.5:
            return "open"
        if self.angle <= 0.5:
            return "closed"
        return "opening" if self.target_angle > self.angle else "closing"

    def emit(self, etype: str, sim_t: float, car: Car | None = None, **extra):
        ev = {"node": self.node_id, "ts": round(sim_t, 1), "type": etype,
              "entries": self.entries, "exits": self.exits, "sent_ms": int(time.time() * 1000)}
        if car is not None:
            ev["card"] = car.card
            ev["car"] = car.id
        ev.update(extra)
        self.events.append(ev)

    # --- main state machine ---------------------------------------------------
    def tick(self, real_dt: float, sim_t: float, on_entered):
        # servo motion
        step = self.SERVO_DEG_PER_S * real_dt
        if self.angle < self.target_angle:
            self.angle = min(self.target_angle, self.angle + step)
        elif self.angle > self.target_angle:
            self.angle = max(self.target_angle, self.angle - step)

        free = self.free_estimate()

        # drivers waiting at a full / closed gate eventually give up
        for car in list(self.queue):
            if car is not self.current and sim_t > car.give_up_at:
                self.queue.remove(car)
                car.state = "gone"
                self.emit("rejected", sim_t, car, reason="full" if free <= 0 else "closed",
                          waited_min=round((sim_t - car.arrived) / 60, 1))

        # pick the next car at the reader
        if self.current is None and self.queue and self.mode != "closed":
            car = self.queue[0]
            if car.card not in config.REGISTERED_CARDS and self.mode == "auto":
                self.queue.pop(0)
                car.state = "gone"
                self.emit("denied", sim_t, car, reason="unknown_card")
            elif self.mode == "open" or free > 0:
                self.queue.pop(0)
                self.current = car
                car.state = "authorised"
                self.target_angle = 90.0

        # car passes once the barrier is fully open
        if self.current is not None:
            car = self.current
            if car.state == "authorised" and self.angle >= 89.5:
                car.state = "entering"
                self.pass_left = self.PASS_REAL_S
            elif car.state == "entering":
                self.pass_left -= real_dt
                if self.pass_left <= 0:
                    self.entries += 1
                    self.emit("entry", sim_t, car, recommended=(self.availability or {}).get("recommended"))
                    on_entered(car)
                    self.current = None
                    self.hold_left = self.HOLD_REAL_S

        # closing logic (safety: never close on a car)
        if self.current is None:
            if self.hold_left > 0:
                self.hold_left -= real_dt
            elif self.mode == "open":
                self.target_angle = 90.0
            elif not self.ir_blocked:
                next_ok = self.queue and self.mode == "auto" and free > 0
                if not next_ok:
                    self.target_angle = 0.0
        if self.mode == "closed" and self.current is None and not self.ir_blocked:
            self.target_angle = 0.0

        # LCD
        rec = (self.availability or {}).get("recommended")
        if self.mode == "closed":
            self.lcd = ["GATE CLOSED", "By operator"]
        elif free <= 0 and self.mode == "auto":
            self.lcd = ["PARKING FULL", f"Queue: {len(self.queue)}"]
        else:
            self.lcd = [f"Free: {max(free, 0)}/{self.capacity}", f"Go to {rec}" if rec else "Welcome"]
        if not self.twin_online():
            self.lcd[1] = "Offline mode"

    def exit_lane(self, car: Car, sim_t: float):
        """Exit-lane IR sensor detects a leaving vehicle (exit barrier opens freely)."""
        self.exits += 1
        self.emit("exit", sim_t, car)

    def state_msg(self, sim_t: float) -> dict:
        return {
            "node": self.node_id, "ts": round(sim_t, 1), "sent_ms": int(time.time() * 1000),
            "mode": self.mode, "barrier": self.barrier_state, "angle": round(self.angle, 1),
            "ir_blocked": self.ir_blocked, "queue": len(self.queue),
            "entries": self.entries, "exits": self.exits,
            "twin_online": self.twin_online(), "lcd": self.lcd,
        }


# ======================================================================= the world
class ParkingWorld:
    def __init__(self, args):
        self.rng = random.Random(args.seed)
        self.clock = SimClock(config.sim_start_epoch() if args.start is None else args.start, args.speed)
        self.demand = 1.0
        zones = args.zones.split(",") if args.zones else config.ZONES
        layout = build_layout(zones)
        self.capacity = len(layout)
        self.zone_nodes = {z: ZoneNode(z, [s for s in layout if s["zone"] == z], self.rng) for z in zones}
        self.slot_index = {sid: node for node in self.zone_nodes.values() for sid in node.slots}
        self.gate = GateNode(self.capacity)
        self.cars: list[Car] = []
        self.unknown_rate = 0.04

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"phys-sim-{self.rng.randint(0, 99999)}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
        self.client.loop_start()

    # ------------------------------------------------------------------ MQTT in
    def _on_connect(self, client, userdata, flags, rc, props=None):
        print(f"[sim] connected to MQTT {config.MQTT_HOST}:{config.MQTT_PORT} rc={rc}", flush=True)
        client.subscribe([(T("gate", "cmd"), 1), (T("twin", "availability"), 0),
                          (T("slot", "+", "cmd"), 1), (T("sim", "cmd"), 1)])

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode() or "{}")
        except ValueError:
            return
        if msg.topic == T("twin", "availability"):
            self.gate.availability = data
            self.gate.avail_rx = time.monotonic()
        elif msg.topic == T("gate", "cmd"):
            if data.get("mode") in ("auto", "open", "closed"):
                self.gate.mode = data["mode"]
                print(f"[sim] gate mode -> {self.gate.mode}", flush=True)
        elif msg.topic.startswith(T("slot")) and msg.topic.endswith("/cmd"):
            sid = msg.topic.split("/")[-2]
            node = self.slot_index.get(sid)
            if node and data.get("mode") in ("normal", "reserved", "maintenance"):
                node.slots[sid]["mode"] = data["mode"]
                node.changed = True
        elif msg.topic == T("sim", "cmd"):
            self._sim_command(data)

    def _sim_command(self, d: dict):
        if "speed" in d:
            self.clock.speed = max(1.0, min(600.0, float(d["speed"])))
        if "demand" in d:
            self.demand = max(0.0, min(5.0, float(d["demand"])))
        if "pause" in d:
            self.clock.paused = bool(d["pause"])
        if d.get("rush"):
            for _ in range(int(d["rush"])):
                self._spawn_car()
        for key, flag in (("fault", True), ("clear_fault", False)):
            sid = d.get(key)
            if sid and sid in self.slot_index:
                self.slot_index[sid].slots[sid]["fault"] = flag
        print(f"[sim] command {d}", flush=True)

    # ------------------------------------------------------------------ traffic
    def _spawn_car(self):
        if self.rng.random() < self.unknown_rate:
            card = "%08X" % self.rng.getrandbits(32)
        else:
            card = self.rng.choice(config.REGISTERED_CARDS)
        car = Car(card, self.rng, self.clock.t)
        self.cars.append(car)
        self.gate.queue.append(car)
        self.gate.emit("arrival", self.clock.t, car)

    def _slot(self, sid):
        return self.slot_index[sid].slots[sid]

    def _free_physical(self, sid) -> bool:
        s = self._slot(sid)
        return s["car"] is None and s["mode"] == "normal" and not self._targeted(sid)

    def _targeted(self, sid) -> bool:
        return any(c.state == "to_slot" and c.target == sid for c in self.cars)

    def _on_entered(self, car: Car):
        car.state = "to_slot"
        car.timer = self.rng.uniform(40, 110)          # sim seconds to drive to the slot
        rec = (self.gate.availability or {}).get("recommended")
        free = [sid for sid in self.slot_index if self._free_physical(sid)]
        if car.follows_guidance and rec and rec in free:
            car.target = rec
        else:
            car.target = self.rng.choice(free) if free else None

    def _move_cars(self, sim_dt: float, sim_t: float):
        for car in self.cars:
            if car.state == "to_slot":
                car.timer -= sim_dt
                if car.timer <= 0:
                    target = car.target
                    if target is None or self._slot(target)["car"] is not None or self._slot(target)["mode"] != "normal":
                        free = [sid for sid in self.slot_index
                                if self._slot(sid)["car"] is None and self._slot(sid)["mode"] == "normal"]
                        target = self.rng.choice(free) if free else None
                    if target is None:               # nowhere to park -> drive out
                        car.state, car.timer = "to_exit", self.rng.uniform(40, 90)
                        continue
                    car.slot = target
                    self._slot(target)["car"] = car
                    car.state = "parked"
                    car.depart_at = sim_t + sample_stay_min(sim_t, self.rng) * 60
            elif car.state == "parked" and sim_t >= car.depart_at:
                self._slot(car.slot)["car"] = None
                car.state, car.timer = "to_exit", self.rng.uniform(40, 90)
            elif car.state == "to_exit":
                car.timer -= sim_dt
                if car.timer <= 0:
                    car.state = "gone"
                    self.gate.exit_lane(car, sim_t)
        self.cars = [c for c in self.cars if c.state != "gone" or c in self.gate.queue]

    # ------------------------------------------------------------------ loop
    def run(self):
        last_tel = last_gate = last_truth = 0.0
        last_gate_sig = None
        print(f"[sim] running: capacity={self.capacity}, speed={self.clock.speed}x, "
              f"start={time.strftime('%Y-%m-%d %H:%M', time.localtime(self.clock.t))}", flush=True)
        while True:
            real_dt, sim_dt = self.clock.tick()
            t = self.clock.t
            # arrivals (Poisson, time-of-day dependent)
            lam = arrival_rate(t, self.capacity, self.demand) * sim_dt / 3600.0
            for _ in range(poisson(lam, self.rng)):
                self._spawn_car()
            self._move_cars(sim_dt, t)
            self.gate.tick(real_dt, t, self._on_entered)

            now = time.monotonic()
            # zone nodes sample at 5 Hz and publish at 1 Hz or immediately on change
            for node in self.zone_nodes.values():
                node.sample()
            if now - last_tel >= 1.0 or any(n.changed for n in self.zone_nodes.values()):
                for node in self.zone_nodes.values():
                    self.client.publish(T("zone", node.zone, "telemetry"), json.dumps(node.telemetry(t)), qos=0)
                    node.changed = False
                last_tel = now

            for ev in self.gate.events:
                self.client.publish(T("gate", "event"), json.dumps(ev), qos=1)
            self.gate.events.clear()

            sig = (self.gate.barrier_state, round(self.gate.angle), len(self.gate.queue), self.gate.mode, tuple(self.gate.lcd))
            if sig != last_gate_sig or now - last_gate >= 1.0:
                self.client.publish(T("gate", "state"), json.dumps(self.gate.state_msg(t)), qos=0)
                last_gate, last_gate_sig = now, sig

            if now - last_truth >= 1.0:
                # ground truth: only exists because this is a simulation -> used to validate the twin
                truth = {sid: int(self._slot(sid)["car"] is not None) for sid in self.slot_index}
                inside = sum(1 for c in self.cars if c.state in ("to_slot", "parked", "to_exit"))
                self.client.publish(T("sim", "truth"), json.dumps({
                    "ts": round(t, 1), "slots": truth, "cars_inside": inside,
                    "speed": self.clock.speed, "demand": self.demand, "paused": self.clock.paused,
                    "queue": len(self.gate.queue),
                }))
                last_truth = now
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description="Smart parking physical-layer simulator")
    ap.add_argument("--speed", type=float, default=config.SIM_SPEED)
    ap.add_argument("--zones", type=str, default=None, help="e.g. A,B (default from config)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", type=float, default=None, help="simulated start epoch")
    args = ap.parse_args()
    world = ParkingWorld(args)
    try:
        world.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
