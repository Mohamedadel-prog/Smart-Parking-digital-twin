"""
DIGITAL TWIN ENGINE
===================

  MQTT  (physical layer: real ESP32s or simulator/physical_sim.py)
     |  zone/<Z>/telemetry   gate/event   gate/state   sim/truth
     v
  TwinState  -- mirrors every slot, the barrier and the queue; fuses the gate counter with
     |           the slot sensors; computes availability + driver guidance; logs events;
     |           aggregates hourly occupancy; raises alerts; validates itself against
     |           ground truth (simulation only)
     |  twin/availability (retained)  gate/cmd  slot/<id>/cmd  sim/cmd
     v
  FastAPI  REST + WebSocket  ->  dashboard/ (2D live map, forecast, what-if, commands)

Run:  python -m twin.engine        (then open http://localhost:8000)
"""
import asyncio
import collections
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from common.layout import build_layout
from twin import forecasting, history, whatif
from twin.storage import Storage

T = config.topic
HOUR = 3600


def hour_of(ts):
    return int(ts // HOUR * HOUR)


def day_of(ts):
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


# ======================================================================= hourly aggregation
class HourTracker:
    """Time-weighted occupancy integral per (simulated) hour -> hourly table."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.hour = None
        self.last_ts = None
        self.last_occ = 0
        self.integral = 0.0
        self.covered_from = None
        self.peak = 0
        self.entries = self.exits = self.rejected = 0

    def count(self, etype):
        if etype == "entry":
            self.entries += 1
        elif etype == "exit":
            self.exits += 1
        elif etype == "rejected":
            self.rejected += 1

    def advance(self, ts, occ, storage: Storage, capacity: int):
        if self.hour is None:
            self.hour, self.last_ts, self.last_occ, self.covered_from, self.peak = hour_of(ts), ts, occ, ts, occ
            return
        if ts < self.last_ts:
            return
        while hour_of(ts) > self.hour:
            end = self.hour + HOUR
            self.integral += self.last_occ * (end - self.last_ts)
            covered = end - self.covered_from
            if covered >= 600:  # store only if we observed at least 10 simulated minutes
                storage.upsert_hour(self.hour, self.integral / covered, self.peak, self.entries, self.exits,
                                    self.rejected, capacity, "live")
            self.hour, self.last_ts, self.covered_from = end, end, end
            self.integral, self.peak = 0.0, self.last_occ
            self.entries = self.exits = self.rejected = 0
        self.integral += self.last_occ * (ts - self.last_ts)
        self.last_ts, self.last_occ = ts, occ
        self.peak = max(self.peak, occ)

    def partial(self):
        if self.hour is None or self.last_ts is None:
            return None
        covered = self.last_ts - self.covered_from
        return self.integral / covered if covered > 0 else float(self.last_occ)


# ======================================================================= twin state
class TwinState:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.lock = threading.RLock()
        self.layout = build_layout()
        self.capacity = len(self.layout)
        self.slots = {}
        for s in self.layout:
            self.slots[s["id"]] = dict(s, occ=None, status="offline", dist=None, raw=None, fault=False,
                                       mode="normal", led=None, since=None, last_rx=0.0)
        self.gate = {"mode": "auto", "barrier": "closed", "angle": 0, "queue": 0, "ir_blocked": False,
                     "lcd": ["", ""], "twin_online": False, "entries": 0, "exits": 0, "last_rx": 0.0}
        self.nodes = {}
        self.now_ts = config.sim_start_epoch()
        self.gate_in = 0                 # cumulative counters reported by the gate node
        self.gate_out = 0
        self.entries_seen = 0
        self.assigned = {}               # slot -> sim ts when a driver was sent there
        self.recommended = None
        self.availability = {}
        self.day = None
        self.today = collections.Counter()
        self.stays_today = []
        self.hour = HourTracker()
        self.alerts = {}
        self.recent = collections.deque(maxlen=40)
        self.sync = {"agree": 0, "total": 0, "latency_ms": collections.deque(maxlen=200),
                     "inside_err": collections.deque(maxlen=200)}
        self.sim_info = {}
        self.version = 0
        self.forecast_cache = None
        self.client = None

    # ------------------------------------------------------------------ helpers
    def _log(self, etype, ts=None, slot=None, card=None, detail=None):
        ts = self.now_ts if ts is None else ts
        self.storage.add_event(ts, etype, slot, card, json.dumps(detail) if isinstance(detail, dict) else detail)
        self.recent.appendleft({"ts": ts, "type": etype, "slot": slot, "card": card, "detail": detail})

    def _set_time(self, ts):
        if ts is None:
            return
        if ts < self.now_ts - 2 * HOUR:
            # the physical side restarted its clock -> start a new session
            self.hour.reset()
            self.entries_seen = 0
            self.today.clear()
            self.stays_today.clear()
        self.now_ts = ts
        d = day_of(ts)
        if d != self.day:
            first = self.day is None
            self.day = d
            self.today.clear()
            self.stays_today.clear()
            if first:   # twin (re)started mid-day: restore today's counters from the event log
                for et in ("entry", "exit", "rejected", "denied"):
                    self.today[et] = self.storage.count_events(et, d, ts)

    def _gate_counters(self, msg):
        if "entries" in msg and "exits" in msg:
            self.gate_in, self.gate_out = int(msg["entries"]), int(msg["exits"])
            self.entries_seen = self.gate_in

    def inside(self):
        return max(0, self.gate_in - self.gate_out)

    def occupied_count(self):
        return sum(1 for s in self.slots.values() if s["occ"] == 1)

    def _alert(self, key, active, level="warning", msg=""):
        if active and key not in self.alerts:
            self.alerts[key] = {"key": key, "level": level, "msg": msg, "ts": self.now_ts}
            self._log("alert", detail={"key": key, "level": level, "msg": msg})
        elif active:
            self.alerts[key]["msg"] = msg
        elif not active and key in self.alerts:
            del self.alerts[key]
            self._log("alert_cleared", detail={"key": key})

    # ------------------------------------------------------------------ inbound
    def on_telemetry(self, data):
        rx = time.time()
        with self.lock:
            self._set_time(data.get("ts"))
            node = data.get("node", "zone-" + data.get("zone", "?"))
            lat = rx * 1000 - data["sent_ms"] if "sent_ms" in data else None
            if lat is not None and lat >= 0:
                self.sync["latency_ms"].append(lat)
            self.nodes[node] = {"node": node, "kind": "zone", "zone": data.get("zone"), "last_rx": rx,
                                "uptime_s": data.get("uptime_s"), "rssi": data.get("rssi"),
                                "latency_ms": None if lat is None else round(lat, 1)}
            for s in data.get("slots", []):
                slot = self.slots.get(s["id"])
                if slot is None:
                    continue
                slot["last_rx"] = rx
                slot["raw"], slot["dist"], slot["led"] = s.get("raw"), s.get("dist"), s.get("led")
                slot["fault"] = bool(s.get("fault"))
                if s.get("mode") and s["mode"] != slot["mode"]:
                    self._log("slot_mode", slot=slot["id"], detail=s["mode"])
                    slot["mode"] = s["mode"]
                occ = int(s.get("occ", 0))
                if slot["occ"] is not None and occ != slot["occ"]:
                    if occ == 1:
                        self._log("slot_occupied", slot=slot["id"])
                        self.assigned.pop(slot["id"], None)
                    else:
                        if slot["since"] is not None:
                            self.stays_today.append((self.now_ts - slot["since"]) / 60)
                        self._log("slot_freed", slot=slot["id"],
                                  detail={"stay_min": round((self.now_ts - (slot["since"] or self.now_ts)) / 60, 1)})
                if slot["occ"] != occ:
                    slot["since"] = self.now_ts
                slot["occ"] = occ
                self._alert(f"fault:{slot['id']}", slot["fault"], "danger",
                            f"Sensor fault on slot {slot['id']} (no valid echo)")
            self.hour.advance(self.now_ts, self.occupied_count(), self.storage, self.capacity)
            self._recompute()

    def on_gate_event(self, ev):
        with self.lock:
            self._set_time(ev.get("ts"))
            et = ev.get("type")
            self._gate_counters(ev)
            if et == "entry":
                rec = ev.get("recommended") or self.recommended
                if rec:
                    self.assigned[rec] = self.now_ts
            if et in ("entry", "exit", "rejected", "denied", "arrival"):
                self.today[et] += 1
                self.hour.count(et)
            if et != "arrival":
                detail = {k: v for k, v in ev.items() if k not in ("node", "ts", "type", "card", "sent_ms")}
                self._log(et, card=ev.get("card"), detail=detail)
            self._recompute()

    def on_gate_state(self, st):
        with self.lock:
            self._set_time(st.get("ts"))
            rx = time.time()
            self.gate.update({k: st[k] for k in ("mode", "barrier", "angle", "queue", "ir_blocked", "lcd",
                                                 "twin_online", "entries", "exits") if k in st})
            self.gate["last_rx"] = rx
            self._gate_counters(st)
            self.nodes[st.get("node", "esp32-gate")] = {"node": st.get("node", "esp32-gate"), "kind": "gate",
                                                        "last_rx": rx, "latency_ms": None}
            self._alert("queue", st.get("queue", 0) >= 3, "warning", f"{st.get('queue')} cars queued at the gate")
            self.version += 1

    def on_truth(self, tr):
        """Ground truth from the simulator -> twin fidelity metrics (impossible with real hardware)."""
        with self.lock:
            self.sim_info = {k: tr.get(k) for k in ("speed", "demand", "paused", "queue")}
            for sid, true_occ in tr.get("slots", {}).items():
                s = self.slots.get(sid)
                if s is None or s["occ"] is None or s["fault"]:
                    continue
                self.sync["total"] += 1
                self.sync["agree"] += int(s["occ"] == true_occ)
            if "cars_inside" in tr:
                self.sync["inside_err"].append(abs(self.inside() - tr["cars_inside"]))

    # ------------------------------------------------------------------ availability / guidance
    def _recompute(self):
        rx = time.time()
        for s in self.slots.values():
            online = rx - s["last_rx"] < config.NODE_OFFLINE_AFTER_S
            if not online:
                s["status"] = "offline"
            elif s["fault"]:
                s["status"] = "fault"
            elif s["mode"] != "normal":
                s["status"] = s["mode"]
            else:
                s["status"] = "occupied" if s["occ"] else "free"
        occupied = self.occupied_count()
        # sensor fusion: vehicles inside (gate counters) minus vehicles seen in slots. Faulty slots
        # are assumed occupied (conservative) so a broken sensor never creates phantom free space.
        seen = sum(1 for s in self.slots.values() if s["occ"] == 1 or s["status"] == "fault")
        moving = max(0, self.inside() - seen)
        free_slots = sorted((s for s in self.slots.values() if s["status"] == "free"),
                            key=lambda s: s["gate_distance_m"])
        for sid, t0 in list(self.assigned.items()):
            if self.now_ts - t0 > 6 * 60 or self.slots[sid]["status"] != "free":
                del self.assigned[sid]
        free = max(0, len(free_slots) - moving)
        candidates = [s for s in free_slots if s["id"] not in self.assigned]
        self.recommended = (candidates or free_slots or [None])[0]
        self.recommended = self.recommended["id"] if self.recommended else None
        if free == 0:
            self.recommended = None
        usable = sum(1 for s in self.slots.values() if s["status"] in ("free", "occupied"))
        self.availability = {"ts": round(self.now_ts, 1), "free": free, "total": self.capacity,
                             "usable": usable, "occupied": occupied, "moving": moving,
                             "recommended": self.recommended, "full": free == 0,
                             "entries_seen": self.entries_seen}
        self._alert("full", free == 0 and any(s["status"] != "offline" for s in self.slots.values()),
                    "warning", "Lot is full - barrier holds new arrivals")
        for node, n in self.nodes.items():
            self._alert(f"offline:{node}", rx - n["last_rx"] > config.NODE_OFFLINE_AFTER_S, "danger",
                        f"Node {node} offline")
        self.version += 1

    def publish_availability(self):
        if self.client is not None:
            with self.lock:
                payload = json.dumps(self.availability)
            self.client.publish(T("twin", "availability"), payload, qos=0, retain=True)

    # ------------------------------------------------------------------ outbound commands
    def command_gate(self, mode):
        self.client.publish(T("gate", "cmd"), json.dumps({"mode": mode, "by": "twin"}), qos=1, retain=True)
        with self.lock:
            self._log("command", detail={"target": "gate", "mode": mode})

    def command_slot(self, sid, mode):
        self.client.publish(T("slot", sid, "cmd"), json.dumps({"mode": mode, "by": "twin"}), qos=1, retain=True)
        with self.lock:
            self._log("command", slot=sid, detail={"target": "slot", "mode": mode})

    def command_sim(self, payload):
        self.client.publish(T("sim", "cmd"), json.dumps(payload), qos=1)
        with self.lock:
            self._log("command", detail={"target": "simulator", **payload})

    # ------------------------------------------------------------------ snapshot for UI
    def snapshot(self):
        with self.lock:
            rx = time.time()
            occ = self.occupied_count()
            stays = self.stays_today
            lat = list(self.sync["latency_ms"])
            ierr = list(self.sync["inside_err"])
            fc = self.forecast_cache
            return {
                "now_ts": self.now_ts,
                "capacity": self.capacity,
                "slots": [{k: s[k] for k in ("id", "zone", "index", "row", "status", "occ", "mode", "dist", "raw",
                                             "fault", "since", "led", "gate_distance_m")} for s in self.slots.values()],
                "gate": {k: v for k, v in self.gate.items() if k != "last_rx"} |
                        {"online": rx - self.gate["last_rx"] < config.NODE_OFFLINE_AFTER_S},
                "availability": self.availability,
                "kpi": {
                    "occupied": occ, "free": self.availability.get("free", 0),
                    "usable": self.availability.get("usable", 0), "moving": self.availability.get("moving", 0),
                    "occupancy_pct": round(100 * occ / self.capacity, 1), "inside": self.inside(),
                    "entries_today": self.today["entry"], "exits_today": self.today["exit"],
                    "rejected_today": self.today["rejected"], "denied_today": self.today["denied"],
                    "arrivals_today": self.today["arrival"],
                    "avg_stay_min": round(sum(stays) / len(stays), 1) if stays else None,
                },
                "nodes": [{**n, "online": rx - n["last_rx"] < config.NODE_OFFLINE_AFTER_S,
                           "age_s": round(rx - n["last_rx"], 1)} for n in self.nodes.values()],
                "alerts": sorted(self.alerts.values(), key=lambda a: a["level"] != "danger"),
                "events": list(self.recent)[:15],
                "fidelity": {
                    "state_agreement_pct": round(100 * self.sync["agree"] / self.sync["total"], 2)
                    if self.sync["total"] else None,
                    "samples": self.sync["total"],
                    "latency_ms_avg": round(sum(lat) / len(lat), 1) if lat else None,
                    "latency_ms_max": round(max(lat), 1) if lat else None,
                    "inside_count_mae": round(sum(ierr) / len(ierr), 2) if ierr else None,
                },
                "sim": self.sim_info,
                "forecast_hint": None if not fc else {"peak": fc.get("peak"), "full_soon": fc.get("full_soon")},
            }

    def forecast(self, horizon=12, method="hybrid"):
        with self.lock:
            now = self.now_ts
            partial = self.hour.partial()
        rows = self.storage.hourly(since=now - 16 * 86400, until=hour_of(now))
        return forecasting.build_forecast(rows, now, self.capacity, horizon, method, partial)


# ======================================================================= MQTT wiring
def start_mqtt(state: TwinState):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"twin-engine-{int(time.time()) % 100000}")
    state.client = client

    def on_connect(c, u, flags, rc, props=None):
        print(f"[twin] connected to MQTT {config.MQTT_HOST}:{config.MQTT_PORT} rc={rc}", flush=True)
        c.subscribe([(T("zone", "+", "telemetry"), 0), (T("gate", "event"), 1),
                     (T("gate", "state"), 0), (T("sim", "truth"), 0)])

    def on_message(c, u, msg):
        try:
            data = json.loads(msg.payload.decode() or "{}")
        except ValueError:
            return
        t = msg.topic
        if t.endswith("/telemetry"):
            state.on_telemetry(data)
        elif t == T("gate", "event"):
            state.on_gate_event(data)
        elif t == T("gate", "state"):
            state.on_gate_state(data)
        elif t == T("sim", "truth"):
            state.on_truth(data)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(1, 5)
    client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


# ======================================================================= FastAPI
storage = Storage(config.DB_PATH)
state = TwinState(storage)
websockets: set = set()


async def _background():
    last_avail = None
    last_pub = 0.0
    last_fc = 0.0
    last_sent_version = -1
    while True:
        await asyncio.sleep(0.25)
        now = time.monotonic()
        with state.lock:
            state._recompute() if now - last_pub > 1.0 else None
            av = dict(state.availability)
        sig = {k: v for k, v in av.items() if k != "ts"}
        if sig != last_avail or now - last_pub > 2.0:
            state.publish_availability()
            last_avail, last_pub = sig, now
        if now - last_fc > 5.0:
            try:
                fc = await asyncio.to_thread(state.forecast)
                with state.lock:
                    state.forecast_cache = fc
                    fs = fc.get("full_soon")
                    state._alert("forecast_full", fs is not None, "info",
                                 "Forecast: lot expected >=90% full at "
                                 + time.strftime("%H:%M", time.localtime(fs["ts"])) if fs else "")
            except Exception as e:  # noqa: BLE001
                print("[twin] forecast error:", e, flush=True)
            last_fc = now
        if websockets and state.version != last_sent_version:
            last_sent_version = state.version
            msg = json.dumps({"type": "snapshot", "data": state.snapshot()})
            for ws in list(websockets):
                try:
                    await ws.send_text(msg)
                except Exception:  # noqa: BLE001
                    websockets.discard(ws)


@asynccontextmanager
async def lifespan(app):
    n = history.ensure_history(storage)
    if n:
        print(f"[twin] generated {n} hours of bootstrap history for forecasting", flush=True)
    start_mqtt(state)
    task = asyncio.create_task(_background())
    print(f"[twin] dashboard on http://localhost:{config.API_PORT}", flush=True)
    yield
    task.cancel()
    state.client.loop_stop()


app = FastAPI(title="Smart Parking Lot Digital Twin", version="1.0", lifespan=lifespan)


class GateCmd(BaseModel):
    mode: str = Field(pattern="^(auto|open|closed)$")


class SlotCmd(BaseModel):
    mode: str = Field(pattern="^(normal|reserved|maintenance)$")


class SimCmd(BaseModel):
    speed: float | None = None
    demand: float | None = None
    rush: int | None = None
    fault: str | None = None
    clear_fault: str | None = None
    pause: bool | None = None


class WhatIf(BaseModel):
    demand: float = 1.0
    closed_slots: int = 0
    stay_mult: float = 1.0
    hours: int = 12


@app.get("/api/state")
def api_state():
    return state.snapshot()


@app.get("/api/slots")
def api_slots():
    return state.snapshot()["slots"]


@app.get("/api/slots/{sid}")
def api_slot(sid: str):
    for s in state.snapshot()["slots"]:
        if s["id"] == sid:
            return s
    raise HTTPException(404, "unknown slot")


@app.get("/api/events")
def api_events(limit: int = 50, type: str | None = None):
    return storage.events(limit=min(limit, 500), etype=type)


@app.get("/api/history")
def api_history(hours: int = 48):
    return storage.hourly(since=state.now_ts - hours * HOUR)


@app.get("/api/forecast")
def api_forecast(horizon: int = 12, method: str = "hybrid"):
    if method not in ("hybrid", "seasonal", "ma", "prophet"):
        raise HTTPException(400, "method must be hybrid|seasonal|ma|prophet")
    return state.forecast(max(1, min(24, horizon)), method)


@app.get("/api/forecast/compare")
def api_forecast_compare():
    """Backtest accuracy of every method (for the Results chapter)."""
    return {m: state.forecast(12, m)["metrics"] for m in ("ma", "seasonal", "hybrid")}


@app.post("/api/gate")
def api_gate(cmd: GateCmd):
    state.command_gate(cmd.mode)
    return {"ok": True, "mode": cmd.mode}


@app.post("/api/slots/{sid}/mode")
def api_slot_mode(sid: str, cmd: SlotCmd):
    if sid not in state.slots:
        raise HTTPException(404, "unknown slot")
    state.command_slot(sid, cmd.mode)
    return {"ok": True}


@app.post("/api/sim")
def api_sim(cmd: SimCmd):
    payload = {k: v for k, v in cmd.model_dump().items() if v is not None}
    state.command_sim(payload)
    return {"ok": True, "sent": payload}


@app.post("/api/whatif")
def api_whatif(p: WhatIf):
    with state.lock:
        now, occ = state.now_ts, state.occupied_count()
    return whatif.run_whatif(now, occ, state.capacity, p.demand, p.closed_slots, p.stay_mult, p.hours)


@app.get("/api/summary")
def api_summary():
    """Daily KPIs from the event log (useful for the report's Demo Results)."""
    snap = state.snapshot()
    day = day_of(state.now_ts)
    rows = storage.hourly(since=day, until=day + 86400)
    return {"date": time.strftime("%Y-%m-%d", time.localtime(day)), "kpi": snap["kpi"],
            "fidelity": snap["fidelity"], "hours": rows}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    websockets.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": state.snapshot()}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        websockets.discard(ws)


@app.exception_handler(Exception)
async def _err(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=500)


DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
app.mount("/", StaticFiles(directory=str(DASHBOARD), html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="warning")
