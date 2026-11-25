# Smart Parking Lot Digital Twin — Technical Notes (for the report)

## 1. Why simulation instead of hardware

The physical layer is replaced by a software model that follows the same interface contract
(MQTT topics + JSON payloads) as the ESP32 firmware. The twin engine and dashboard are
therefore hardware-agnostic: the same code runs against (a) the Python physical simulator,
(b) the ESP32 firmware running in the Wokwi simulator, or (c) real boards. A simulation also
provides something hardware cannot: **ground truth**, which is used to measure twin fidelity.

## 2. Five-layer architecture mapping

| Layer (catalogue) | Real hardware | This project |
|---|---|---|
| Physical | HC-SR04 per slot, RC522 at gate, SG90 barrier, LEDs, 16×2 LCD | `simulator/physical_sim.py` — sensor physics (noise σ=1.5 cm, 3 % outliers, echo timeouts), vehicles, barrier dynamics |
| Edge | ESP32 per zone: sampling, filtering, local safety | `ZoneNode` / `GateNode` classes, identical logic to `firmware/esp32_parking_node.ino` |
| Communication | Wi-Fi, MQTT, REST | MQTT (amqtt / Mosquitto / HiveMQ), FastAPI REST, WebSocket |
| Twin model | 2D occupancy map + time-series forecasting | `twin/engine.py` state mirror, `twin/forecasting.py`, `twin/whatif.py` |
| Application | Web dashboard / Node-RED | `dashboard/` (HTML/JS, mobile-friendly), live map, what-if, remote commands |

## 3. MQTT contract (`<prefix>` = TOPIC_PREFIX)

| Topic | Direction | QoS / retain | Payload |
|---|---|---|---|
| `<prefix>/zone/<Z>/telemetry` | edge → twin | 0 | `{"node","zone","ts","sent_ms","uptime_s","rssi","slots":[{"id","raw","dist","occ","fault","mode","led"}]}` |
| `<prefix>/gate/event` | edge → twin | 1 | `{"node","ts","type":"arrival|entry|exit|denied|rejected","card","entries","exits","recommended","reason"}` |
| `<prefix>/gate/state` | edge → twin | 0 | `{"mode","barrier","angle","ir_blocked","queue","entries","exits","twin_online","lcd":[l1,l2]}` |
| `<prefix>/twin/availability` | twin → edge | 0 / retained | `{"free","total","usable","occupied","moving","recommended","full","entries_seen"}` |
| `<prefix>/gate/cmd` | twin → edge | 1 / retained | `{"mode":"auto|open|closed"}` |
| `<prefix>/slot/<id>/cmd` | twin → edge | 1 / retained | `{"mode":"normal|reserved|maintenance"}` |
| `<prefix>/sim/cmd` | twin → simulator | 1 | `{"speed","demand","rush","fault","clear_fault","pause"}` |
| `<prefix>/sim/truth` | simulator → twin | 0 | ground-truth occupancy for validation |

Telemetry is published at 1 Hz **and** immediately on any state change (event-driven + heartbeat).

## 4. Algorithms

**Presence detection (edge).** Five pings per slot → discard out-of-range readings (HC-SR04
valid range 2–400 cm) → median → hysteresis (occupied below 100 cm, free above 150 cm) →
debounce (2 consecutive confirmations). If fewer than 3 of the 5 readings are valid, the slot is
flagged `fault` and keeps its last known state.

**Barrier control (edge, with twin input).** Auto mode opens only when the card is whitelisted
**and** `free > 0`. `free` comes from the twin's retained availability message, corrected by
entries the twin has not yet acknowledged (`entries − entries_seen`). If the twin is silent for
15 s the gate switches to local inventory counting (`capacity − (entries − exits)`). Safety
interlock: the arm is never lowered while the IR beam is blocked.

**Sensor fusion (twin).** `inside = gate entries − gate exits`; `moving = inside − slots seen
occupied` (faulty slots counted as occupied, conservatively); `free = free slots − moving`. This
avoids over-admitting cars that have passed the gate but have not yet parked.

**Guidance (twin).** The nearest free slot (by distance from the gate) that has not already
been assigned to a car still driving in. The assignment is released once a slot becomes occupied,
or after 6 simulated minutes.

**Hourly aggregation.** Time-weighted occupancy integral per simulated hour → `hourly` table
(average, peak, entries, exits, rejected).

**Forecasting.** Let *y(h)* be the average occupied slots in hour *h*.
- Moving average: ŷ = mean(y(h−1..h−3)).
- Seasonal moving average: ŷ(h) = mean of y at the same hour on the last 7 days of the same type
  (weekday vs Egyptian weekend Fri/Sat).
- Hybrid: ŷ(h+i) = seasonal(h+i) + c·0.7^i, where c blends the residual of the current partial
  hour (60 %) with the mean residual of the last 3 hours (40 %).
- Prophet: optional (`pip install prophet pandas`).
Accuracy is reported from a rolling one-step-ahead backtest over the last 24 h (MAE, RMSE,
error as % of capacity). `/api/forecast/compare` returns all three methods side by side.

**What-if.** 40 Monte-Carlo runs of the minute-step `LotModel` starting from the current
occupancy and time of day, baseline vs scenario (demand ×k, closed slots, stay ×k). Outputs the
mean / p10 / p90 occupancy per hour, expected turned-away cars and hours at full.

## 5. Demand model (`common/model.py`)

- Arrivals: non-homogeneous Poisson, hourly profile (morning peak 07–09, lunch, evening),
  ×0.55 on Fri/Sat.
- Parking duration: log-normal, mean 240 min for commuters (06–10), 100 min midday, 110 min evening.
- Patience at a full gate: U(3, 12) min. 4 % of cards are unknown (access denied).
- 85 % of drivers follow the guidance; the rest choose a random free slot.

## 6. Results to report (collect from the running system)

| Metric | Where |
|---|---|
| State agreement with ground truth (%) | Dashboard → Twin fidelity, `/api/state` → `fidelity` |
| MQTT sync latency avg / max (ms) | same |
| Vehicles-inside error (MAE) | same |
| Forecast MAE / RMSE per method | `/api/forecast/compare` |
| Entries, exits, rejected, denied, avg stay | `/api/summary` |
| Gate closes when full | Event log: `rejected` with reason `full`, LCD "PARKING FULL" |
| What-if comparison | Dashboard → What-if panel (screenshot) |

Values from one test run at 120× (they vary from run to run): state agreement ≈ 99.9 %, mean MQTT
latency ≈ 4 ms on localhost, vehicles-inside MAE = 0, hybrid forecast MAE ≈ 1.5–1.7 slots
(≈ 13 % of capacity) vs. 2.6 for a plain moving average.

## 7. Report chapter mapping

- **Hardware Design** → describe the *virtual* hardware: component table, Wokwi wiring
  (`firmware/wokwi/diagram.json`), pin map below, sensor model parameters.
- **Modelling** → sections 4–5 of this file.
- **Codes** → `firmware/esp32_parking_node.ino` + `twin/engine.py` (+ forecasting/whatif).
- **Demo Results** → section 6 + dashboard screenshots.

### ESP32 pin map (Wokwi / real board)

| Function | Pin |
|---|---|
| HC-SR04 TRIG (shared) | GPIO27 |
| HC-SR04 ECHO A1..A6 | GPIO34, 35, 32, 33, 25, 26 |
| SG90 servo | GPIO13 |
| WS2812 LEDs (6, chained) | GPIO4 |
| LCD I2C SDA / SCL | GPIO21 / GPIO22 |
| Registered card button / unknown card / exit IR | GPIO18 / 19 / 23 |
| Barrier IR beam (switch) | GPIO5 |
| RC522 (real HW, SPI) | SS 15, RST 2, SCK 18*, MISO 19*, MOSI 23* (*move the buttons when using RC522) |
