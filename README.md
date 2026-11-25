# Smart Parking Lot Digital Twin (Simulation-based)

**Project 5 — Domain: Smart City — Level: Intermediate**
Digital Twin لجراج فيه 12 مكان (Zone A + Zone B، كل Zone عليها ESP32). الـ **Physical layer كله Simulation** بدل الهاردوير، وبيكلم الـ Twin بنفس بروتوكول الـ MQTT اللي الـ ESP32 الحقيقي هيستخدمه. يعني لو جبت هاردوير بعدين، الـ Twin والـ Dashboard مش هيتغيروا خالص.

```
┌──────────────── Physical layer (SIMULATED) ────────────────┐
│ simulator/physical_sim.py           firmware/ (Wokwi ESP32) │
│  • 2× ESP32 zone node: 6× HC-SR04 each (noise, outliers)   │
│  • ESP32 gate node: RC522, SG90 barrier, IR, 16×2 LCD      │
│  • traffic: Poisson arrivals, queue, guidance, stays       │
└───────────────┬────────────────────────────────▲───────────┘
   MQTT  zone/+/telemetry · gate/event · gate/state   twin/availability · gate/cmd · slot/+/cmd
┌───────────────▼────────────────────────────────┴───────────┐
│ Twin engine  (twin/engine.py)                               │
│  state mirror · sensor fusion · guidance · SQLite log ·     │
│  hourly aggregation · forecasting · what-if · alerts        │
└───────────────┬────────────────────────────────────────────┘
        REST + WebSocket (FastAPI)
┌───────────────▼────────────────────────────────────────────┐
│ Dashboard (dashboard/) – live 2D map, forecast vs actual,  │
│ what-if, barrier & slot commands, event log, fidelity      │
└────────────────────────────────────────────────────────────┘
```

## تشغيل سريع (Windows / Linux / Mac)

محتاج Python 3.10 أو أحدث.

```bash
cd smart-parking-digital-twin
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
python run_all.py
```

هيفتح لوحده `http://localhost:8000`. الأمر ده بيشغّل 3 processes:

| Process | Command | الدور |
|---|---|---|
| MQTT broker | `python -m broker.embedded_broker` | Broker مكتوب بـ Python (مش محتاج تسطّب Mosquitto) |
| Twin engine | `python -m twin.engine` | الـ Twin + REST API + WebSocket + Dashboard |
| Physical simulator | `python -m simulator.physical_sim` | بيعمل نفس شغل الـ ESP32s والـ sensors والعربيات |

Options مفيدة:

```bash
python run_all.py --speed 120      # الوقت أسرع (60 = كل ثانية حقيقية = دقيقة)
python run_all.py --reset          # يمسح الـ database ويعمل history جديد
python run_all.py --no-broker      # لو شغال Mosquitto (docker compose up -d)
python run_all.py --no-sim         # لو الـ physical layer جاي من Wokwi
```

الـ simulated day بيبدأ الساعة 06:00 علشان تشوف الـ morning rush والجراج بيتملي والبوابة بتقفل. أول مرة الـ Twin بيعمل 14 يوم **synthetic history** بنفس الـ demand model علشان الـ forecasting يشتغل من أول دقيقة، وبعد كده الداتا الـ live بتتسجل فوقيه.

## الـ Objectives اتعملت إزاي

| Objective (من الكتالوج) | التنفيذ |
|---|---|
| Detect vehicle presence in at least 6 slots | 12 slot. كل HC-SR04 بيتعمله 5 pings ثم **median filter** ثم **hysteresis** (<100 cm مشغول، >150 cm فاضي) ثم **debounce** (قراءتين). لو الـ echo مش valid في أغلب العينات بيطلع **sensor fault** |
| Live 2D map with colour-coded slots | SVG map: فاضي / مشغول / Reserved / Maintenance / Sensor fault / Offline، والعربيات والـ queue والـ barrier بيتحركوا live |
| Guide drivers to free slots | الـ Twin بيختار أقرب slot فاضي من البوابة (ومش بيبعت عربيتين لنفس المكان) وبيبعته للبوابة، فيظهر على الـ LCD «Go to A3» ومسار متحرك على الخريطة |
| Log entries/exits | SQLite: جدول `events` (entry / exit / denied / rejected / slot changes / commands / alerts) |
| Forecast occupancy per hour | Moving average، Seasonal moving average، Hybrid (seasonal + level correction)، و Prophet (اختياري). فيه backtest بيحسب MAE / RMSE |
| Control entry barrier automatically | البوابة بتفتح بس لو الكارت مسجل **و** فيه مكان فاضي. لو الجراج اتملى بتقفل والـ LCD يكتب PARKING FULL. وفيه **safety interlock**: الـ barrier مستحيل ينزل والـ IR beam مقطوع |

## Digital Twin features زيادة (بتفرق في الـ Demo)

- **Sensor fusion**: عدد العربيات جوه (gate counters) − العربيات اللي الـ sensors شايفاها = العربيات اللي لسه بتتحرك جوه. بالطريقة دي الـ free count مبيكدبش.
- **Edge autonomy**: لو الـ Twin أو النت وقع، البوابة بتكمل بـ local counting (LCD: Offline mode).
- **What-if simulation**: Monte-Carlo (40 run) بيبدأ من الحالة الحالية للجراج. تقدر تجرب مثلاً demand +50%، أو قفل 2 slots، أو parking time أقل، وتقارن بالـ baseline.
- **Remote commands**: Force open / close / auto للبوابة، و Reserve / Maintenance لأي slot.
- **Twin fidelity** (ميزة الـ simulation): الـ simulator بيبعت ground truth، فالـ Twin بيقيس دقته: state agreement ‎%، و MQTT latency، والـ error في عدد العربيات جوه.
- **Fault injection**: من الـ Dashboard تقدر تبوّظ sensor وتشوف الـ alert والـ LED بيرمش.

## سيناريو الـ Demo (5 دقايق)

1. شغّل `python run_all.py --reset` والساعة 06:00. الجراج فاضي والـ guidance بيشاور على A1.
2. استنى لحد 08:00–09:00 (حوالي 3 دقايق على 60×). العربيات بتدخل والـ LCD بيقول لكل سواق يروح فين.
3. لما الجراج يتملى: البوابة بتقفل لوحدها، الـ LCD يكتب **PARKING FULL**، الـ queue تزيد، والعربيات اللي زهقت بتتسجل `rejected`. ده الـ Expected Demo Result: *gate automatically closes when full*.
4. Forecast chart: الـ actual قصاد الـ predicted، والـ MAE، ووقت الـ peak المتوقع.
5. What-if: خلي الـ demand 150% واقفل 2 slots واعمل Run. هيوريك كام عربية هترجع وكام ساعة الجراج هيبقى full.
6. دوس على slot واعمل *Inject sensor fault* هيطلع alert، أو *Maintenance* والـ slot هيخرج من الـ free count.
7. اعمل *Force close* للبوابة وبعدين *Auto*، والـ command هيتسجل في الـ log.

## REST API

Swagger على `http://localhost:8000/docs`.

| Method | Endpoint | الوصف |
|---|---|---|
| GET | `/api/state` | Snapshot كامل (slots, gate, KPIs, alerts, fidelity) |
| GET | `/api/slots`, `/api/slots/{id}` | حالة الـ slots |
| GET | `/api/events?limit=&type=` | Entry/exit log |
| GET | `/api/history?hours=48` | Hourly occupancy |
| GET | `/api/forecast?method=hybrid&horizon=12` | Forecast + backtest + metrics |
| GET | `/api/forecast/compare` | مقارنة دقة كل الـ methods (جدول جاهز للتقرير) |
| GET | `/api/summary` | KPIs اليوم |
| POST | `/api/gate` `{"mode":"auto\|open\|closed"}` | Remote barrier command |
| POST | `/api/slots/{id}/mode` `{"mode":"normal\|reserved\|maintenance"}` | Slot command |
| POST | `/api/whatif` `{"demand":1.5,"closed_slots":2,"stay_mult":1,"hours":12}` | What-if |
| POST | `/api/sim` `{"speed":120,"demand":1.3,"rush":6,"fault":"A3","clear_fault":"A3","pause":true}` | التحكم في الـ simulator |
| WS | `/ws` | Live snapshots |

## Wokwi mode (ESP32 firmware فعلي على simulator)

الـ firmware في `firmware/esp32_parking_node/` (نسخة جاهزة لـ Wokwi في `firmware/wokwi/`). ده اللي يتحط في فصل **Codes** في التقرير.

1. ادخل https://wokwi.com واعمل New project، واختار ESP32.
2. حط `sketch.ino` و `diagram.json` و `libraries.txt` من `firmware/wokwi/`.
3. غيّر `PREFIX` في الـ sketch لاسم unique، مثلاً `smartparking-mohamed`.
4. على جهازك شغّل الـ Twin على نفس الـ public broker مع Zone A بس:
   ```bash
   # Windows PowerShell
   $env:MQTT_HOST="broker.hivemq.com"; $env:TOPIC_PREFIX="smartparking-mohamed"; $env:LOT_ZONES="A"
   python run_all.py --no-broker --no-sim
   ```
5. في Wokwi: غيّر الـ distance بتاع أي HC-SR04 (دوس عليه وحرك الـ slider)، وهتلاقي الـ slot اتغير على الـ Dashboard. زرار **Scan registered card** بيفتح البوابة، و **Exit-lane IR** بيسجل خروج، والـ slide switch بيمثل IR beam تحت البوابة.

> Wokwi مفيهوش RC522، علشان كده الكارت متمثل بزراير. على الهاردوير الحقيقي فعّل `#define USE_RC522`.
> الـ firmware ما اتعملهوش compile هنا. جرّبه في Wokwi الأول، ولو طلع compile error ابعتهولي.

## Configuration (`config.py` أو environment variables)

| Variable | Default | معناه |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | الـ Broker |
| `TOPIC_PREFIX` | `smartparking` | أول جزء في كل topic |
| `LOT_ZONES` | `A,B` | Zones (كل zone = ESP32 + 6 sensors) |
| `SIM_SPEED` | `60` | سرعة الوقت |
| `SIM_START_HOUR` | `6` | اليوم بيبدأ الساعة كام |
| `HISTORY_DAYS` | `14` | أيام الـ synthetic history |

## هيكل المشروع

```
config.py                    إعدادات مشتركة
run_all.py                   تشغيل كل حاجة بأمر واحد
broker/embedded_broker.py    MQTT broker (amqtt)
common/model.py              demand model (arrivals/stays) – مشترك بين الـ sim والـ history والـ what-if
common/layout.py             geometry بتاع الـ slots
simulator/physical_sim.py    Physical + edge layer simulation
twin/engine.py               Twin engine + FastAPI
twin/forecasting.py          MA / seasonal / hybrid / Prophet + backtest
twin/whatif.py               Monte-Carlo what-if
twin/history.py              synthetic history bootstrap
twin/storage.py              SQLite
dashboard/                   HTML/CSS/JS (من غير CDN، يشتغل offline)
firmware/                    ESP32 Arduino firmware + Wokwi diagram
docs/architecture.md         تفاصيل للتقرير (topics، payloads، algorithms)
```
