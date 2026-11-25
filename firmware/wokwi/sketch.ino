/*
 * Smart Parking Lot Digital Twin — ESP32 edge node (zone A + entry gate)
 * ----------------------------------------------------------------------
 * Runs unchanged on the Wokwi ESP32 simulator (https://wokwi.com) and on a real board.
 *
 *  Sensors   : 6 x HC-SR04 (shared TRIG, one ECHO pin per slot)
 *              RFID  -> RC522 on real hardware (#define USE_RC522); on Wokwi two push
 *                       buttons simulate a registered card / an unknown card
 *              IR beam under the barrier -> slide switch on Wokwi
 *              Exit-lane IR -> push button on Wokwi
 *  Actuators : SG90 servo barrier, 6 WS2812 slot LEDs, 16x2 I2C LCD
 *  Comms     : Wi-Fi + MQTT (same topics / JSON as simulator/physical_sim.py)
 *
 *  Edge logic: median-of-5 filtering, hysteresis + debounce, sensor-fault detection,
 *              barrier safety interlock (never closes on a car), local counting fallback
 *              when the twin is unreachable.
 *
 * Libraries: PubSubClient, ArduinoJson (v7), ESP32Servo, LiquidCrystal I2C, Adafruit NeoPixel
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_NeoPixel.h>
#include <time.h>
#include <sys/time.h>

// #define USE_RC522          // uncomment on real hardware with an RC522 reader (SPI)
#ifdef USE_RC522
#include <SPI.h>
#include <MFRC522.h>
#define RC522_SS 15
#define RC522_RST 2
MFRC522 rfid(RC522_SS, RC522_RST);
#endif

// ------------------------------------------------------------------ configuration
const char* WIFI_SSID = "Wokwi-GUEST";
const char* WIFI_PASS = "";
const char* MQTT_HOST = "broker.hivemq.com";   // must match MQTT_HOST of the twin engine
const int   MQTT_PORT = 1883;
const char* PREFIX    = "smartparking-mansoura-demo";  // must match TOPIC_PREFIX of the twin engine
const char* ZONE      = "A";

#define N_SLOTS 6
const int TRIG_PIN = 27;
const int ECHO_PINS[N_SLOTS] = {34, 35, 32, 33, 25, 26};
const int SERVO_PIN = 13;
const int PIXEL_PIN = 4;
const int BTN_CARD = 18;      // simulates scanning a registered RFID card
const int BTN_UNKNOWN = 19;   // simulates an unknown card
const int BTN_EXIT = 23;      // exit-lane IR sensor
const int IR_PIN = 5;         // IR beam under the barrier (LOW = blocked)

const float OCC_ON_CM = 100, OCC_OFF_CM = 150;
const int   DEBOUNCE = 2, SAMPLES = 5;
const unsigned long TWIN_TIMEOUT_MS = 15000;

// a few cards from config.REGISTERED_CARDS (the whitelist)
const char* KNOWN_CARDS[] = {"00A61F93", "02F0EE99", "06555096", "1389B372"};

// ------------------------------------------------------------------ globals
WiFiClient net;
PubSubClient mqtt(net);
Servo barrier;
LiquidCrystal_I2C lcd(0x27, 16, 2);
Adafruit_NeoPixel pixels(N_SLOTS, PIXEL_PIN, NEO_GRB + NEO_KHZ800);

struct Slot {
  char id[4];
  float raw = 0, dist = 255;
  int occ = 0, pending = 0;
  bool fault = false;
  String mode = "normal";
} slots[N_SLOTS];

String gateMode = "auto";       // auto | open | closed (remote command)
int   angle = 0, targetAngle = 0;
int   entries = 0, exits = 0, queueLen = 0;
bool  carAuthorised = false, carPassing = false;
String pendingCard = "";
int   twinFree = -1;
String twinRecommended = "";
int   twinEntriesSeen = 0;
unsigned long twinRxMs = 0, lastTel = 0, lastGate = 0, lastSample = 0, lastServo = 0, authorisedAt = 0;
bool  changed = false;
int   cardIdx = 0;
String lcdL1, lcdL2;

String topic(const String& a) { return String(PREFIX) + "/" + a; }

double nowEpoch() {                 // NTP-synchronised UTC epoch (seconds, fractional)
  struct timeval tv; gettimeofday(&tv, nullptr);
  return tv.tv_sec + tv.tv_usec / 1e6;
}
unsigned long long nowMs() { return (unsigned long long)(nowEpoch() * 1000.0); }

// ------------------------------------------------------------------ ultrasonic + filtering
float ping(int echoPin) {
  digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long us = pulseIn(echoPin, HIGH, 30000);   // 0 on timeout
  return us * 0.0343f / 2.0f;
}

float median(float* v, int n) {
  for (int i = 1; i < n; i++) { float k = v[i]; int j = i - 1; while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; } v[j + 1] = k; }
  return (n % 2) ? v[n / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
}

// round-robin: one slot (5 pings) per call so the barrier/servo loop never stalls
void sampleNextSlot() {
  static int s = 0;
  s = (s + 1) % N_SLOTS;
  {
    float valid[SAMPLES]; int nv = 0; float last = 0;
    for (int k = 0; k < SAMPLES; k++) {
      last = ping(ECHO_PINS[s]);
      if (last >= 2 && last <= 400) valid[nv++] = last;
      delay(2);
    }
    slots[s].raw = last;
    if (nv < 3) { if (!slots[s].fault) changed = true; slots[s].fault = true; return; }
    if (slots[s].fault) changed = true;
    slots[s].fault = false;
    float d = median(valid, nv);
    slots[s].dist = d;
    int want = slots[s].occ;
    if (slots[s].occ == 0 && d < OCC_ON_CM) want = 1;
    else if (slots[s].occ == 1 && d > OCC_OFF_CM) want = 0;
    if (want != slots[s].occ) {
      if (++slots[s].pending >= DEBOUNCE) { slots[s].occ = want; slots[s].pending = 0; changed = true; }
    } else slots[s].pending = 0;
  }
}

const char* ledName(const Slot& s) {
  if (s.fault) return "amber_blink";
  if (s.mode == "reserved") return "yellow";
  if (s.mode == "maintenance") return "blue";
  return s.occ ? "red" : "green";
}

void updateLeds() {
  bool blinkOn = (millis() / 400) % 2;
  for (int s = 0; s < N_SLOTS; s++) {
    uint32_t c;
    if (slots[s].fault)                    c = blinkOn ? pixels.Color(255, 140, 0) : 0;
    else if (slots[s].mode == "reserved")  c = pixels.Color(255, 180, 0);
    else if (slots[s].mode == "maintenance") c = pixels.Color(0, 80, 255);
    else                                   c = slots[s].occ ? pixels.Color(255, 0, 0) : pixels.Color(0, 200, 0);
    pixels.setPixelColor(s, c);
  }
  pixels.show();
}

// ------------------------------------------------------------------ MQTT out
void publishTelemetry() {
  JsonDocument doc;
  doc["node"] = String("esp32-zone-") + ZONE;
  doc["zone"] = ZONE;
  doc["ts"] = nowEpoch();
  doc["sent_ms"] = nowMs();
  doc["uptime_s"] = millis() / 1000;
  doc["rssi"] = WiFi.RSSI();
  JsonArray arr = doc["slots"].to<JsonArray>();
  for (int s = 0; s < N_SLOTS; s++) {
    JsonObject o = arr.add<JsonObject>();
    o["id"] = slots[s].id; o["raw"] = round(slots[s].raw * 10) / 10.0; o["dist"] = round(slots[s].dist * 10) / 10.0;
    o["occ"] = slots[s].occ; o["fault"] = slots[s].fault ? 1 : 0; o["mode"] = slots[s].mode; o["led"] = ledName(slots[s]);
  }
  char buf[1400]; size_t n = serializeJson(doc, buf);
  mqtt.publish(topic(String("zone/") + ZONE + "/telemetry").c_str(), (const uint8_t*)buf, n, false);
}

void gateEvent(const char* type, const String& card, const char* reason = nullptr) {
  JsonDocument doc;
  doc["node"] = "esp32-gate"; doc["ts"] = nowEpoch(); doc["type"] = type;
  doc["entries"] = entries; doc["exits"] = exits; doc["sent_ms"] = nowMs();
  if (card.length()) doc["card"] = card;
  if (reason) doc["reason"] = reason;
  if (!strcmp(type, "entry") && twinRecommended.length()) doc["recommended"] = twinRecommended;
  char buf[384]; size_t n = serializeJson(doc, buf);
  mqtt.publish(topic("gate/event").c_str(), (const uint8_t*)buf, n, false);
}

bool twinOnline() { return twinFree >= 0 && millis() - twinRxMs < TWIN_TIMEOUT_MS; }

int freeEstimate() {
  if (twinOnline()) return twinFree - max(0, entries - twinEntriesSeen);
  return N_SLOTS - (entries - exits);          // local fallback (edge autonomy)
}

const char* barrierState() {
  if (angle >= 89) return "open";
  if (angle <= 1) return "closed";
  return targetAngle > angle ? "opening" : "closing";
}

void publishGateState() {
  JsonDocument doc;
  doc["node"] = "esp32-gate"; doc["ts"] = nowEpoch(); doc["sent_ms"] = nowMs();
  doc["mode"] = gateMode; doc["barrier"] = barrierState(); doc["angle"] = angle;
  doc["ir_blocked"] = digitalRead(IR_PIN) == LOW; doc["queue"] = queueLen;
  doc["entries"] = entries; doc["exits"] = exits; doc["twin_online"] = twinOnline();
  JsonArray l = doc["lcd"].to<JsonArray>(); l.add(lcdL1); l.add(lcdL2);
  char buf[384]; size_t n = serializeJson(doc, buf);
  mqtt.publish(topic("gate/state").c_str(), (const uint8_t*)buf, n, false);
}

// ------------------------------------------------------------------ MQTT in
void onMessage(char* t, byte* payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  String tp(t);
  if (tp == topic("twin/availability")) {
    twinFree = doc["free"] | 0;
    twinRecommended = doc["recommended"].isNull() ? String("") : String((const char*)doc["recommended"]);
    twinEntriesSeen = doc["entries_seen"] | entries;
    twinRxMs = millis();
  } else if (tp == topic("gate/cmd")) {
    String m = doc["mode"] | "auto";
    if (m == "auto" || m == "open" || m == "closed") gateMode = m;
  } else if (tp.startsWith(topic("slot/")) && tp.endsWith("/cmd")) {
    String sid = tp.substring(topic("slot/").length(), tp.length() - 4);
    for (int s = 0; s < N_SLOTS; s++) if (sid == slots[s].id) { slots[s].mode = (const char*)(doc["mode"] | "normal"); changed = true; }
  }
}

void ensureMqtt() {
  while (!mqtt.connected()) {
    String cid = "esp32-parking-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    if (mqtt.connect(cid.c_str())) {
      mqtt.subscribe(topic("twin/availability").c_str());
      mqtt.subscribe(topic("gate/cmd").c_str());
      mqtt.subscribe(topic("slot/+/cmd").c_str());
    } else delay(1000);
  }
}

// ------------------------------------------------------------------ gate logic
bool pressed(int pin) {                       // edge-triggered, debounced button
  static unsigned long last[40]; static bool prev[40];
  bool now = digitalRead(pin) == LOW;
  bool fire = now && !prev[pin] && millis() - last[pin] > 200;
  if (now != prev[pin]) last[pin] = millis();
  prev[pin] = now;
  return fire;
}

String readCard() {
#ifdef USE_RC522
  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    char uid[9]; snprintf(uid, 9, "%02X%02X%02X%02X", rfid.uid.uidByte[0], rfid.uid.uidByte[1], rfid.uid.uidByte[2], rfid.uid.uidByte[3]);
    rfid.PICC_HaltA();
    return String(uid);
  }
#endif
  if (pressed(BTN_CARD)) return String(KNOWN_CARDS[cardIdx++ % 4]);
  if (pressed(BTN_UNKNOWN)) return String("DEADBEEF");
  return "";
}

bool isKnown(const String& c) {
  for (auto k : KNOWN_CARDS) if (c == k) return true;
  return false;                                // real deployment: full whitelist / server lookup
}

void gateTick() {
  bool irBlocked = digitalRead(IR_PIN) == LOW;
  int freeNow = freeEstimate();

  String card = readCard();
  if (card.length() && !carAuthorised) {
    if (gateMode == "closed") { gateEvent("rejected", card, "closed"); }
    else if (gateMode == "auto" && !isKnown(card)) { gateEvent("denied", card, "unknown_card"); }
    else if (gateMode == "open" || freeNow > 0) { carAuthorised = true; pendingCard = card; authorisedAt = millis(); targetAngle = 90; }
    else { gateEvent("rejected", card, "full"); }
  }
  // the car passes: IR beam blocked then cleared  (or 6 s timeout on Wokwi if the switch is not used)
  if (carAuthorised && angle >= 89) {
    if (irBlocked) carPassing = true;
    if ((carPassing && !irBlocked) || (!carPassing && millis() - authorisedAt > 6000)) {
      entries++; gateEvent("entry", pendingCard);
      carAuthorised = carPassing = false; pendingCard = "";
    }
  }
  if (pressed(BTN_EXIT)) { exits++; gateEvent("exit", ""); }

  // safety interlock: never lower the arm while the beam is blocked
  if (gateMode == "open") targetAngle = 90;
  else if (!carAuthorised && !irBlocked) targetAngle = 0;

  if (millis() - lastServo > 15) {             // smooth servo motion
    if (angle < targetAngle) angle = min(targetAngle, angle + 2);
    else if (angle > targetAngle) angle = max(targetAngle, angle - 2);
    barrier.write(angle);
    lastServo = millis();
  }

  String l1, l2;
  if (gateMode == "closed") { l1 = "GATE CLOSED"; l2 = "By operator"; }
  else if (freeNow <= 0 && gateMode == "auto") { l1 = "PARKING FULL"; l2 = "Please wait"; }
  else { l1 = "Free: " + String(max(freeNow, 0)) + "/" + String(N_SLOTS); l2 = twinRecommended.length() ? String("Go to ") + twinRecommended : String("Welcome"); }
  if (!twinOnline()) l2 = "Offline mode";
  if (l1 != lcdL1 || l2 != lcdL2) {
    lcdL1 = l1; lcdL2 = l2;
    lcd.clear(); lcd.setCursor(0, 0); lcd.print(l1); lcd.setCursor(0, 1); lcd.print(l2);
  }
}

// ------------------------------------------------------------------ setup / loop
void setup() {
  Serial.begin(115200);
  pinMode(TRIG_PIN, OUTPUT);
  for (int s = 0; s < N_SLOTS; s++) { pinMode(ECHO_PINS[s], INPUT); snprintf(slots[s].id, 4, "%s%d", ZONE, s + 1); }
  pinMode(BTN_CARD, INPUT_PULLUP); pinMode(BTN_UNKNOWN, INPUT_PULLUP); pinMode(BTN_EXIT, INPUT_PULLUP);
  pinMode(IR_PIN, INPUT_PULLUP);
  barrier.attach(SERVO_PIN, 500, 2400); barrier.write(0);
  pixels.begin(); pixels.clear(); pixels.show();
  lcd.init(); lcd.backlight(); lcd.print("Smart Parking"); lcd.setCursor(0, 1); lcd.print("Connecting...");
#ifdef USE_RC522
  SPI.begin(); rfid.PCD_Init();
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);
  while (WiFi.status() != WL_CONNECTED) { delay(250); Serial.print("."); }
  Serial.println("\nWi-Fi connected");
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  for (int i = 0; i < 40 && time(nullptr) < 1700000000; i++) delay(250);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1500);
  mqtt.setCallback(onMessage);
  ensureMqtt();
  Serial.println("MQTT connected");
}

void loop() {
  ensureMqtt();
  mqtt.loop();
  if (millis() - lastSample > 35) { sampleNextSlot(); lastSample = millis(); }
  gateTick();
  updateLeds();
  if (changed || millis() - lastTel > 1000) { publishTelemetry(); lastTel = millis(); changed = false; }
  if (millis() - lastGate > 500) { publishGateState(); lastGate = millis(); }
}
