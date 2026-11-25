/* Tiny dependency-free SVG line chart (works offline during the demo). */
const Chart = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };

  function line(container, o) {
    container.innerHTML = "";
    const W = Math.max(320, container.clientWidth), H = Math.max(160, container.clientHeight);
    const P = { l: 38, r: 14, t: 26, b: 26 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` }, container);
    const tip = document.createElement("div");
    tip.className = "tip";
    container.appendChild(tip);

    const xs = [];
    (o.series || []).forEach(s => s.points.forEach(p => xs.push(p.x)));
    (o.bands || []).forEach(b => b.points.forEach(p => xs.push(p.x)));
    if (!xs.length) { container.innerHTML = '<div class="empty">No data yet</div>'; return; }
    const xMin = o.xMin ?? Math.min(...xs), xMax = o.xMax ?? Math.max(...xs);
    const yMin = o.yMin ?? 0, yMax = o.yMax ?? 1;
    const X = x => P.l + (x - xMin) / Math.max(1, xMax - xMin) * (W - P.l - P.r);
    const Y = y => H - P.b - (y - yMin) / Math.max(1e-9, yMax - yMin) * (H - P.t - P.b);

    // grid + y ticks
    const step = o.yStep || Math.max(1, Math.round((yMax - yMin) / 4));
    for (let v = yMin; v <= yMax + 1e-9; v += step) {
      el("line", { x1: P.l, x2: W - P.r, y1: Y(v), y2: Y(v), stroke: "#1f2b46", "stroke-width": 1 }, svg);
      el("text", { x: P.l - 6, y: Y(v) + 4, "text-anchor": "end", fill: "#8696b8", "font-size": 10.5,
        "font-family": "ui-monospace,monospace" }, svg).textContent = o.yFmt ? o.yFmt(v) : v;
    }
    if (o.capacity != null) {
      el("line", { x1: P.l, x2: W - P.r, y1: Y(o.capacity), y2: Y(o.capacity), stroke: "#ef4444",
        "stroke-dasharray": "3 4", "stroke-opacity": .6 }, svg);
      el("text", { x: W - P.r, y: Y(o.capacity) - 4, "text-anchor": "end", fill: "#ef4444", "font-size": 10 }, svg)
        .textContent = "capacity";
    }
    // x ticks
    const uniq = [...new Set(xs)].sort((a, b) => a - b);
    const every = Math.max(1, Math.ceil(uniq.length / Math.floor((W - P.l) / 62)));
    uniq.forEach((x, i) => {
      if (i % every) return;
      el("text", { x: X(x), y: H - 8, "text-anchor": "middle", fill: "#8696b8", "font-size": 10.5,
        "font-family": "ui-monospace,monospace" }, svg).textContent = o.xFmt ? o.xFmt(x) : x;
    });
    // "now" marker
    if (o.nowX != null && o.nowX >= xMin && o.nowX <= xMax) {
      el("line", { x1: X(o.nowX), x2: X(o.nowX), y1: P.t - 6, y2: H - P.b, stroke: "#22d3ee", "stroke-opacity": .5,
        "stroke-dasharray": "2 3" }, svg);
      el("text", { x: X(o.nowX) + 4, y: P.t - 2, fill: "#22d3ee", "font-size": 10 }, svg).textContent = "now";
    }
    // bands
    (o.bands || []).forEach(b => {
      if (!b.points.length) return;
      const top = b.points.map(p => `${X(p.x)},${Y(p.hi)}`);
      const bot = b.points.slice().reverse().map(p => `${X(p.x)},${Y(p.lo)}`);
      el("polygon", { points: top.concat(bot).join(" "), fill: b.color, "fill-opacity": b.opacity ?? .15 }, svg);
    });
    // series
    (o.series || []).forEach(s => {
      if (!s.points.length) return;
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p.x)},${Y(p.y)}`).join("");
      if (s.area) {
        el("path", { d: d + `L${X(s.points.at(-1).x)},${Y(yMin)}L${X(s.points[0].x)},${Y(yMin)}Z`,
          fill: s.color, "fill-opacity": .08 }, svg);
      }
      el("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2,
        "stroke-dasharray": s.dash || "", "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      if (s.dots) s.points.forEach(p => el("circle", { cx: X(p.x), cy: Y(p.y), r: 2.6, fill: s.color }, svg));
    });
    // legend
    let lx = P.l;
    (o.series || []).forEach(s => {
      el("line", { x1: lx, x2: lx + 16, y1: 10, y2: 10, stroke: s.color, "stroke-width": 2.5,
        "stroke-dasharray": s.dash || "" }, svg);
      const t = el("text", { x: lx + 21, y: 14, fill: "#c7d2e8", "font-size": 11 }, svg);
      t.textContent = s.name;
      lx += 30 + s.name.length * 6.2;
    });

    // hover crosshair + tooltip
    const cross = el("line", { y1: P.t, y2: H - P.b, stroke: "#94a3b8", "stroke-opacity": 0 }, svg);
    const hit = el("rect", { x: P.l, y: P.t, width: W - P.l - P.r, height: H - P.t - P.b, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect();
      const mx = (ev.clientX - r.left) * W / r.width;
      let best = uniq[0];
      for (const x of uniq) if (Math.abs(X(x) - mx) < Math.abs(X(best) - mx)) best = x;
      cross.setAttribute("x1", X(best)); cross.setAttribute("x2", X(best)); cross.setAttribute("stroke-opacity", .5);
      const rows = [];
      (o.series || []).forEach(s => {
        const p = s.points.find(q => q.x === best);
        if (p) rows.push(`<span style="color:${s.color}">■</span> ${s.name}: <b>${o.yFmt ? o.yFmt(p.y) : p.y.toFixed(2)}</b>`);
      });
      (o.bands || []).forEach(b => {
        const p = b.points.find(q => q.x === best);
        if (p && b.name) rows.push(`${b.name}: ${p.lo.toFixed(1)} – ${p.hi.toFixed(1)}`);
      });
      tip.innerHTML = `<div style="color:#8696b8">${o.xFmt ? o.xFmt(best) : best}</div>` + rows.join("<br>");
      tip.style.display = rows.length ? "block" : "none";
      const px = X(best) / W * r.width;
      tip.style.left = Math.min(r.width - tip.offsetWidth - 4, px + 12) + "px";
      tip.style.top = "30px";
    });
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("stroke-opacity", 0); });
  }

  return { line };
})();
