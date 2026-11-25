/* Smart Parking Lot Digital Twin — dashboard client */
const $ = s => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const COLORS = { free: "#22c55e", occupied: "#ef4444", reserved: "#f59e0b", maintenance: "#3b82f6",
                 fault: "#d946ef", offline: "#475569" };
const LED = { green: "#22c55e", red: "#ef4444", yellow: "#f59e0b", blue: "#3b82f6", amber_blink: "#f59e0b" };
const CAR_COLORS = ["#e2e8f0", "#60a5fa", "#f87171", "#fbbf24", "#a78bfa", "#34d399", "#94a3b8", "#fb923c"];

let S = null;              // latest snapshot
let selected = null;       // selected slot id
let mapBuilt = false;
const refs = {};           // svg element references
let lastFc = 0;

const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const fmtHour = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const api = (path, body) => fetch(path, body === undefined ? {} : {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then(r => r.json());

function svg(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function text(parent, x, y, str, attrs = {}) {
  const t = svg("text", { x, y, fill: "#8696b8", "font-size": 12, "font-family": "ui-monospace,monospace", ...attrs }, parent);
  t.textContent = str;
  return t;
}

/* ===================================================================== MAP */
const G = { X0: 215, XEND: 985, ROAD_T: 212, ROAD_M: 268, ROAD_B: 325, A_Y: 30, B_Y: 337, H: 170 };

function slotGeom(s, perZone) {
  const gap = 8;
  const w = (G.XEND - G.X0 - gap * (perZone - 1)) / perZone;
  const x = G.X0 + s.index * (w + gap);
  const y = s.row === 0 ? G.A_Y : G.B_Y;
  return { x, y, w, h: G.H, cx: x + w / 2, cy: y + G.H / 2, top: s.row === 0 };
}

function carShape(parent, cx, cy, w, h, color, vertical = true) {
  const g = svg("g", {}, parent);
  const W = vertical ? w : h, H = vertical ? h : w;
  svg("rect", { x: cx - W / 2 + 3, y: cy - H / 2 + 4, width: W, height: H, rx: 12, fill: "#000", opacity: .35 }, g);
  svg("rect", { x: cx - W / 2, y: cy - H / 2, width: W, height: H, rx: vertical ? 14 : 10, fill: color }, g);
  if (vertical) {
    svg("rect", { x: cx - W / 2 + 7, y: cy - H / 2 + 18, width: W - 14, height: H * .22, rx: 5, fill: "#0f172a", opacity: .8 }, g);
    svg("rect", { x: cx - W / 2 + 7, y: cy + H * .18, width: W - 14, height: H * .16, rx: 5, fill: "#0f172a", opacity: .7 }, g);
    svg("rect", { x: cx - W / 2 + 9, y: cy - H * .06, width: W - 18, height: H * .22, rx: 4, fill: "#fff", opacity: .12 }, g);
  } else {
    svg("rect", { x: cx + W * .12, y: cy - H / 2 + 4, width: W * .2, height: H - 8, rx: 4, fill: "#0f172a", opacity: .8 }, g);
    svg("rect", { x: cx - W * .36, y: cy - H / 2 + 4, width: W * .16, height: H - 8, rx: 4, fill: "#0f172a", opacity: .7 }, g);
  }
  return g;
}

function buildMap(snap) {
  const m = $("#map");
  m.innerHTML = "";
  const defs = svg("defs", {}, m);
  const pat = svg("pattern", { id: "stripes", width: 10, height: 10, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
  svg("rect", { width: 10, height: 10, fill: "#2a0b2f" }, pat);
  svg("rect", { width: 4, height: 10, fill: "#d946ef", opacity: .55 }, pat);

  svg("rect", { x: 0, y: 0, width: 1000, height: 540, fill: "#0d1527" }, m);
  // road
  svg("rect", { x: 0, y: G.ROAD_T, width: 1000, height: G.ROAD_B - G.ROAD_T, fill: "#1a2338" }, m);
  svg("line", { x1: 205, x2: 1000, y1: G.ROAD_M, y2: G.ROAD_M, stroke: "#e2e8f0", "stroke-opacity": .35, "stroke-dasharray": "18 14", "stroke-width": 2 }, m);
  svg("line", { x1: 0, x2: 1000, y1: G.ROAD_T, y2: G.ROAD_T, stroke: "#334155", "stroke-width": 2 }, m);
  svg("line", { x1: 0, x2: 1000, y1: G.ROAD_B, y2: G.ROAD_B, stroke: "#334155", "stroke-width": 2 }, m);
  for (const x of [320, 560, 800]) {
    text(m, x, 246, "→ IN", { fill: "#e2e8f0", opacity: .25, "font-size": 14 });
    text(m, x, 303, "OUT ←", { fill: "#e2e8f0", opacity: .25, "font-size": 14 });
  }
  // gate panel
  svg("rect", { x: 12, y: 22, width: 178, height: 178, rx: 10, fill: "#0b1326", stroke: "#1f2b46" }, m);
  text(m, 24, 44, "ENTRY GATE NODE", { fill: "#22d3ee", "font-size": 11, "font-weight": 700 });
  text(m, 24, 60, "RC522 · SG90 · IR · LCD", { "font-size": 10 });
  svg("rect", { x: 22, y: 72, width: 158, height: 52, rx: 4, fill: "#0b3d1f", stroke: "#14532d", "stroke-width": 2 }, m);
  refs.lcd1 = text(m, 30, 93, "", { fill: "#86efac", "font-size": 13 });
  refs.lcd2 = text(m, 30, 114, "", { fill: "#86efac", "font-size": 13 });
  refs.gateLine1 = text(m, 24, 146, "", { fill: "#c7d2e8", "font-size": 11 });
  refs.gateLine2 = text(m, 24, 164, "", { fill: "#c7d2e8", "font-size": 11 });
  refs.gateLine3 = text(m, 24, 182, "", { fill: "#c7d2e8", "font-size": 11 });
  // legend panel bottom-left
  svg("rect", { x: 12, y: 337, width: 178, height: 170, rx: 10, fill: "#0b1326", stroke: "#1f2b46" }, m);
  text(m, 24, 360, "DRIVER GUIDANCE", { fill: "#22d3ee", "font-size": 11, "font-weight": 700 });
  refs.guideBig = text(m, 24, 408, "--", { fill: "#e6edf7", "font-size": 40, "font-weight": 700 });
  refs.guideSub = text(m, 24, 432, "", { "font-size": 11 });
  refs.movingTxt = text(m, 24, 470, "", { fill: "#c7d2e8", "font-size": 11 });
  refs.queueTxt = text(m, 24, 490, "", { fill: "#c7d2e8", "font-size": 11 });

  // barrier + IR + exit
  refs.ir = svg("line", { x1: 204, x2: 204, y1: G.ROAD_T + 3, y2: G.ROAD_M - 3, "stroke-width": 3, "stroke-dasharray": "4 4" }, m);
  svg("rect", { x: 176, y: G.ROAD_T - 12, width: 18, height: 16, rx: 3, fill: "#334155", stroke: "#64748b" }, m);
  refs.armR = svg("line", { x1: 185, x2: 185, y1: G.ROAD_T, y2: G.ROAD_M - 4, stroke: "#ef4444", "stroke-width": 7, "stroke-linecap": "round" }, m);
  refs.armW = svg("line", { x1: 185, x2: 185, y1: G.ROAD_T, y2: G.ROAD_M - 4, stroke: "#fff", "stroke-width": 7, "stroke-dasharray": "7 7" }, m);
  svg("rect", { x: 176, y: G.ROAD_B - 4, width: 18, height: 16, rx: 3, fill: "#334155", stroke: "#64748b" }, m);
  svg("line", { x1: 185, x2: 185, y1: G.ROAD_B, y2: G.ROAD_B - 14, stroke: "#94a3b8", "stroke-width": 5, "stroke-linecap": "round" }, m);
  text(m, 120, G.ROAD_B - 8, "EXIT IR", { "font-size": 10 });
  refs.queueG = svg("g", {}, m);
  refs.passG = svg("g", {}, m);

  // guidance path (under slots' labels but above road)
  refs.guidePath = svg("path", { fill: "none", stroke: "#22d3ee", "stroke-width": 3, "stroke-dasharray": "10 10", class: "guide-path", opacity: 0 }, m);

  // zones
  const perZone = Math.max(...snap.slots.map(s => s.index)) + 1;
  const zones = [...new Set(snap.slots.map(s => s.zone))];
  zones.forEach((z, i) => text(m, G.X0, i === 0 ? 22 : 528, `ZONE ${z} · esp32-zone-${z} · ${perZone}× HC-SR04`, { "font-size": 10.5 }));

  refs.slots = {};
  for (const s of snap.slots) {
    const g = slotGeom(s, perZone);
    const grp = svg("g", { class: "slot", "data-id": s.id }, m);
    grp.addEventListener("click", () => { selected = s.id; renderSlotCard(); renderMap(); });
    const bg = svg("rect", { class: "slot-bg", x: g.x, y: g.y, width: g.w, height: g.h, rx: 8, "stroke-width": 2 }, grp);
    const ring = svg("rect", { x: g.x - 5, y: g.y - 5, width: g.w + 10, height: g.h + 10, rx: 11, fill: "none",
      stroke: "#22d3ee", "stroke-width": 3, class: "guide-ring", opacity: 0 }, grp);
    const carG = svg("g", {}, grp);
    // sensor on the back wall
    const sy = g.top ? g.y + 6 : g.y + g.h - 14;
    svg("rect", { x: g.cx - 13, y: sy, width: 26, height: 8, rx: 2, fill: "#1e3a5f", stroke: "#3b82f6" }, grp);
    svg("circle", { cx: g.cx - 6, cy: sy + 4, r: 2.6, fill: "#94a3b8" }, grp);
    svg("circle", { cx: g.cx + 6, cy: sy + 4, r: 2.6, fill: "#94a3b8" }, grp);
    const ping = svg("circle", { cx: g.cx, cy: sy + 4 + (g.top ? 18 : -18), r: 14, fill: "none", stroke: "#22d3ee",
      "stroke-width": 1.5, class: "ping" }, grp);
    const led = svg("circle", { cx: g.x + g.w - 12, cy: sy + 4, r: 5, stroke: "#0b1326", "stroke-width": 2 }, grp);
    const labelY = g.top ? g.y + g.h - 12 : g.y + 22;
    const label = text(grp, g.x + 9, labelY, s.id, { fill: "#e6edf7", "font-size": 15, "font-weight": 700 });
    const dist = text(grp, g.x + g.w - 9, labelY, "", { "text-anchor": "end", "font-size": 11 });
    const centerBg = svg("rect", { x: g.x + 6, y: g.cy - 11, width: g.w - 12, height: 22, rx: 6, fill: "#0b1326", opacity: 0 }, grp);
    const center = text(grp, g.cx, g.cy + 4, "", { "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#e6edf7" });
    refs.slots[s.id] = { grp, bg, ring, carG, ping, led, label, dist, center, centerBg, g, lastCar: null };
  }
  mapBuilt = true;
}

function hashColor(str) {
  let h = 0;
  for (const c of str) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return CAR_COLORS[h % CAR_COLORS.length];
}

function renderMap() {
  if (!S) return;
  if (!mapBuilt) buildMap(S);
  const rec = S.availability?.recommended;
  for (const s of S.slots) {
    const r = refs.slots[s.id];
    if (!r) continue;
    const col = COLORS[s.status] || COLORS.offline;
    r.bg.setAttribute("fill", s.status === "fault" ? "url(#stripes)" : col);
    r.bg.setAttribute("fill-opacity", s.status === "fault" ? 1 : (s.status === "offline" ? .12 : .16));
    r.bg.setAttribute("stroke", col);
    r.grp.classList.toggle("sel", s.id === selected);
    r.ring.setAttribute("opacity", s.id === rec ? 1 : 0);
    r.ping.style.display = s.status === "offline" ? "none" : "";
    r.led.setAttribute("fill", LED[s.led] || "#334155");
    r.led.classList.toggle("blink", s.led === "amber_blink");
    r.dist.textContent = s.dist != null ? `${Math.round(s.dist)} cm` : "";
    r.center.textContent = { reserved: "RESERVED", maintenance: "MAINTENANCE", fault: "SENSOR FAULT", offline: "OFFLINE" }[s.status] || "";
    // car
    const carKey = s.occ ? `${s.id}:${s.since}` : null;
    if (carKey !== r.lastCar) {
      r.carG.innerHTML = "";
      if (s.occ) carShape(r.carG, r.g.cx, r.g.cy + (r.g.top ? -4 : 4), Math.min(70, r.g.w * .58), 112, hashColor(carKey));
      r.lastCar = carKey;
    }
    r.centerBg.setAttribute("opacity", r.center.textContent ? .88 : 0);
    r.grp.appendChild(r.centerBg); r.grp.appendChild(r.center);   // keep label above the car
  }
  // guidance path
  if (rec && refs.slots[rec]) {
    const g = refs.slots[rec].g;
    const laneY = 240;
    const endY = g.top ? g.y + g.h - 30 : g.y + 30;
    refs.guidePath.setAttribute("d", `M214,${laneY} L${g.cx},${laneY} L${g.cx},${endY}`);
    refs.guidePath.setAttribute("opacity", .9);
  } else refs.guidePath.setAttribute("opacity", 0);

  // gate
  const gt = S.gate || {};
  const ang = (gt.angle || 0) * Math.PI / 180;
  const len = (G.ROAD_M - 4 - G.ROAD_T) * Math.cos(ang);
  for (const a of [refs.armR, refs.armW]) a.setAttribute("y2", G.ROAD_T + Math.max(4, len));
  refs.ir.setAttribute("stroke", gt.ir_blocked ? "#ef4444" : "#22c55e");
  refs.ir.setAttribute("stroke-opacity", gt.ir_blocked ? .95 : .45);
  refs.lcd1.textContent = (gt.lcd?.[0] || "").padEnd(16).slice(0, 16);
  refs.lcd2.textContent = (gt.lcd?.[1] || "").padEnd(16).slice(0, 16);
  refs.gateLine1.textContent = `Barrier: ${gt.barrier || "?"} (${Math.round(gt.angle || 0)}°)`;
  refs.gateLine2.textContent = `Mode: ${(gt.mode || "?").toUpperCase()}  IR: ${gt.ir_blocked ? "BLOCKED" : "clear"}`;
  refs.gateLine3.textContent = `Edge↔twin: ${gt.twin_online ? "synced" : "OFFLINE MODE"}`;

  refs.queueG.innerHTML = "";
  const q = gt.queue || 0;
  for (let i = 0; i < Math.min(q, 3); i++) carShape(refs.queueG, 145 - i * 52, 240, 24, 44, CAR_COLORS[(i * 3 + 1) % 8], false);
  if (q > 3) text(refs.queueG, 4, 262, `+${q - 3}`, { fill: "#fbbf24", "font-size": 13, "font-weight": 700 });
  refs.passG.innerHTML = "";
  if (gt.ir_blocked) carShape(refs.passG, 214, 240, 24, 44, "#e2e8f0", false);

  const av = S.availability || {};
  refs.guideBig.textContent = rec || (av.full ? "FULL" : "--");
  refs.guideBig.setAttribute("fill", rec ? "#22d3ee" : "#ef4444");
  refs.guideSub.textContent = rec ? `nearest free · ${S.slots.find(s => s.id === rec)?.gate_distance_m} m` : "no slot available";
  refs.movingTxt.textContent = `Moving inside: ${av.moving ?? 0} vehicle(s)`;
  refs.queueTxt.textContent = `Waiting at gate: ${q}`;
}

/* ===================================================================== PANELS */
function kpi(label, value, sub = "", cls = "", bar = null) {
  return `<div class="kpi ${cls}"><div class="l">${label}</div><div class="v">${value}</div>
    ${bar != null ? `<div class="bar"><b style="width:${bar}%"></b></div>` : `<div class="s">${sub}</div>`}</div>`;
}

function renderKpis() {
  const k = S.kpi;
  $("#kpis").innerHTML = [
    kpi("Free slots", k.free, `of ${k.usable} usable`, "free"),
    kpi("Occupied", k.occupied, `of ${S.capacity}`, "occ"),
    kpi("Occupancy", k.occupancy_pct + "%", "", "acc", k.occupancy_pct),
    kpi("Moving inside", k.moving, `${k.inside} vehicles inside`),
    kpi("Entries today", k.entries_today, `${k.arrivals_today} arrivals`),
    kpi("Exits today", k.exits_today, "exit-lane IR"),
    kpi("Turned away", k.rejected_today + k.denied_today, `${k.rejected_today} full · ${k.denied_today} bad RFID`),
    kpi("Avg stay", k.avg_stay_min != null ? Math.round(k.avg_stay_min) + "m" : "—", "completed stays today"),
  ].join("");
}

function renderHeader() {
  const d = new Date(S.now_ts * 1000);
  $("#clockTime").textContent = d.toLocaleTimeString([], { hour12: false });
  const sp = S.sim?.speed;
  $("#clockDate").textContent = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) +
    (sp ? ` · ${sp}× ${S.sim.paused ? "(paused)" : ""}` : "");
  const gate = S.nodes.find(n => n.kind === "gate");
  const zones = S.nodes.filter(n => n.kind === "zone");
  const expected = new Set(S.slots.map(s => s.zone)).size;
  setPill("#pillGate", gate?.online);
  const zon = zones.filter(z => z.online).length;
  setPill("#pillZones", zon === expected, `Zone nodes ${zon}/${expected}`);
  $("#btnPause").textContent = S.sim?.paused ? "Resume" : "Pause";
  const sel = $("#simSpeed");
  if (sp && document.activeElement !== sel && [...sel.options].some(o => +o.value === sp)) sel.value = String(sp);
  const dem = $("#simDemand");
  if (S.sim?.demand != null && document.activeElement !== dem) {
    dem.value = Math.round(S.sim.demand * 100); $("#demandOut").textContent = dem.value + "%";
  }
}
function setPill(sel, ok, label) {
  const p = $(sel);
  p.classList.toggle("ok", !!ok); p.classList.toggle("bad", !ok);
  if (label) p.lastChild.textContent = label;
}

function renderGate() {
  const g = S.gate;
  document.querySelectorAll("#gateSeg button").forEach(b => b.classList.toggle("active", b.dataset.mode === g.mode));
  const badge = $("#gateBadge");
  badge.textContent = g.barrier || "?";
  badge.className = "tag " + (g.barrier === "open" ? "on" : g.barrier === "closed" ? "bad" : "warn");
  $("#gateKv").innerHTML = `
    <dt>Barrier angle</dt><dd>${Math.round(g.angle || 0)}°</dd>
    <dt>IR beam (safety)</dt><dd>${g.ir_blocked ? "blocked" : "clear"}</dd>
    <dt>Queue</dt><dd>${g.queue ?? 0}</dd>
    <dt>Gate counters</dt><dd>${g.entries ?? 0} in / ${g.exits ?? 0} out</dd>
    <dt>Edge ↔ twin</dt><dd>${g.twin_online ? "synced" : "local fallback"}</dd>
    <dt>LCD</dt><dd>${(g.lcd || []).join(" | ")}</dd>`;
}

function renderSlotCard() {
  if (!S || !selected) return;
  const s = S.slots.find(x => x.id === selected);
  if (!s) return;
  $("#slotTitle").textContent = s.id;
  const b = $("#slotBadge");
  b.textContent = s.status;
  b.className = "tag " + ({ free: "on", occupied: "bad", fault: "bad" }[s.status] || "warn");
  const since = s.since != null ? Math.round((S.now_ts - s.since) / 60) + " min" : "—";
  $("#slotKv").innerHTML = `
    <dt>Zone / node</dt><dd>${s.zone} · esp32-zone-${s.zone}</dd>
    <dt>Raw echo</dt><dd>${s.raw ?? "—"} cm</dd>
    <dt>Filtered (median 5)</dt><dd>${s.dist ?? "—"} cm</dd>
    <dt>Thresholds</dt><dd>&lt;100 occ · &gt;150 free</dd>
    <dt>${s.occ ? "Occupied for" : "Free for"}</dt><dd>${since}</dd>
    <dt>LED</dt><dd>${s.led ?? "—"}</dd>
    <dt>Distance to gate</dt><dd>${s.gate_distance_m} m</dd>`;
  document.querySelectorAll("#slotSeg button").forEach(btn => btn.classList.toggle("active", btn.dataset.mode === s.mode));
}

function renderAlerts() {
  const a = S.alerts || [];
  $("#alertCount").textContent = a.length;
  $("#alertCount").className = "tag " + (a.some(x => x.level === "danger") ? "bad" : a.length ? "warn" : "on");
  $("#alerts").innerHTML = a.length ? a.map(x =>
    `<li class="${x.level}">${x.msg}<small>${fmtTime(x.ts)} · ${x.key}</small></li>`).join("")
    : `<li class="none">All systems nominal</li>`;
}

function eventDetail(e) {
  const d = e.detail || {};
  switch (e.type) {
    case "entry": return `card ${e.card}${d.recommended ? ` → guided to <b>${d.recommended}</b>` : ""}`;
    case "exit": return `card ${e.card}`;
    case "rejected": return `lot ${d.reason}, waited ${d.waited_min} min`;
    case "denied": return `card ${e.card} not registered`;
    case "slot_occupied": return `slot <b>${e.slot}</b>`;
    case "slot_freed": return `slot <b>${e.slot}</b>${d.stay_min != null ? ` · stayed ${d.stay_min} min` : ""}`;
    case "command": return d.target === "simulator"
      ? "simulator → " + Object.entries(d).filter(([k]) => k !== "target").map(([k, v]) => `${k}=${v}`).join(", ")
      : `${d.target}${e.slot ? " " + e.slot : ""} → ${d.mode}`;
    case "alert": return d.msg || "";
    case "alert_cleared": return d.key || "";
    case "slot_mode": return `${e.slot} → ${typeof d === "string" ? d : ""}`;
    default: return "";
  }
}
function renderEvents() {
  $("#events").innerHTML = (S.events || []).map(e =>
    `<tr><td>${fmtTime(e.ts)}</td><td><span class="badge b-${e.type}">${e.type.replace("slot_", "")}</span></td>
     <td>${eventDetail(e)}</td></tr>`).join("");
}

function renderFidelity() {
  const f = S.fidelity || {};
  $("#fidelity").innerHTML = `
    <div><div class="l">State agreement vs ground truth</div><div class="v">${f.state_agreement_pct ?? "—"}%</div></div>
    <div><div class="l">MQTT sync latency (avg / max)</div><div class="v">${f.latency_ms_avg ?? "—"}<small> / ${f.latency_ms_max ?? "—"} ms</small></div></div>
    <div><div class="l">Vehicles-inside error (MAE)</div><div class="v">${f.inside_count_mae ?? "—"}</div></div>
    <div><div class="l">Samples compared</div><div class="v">${f.samples ?? 0}</div></div>`;
  $("#nodes").innerHTML = (S.nodes || []).map(n =>
    `<li><span><i class="dot ${n.online ? "on" : ""}"></i>${n.node}</span>
     <span>${n.rssi != null ? n.rssi + " dBm · " : ""}${n.uptime_s != null ? "up " + n.uptime_s + "s · " : ""}${n.age_s}s ago</span></li>`).join("");
}

function render(snap) {
  S = snap;
  renderHeader(); renderKpis(); renderMap(); renderGate(); renderSlotCard(); renderAlerts(); renderEvents(); renderFidelity();
  if (Date.now() - lastFc > 10000) loadForecast();
}

/* ===================================================================== FORECAST */
async function loadForecast() {
  lastFc = Date.now();
  const m = $("#fcMethod").value, h = $("#fcHorizon").value;
  let f;
  try { f = await api(`/api/forecast?method=${m}&horizon=${h}`); } catch { return; }
  if (!f || !f.forecast) return;
  const cap = f.capacity;
  Chart.line($("#fcChart"), {
    yMax: cap, yStep: cap > 8 ? 2 : 1, capacity: cap, nowX: S?.now_ts, xFmt: fmtHour,
    yFmt: v => (typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(1)) : v),
    bands: [{ name: "80% band", color: "#22d3ee", opacity: .13, points: f.forecast.map(p => ({ x: p.ts, lo: p.lo, hi: p.hi })) }],
    series: [
      { name: "Actual", color: "#e6edf7", dots: true, area: true, points: f.history.map(p => ({ x: p.ts, y: p.actual })) },
      { name: "Predicted (backtest)", color: "#f59e0b", dash: "5 4", points: f.backtest.map(p => ({ x: p.ts, y: p.predicted })) },
      { name: "Forecast", color: "#22d3ee", width: 2.5, dots: true, points: f.forecast.map(p => ({ x: p.ts, y: p.predicted })) },
    ],
  });
  const mt = f.metrics || {};
  $("#fcMetrics").innerHTML = `
    <span class="chip">Method<b>${f.method}</b></span>
    <span class="chip">MAE<b>${mt.mae ?? "—"}</b> slots</span>
    <span class="chip">RMSE<b>${mt.rmse ?? "—"}</b></span>
    <span class="chip">Error<b>${mt.mape_pct_capacity ?? "—"}%</b> of capacity</span>
    ${f.peak ? `<span class="chip hl">Expected peak<b>${fmtHour(f.peak.ts)}</b> · ${f.peak.pct}%</span>` : ""}
    ${f.full_soon ? `<span class="chip hl"><span class="up">⚠ ≥90% full by ${fmtHour(f.full_soon.ts)}</span></span>` : ""}`;
}

/* ===================================================================== WHAT-IF */
async function runWhatIf() {
  const btn = $("#btnWhatIf");
  btn.disabled = true; btn.textContent = "Simulating…";
  const body = { demand: +$("#wiDemand").value / 100, closed_slots: +$("#wiClosed").value,
                 stay_mult: +$("#wiStay").value / 100, hours: +$("#wiHours").value };
  try {
    const r = await api("/api/whatif", body);
    const b = r.baseline, s = r.scenario;
    Chart.line($("#wiChart"), {
      yMax: b.capacity, yStep: 2, capacity: s.capacity, xFmt: fmtHour,
      yFmt: v => (Number.isInteger(v) ? v : v.toFixed(1)),
      bands: [{ name: "scenario p10–p90", color: "#f472b6", opacity: .15, points: s.curve.map(p => ({ x: p.ts, lo: p.p10, hi: p.p90 })) }],
      series: [
        { name: "Baseline", color: "#94a3b8", dash: "5 4", points: b.curve.map(p => ({ x: p.ts, y: p.mean })) },
        { name: "Scenario", color: "#f472b6", width: 2.5, dots: true, points: s.curve.map(p => ({ x: p.ts, y: p.mean })) },
      ],
    });
    const arrow = (d) => d > 0 ? `<span class="up">+${d}</span>` : d < 0 ? `<span class="down">${d}</span>` : "±0";
    $("#wiSummary").innerHTML = `
      <span class="chip">Turned-away cars<b>${b.expected_rejected} → ${s.expected_rejected}</b> (${arrow(r.delta.rejected)})</span>
      <span class="chip">Hours at full<b>${b.expected_full_hours} → ${s.expected_full_hours}</b> (${arrow(r.delta.full_hours)})</span>
      <span class="chip">Peak occupancy<b>${b.peak_pct}% → ${s.peak_pct}%</b></span>`;
  } finally {
    btn.disabled = false; btn.textContent = "Run scenario";
  }
}

/* ===================================================================== WIRING */
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => setPill("#pillWs", true);
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.type === "snapshot") render(m.data); };
  ws.onclose = () => { setPill("#pillWs", false); setTimeout(connect, 1500); };
  setInterval(() => ws.readyState === 1 && ws.send("ping"), 15000);
}

document.querySelectorAll("#gateSeg button").forEach(b => b.onclick = () => api("/api/gate", { mode: b.dataset.mode }));
document.querySelectorAll("#slotSeg button").forEach(b => b.onclick = () =>
  selected && api(`/api/slots/${selected}/mode`, { mode: b.dataset.mode }));
$("#btnFault").onclick = () => selected && api("/api/sim", { fault: selected });
$("#btnClear").onclick = () => selected && api("/api/sim", { clear_fault: selected });
$("#simSpeed").onchange = e => api("/api/sim", { speed: +e.target.value });
$("#simDemand").oninput = e => $("#demandOut").textContent = e.target.value + "%";
$("#simDemand").onchange = e => api("/api/sim", { demand: +e.target.value / 100 });
$("#btnRush").onclick = () => api("/api/sim", { rush: 6 });
$("#btnPause").onclick = () => api("/api/sim", { pause: !(S?.sim?.paused) });
$("#fcMethod").onchange = loadForecast;
$("#fcHorizon").onchange = loadForecast;
[["wiDemand", "wiDemandOut", v => v + "%"], ["wiClosed", "wiClosedOut", v => v], ["wiStay", "wiStayOut", v => v + "%"],
 ["wiHours", "wiHoursOut", v => v + " h"]].forEach(([i, o, f]) => $("#" + i).oninput = e => $("#" + o).textContent = f(e.target.value));
$("#btnWhatIf").onclick = runWhatIf;
window.addEventListener("resize", () => { lastFc = 0; });

connect();
