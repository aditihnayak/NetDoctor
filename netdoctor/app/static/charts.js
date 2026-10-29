/* NetDoctor charts: small hand-rolled SVG, no dependencies.
   Every chart pairs colour with a second cue (line style, label or icon). */
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function niceMax(v) {
    if (v <= 10) return 10;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  function pct(arr, p) {
    if (!arr.length) return 0;
    const s = [...arr].sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor((s.length - 1) * p))];
  }

  const PHASE_LABEL = { idle: "at rest", download: "downloading", upload: "uploading" };

  /* Delay during the test: router (dashed) vs internet (solid), phases shaded. */
  function delayChart(tl, opts = {}) {
    const W = 680, H = opts.height || 220, L = 40, R = 8, T = 22, B = 24;
    const pts = (tl && tl.points) || [];
    const phases = (tl && tl.phases) || [];
    const tMax = Math.max(opts.tMax || 0, ...pts.map((p) => p[0]), ...phases.map((p) => p[2] || p[1]), 5);
    const vals = pts.map((p) => p[2]).filter((v) => v != null);
    const cap = Math.max(pct(vals, 0.99) * 1.15, 20);
    const yMax = niceMax(cap);
    const x = (t) => L + (t / tMax) * (W - L - R);
    const y = (v) => T + (1 - Math.min(v, yMax) / yMax) * (H - T - B);
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Delay during the test">`;
    // phase bands
    for (const [name, a, b] of phases) {
      if (name === "after") continue;
      const x0 = x(a), x1 = x(b ?? tMax);
      if (name !== "idle") s += `<rect x="${x0}" y="${T}" width="${Math.max(0, x1 - x0)}" height="${H - T - B}" fill="var(${name === "download" ? "--band" : "--band-2"})"/>`;
      if (x1 - x0 > 40) s += `<text x="${(x0 + x1) / 2}" y="${T - 8}" text-anchor="middle">${PHASE_LABEL[name] || name}</text>`;
    }
    // grid
    for (let i = 0; i <= 5; i++) {
      const v = (yMax / 5) * i, yy = y(v);
      s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text x="${L - 6}" y="${yy + 4}" text-anchor="end">${Math.round(v)}</text>`;
    }
    s += `<text x="${L - 6}" y="${T - 8}" text-anchor="end">ms</text>`;
    const step = tMax > 40 ? 10 : 5;
    for (let t = 0; t <= tMax; t += step) s += `<text x="${x(t)}" y="${H - 6}" text-anchor="middle">${t}s</text>`;
    // lines
    const series = [["gateway", "var(--line-a)", "5 4"], ["internet", "var(--line-b)", ""]];
    for (const [key, color, dash] of series) {
      const own = pts.filter((p) => p[1] === key).sort((a, b) => a[0] - b[0]);
      let d = "", pen = false, lost = "";
      for (const [t, , v] of own) {
        if (v == null) { pen = false; lost += `<text x="${x(t)}" y="${T + 9}" text-anchor="middle" fill="${color}" style="fill:${color}">×</text>`; continue; }
        d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      }
      s += `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" stroke-dasharray="${dash}" stroke-linejoin="round"/>` + lost;
    }
    if (vals.some((v) => v > yMax)) s += `<text x="${W - R}" y="${T + 10}" text-anchor="end">peaks above ${yMax} ms cut off</text>`;
    return s + "</svg>";
  }

  function delayLegend(hasGateway = true) {
    return `<div class="legend">${hasGateway ? '<span><span class="sw" style="border-top:2px dashed var(--line-a)"></span>To your router</span>' : ""}<span><span class="sw" style="border-color:var(--line-b)"></span>To the internet</span><span>× = lost</span></div>`;
  }

  /* Per-hop chart: one bar per hop, loss marked; harmless middle-hop loss shown faded. */
  function hopChart(tr) {
    const hops = tr.hops || [];
    const meds = hops.map((h) => (h.stats || {}).median).filter((v) => v != null);
    const max = niceMax(Math.max(...meds, 10));
    const W = 680, rowH = 26, L = 200, R = 120, H = hops.length * rowH + 22;
    const kindName = { home: "home", isp: "provider", internet: "internet", silent: "" };
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Route to ${esc(tr.target)}">`;
    hops.forEach((h, i) => {
      const yy = i * rowH + 4;
      const med = (h.stats || {}).median;
      const harmless = (tr.harmless_loss_hops || []).includes(h.n);
      const origin = tr.loss_origin && tr.loss_origin.n === h.n;
      const jump = tr.latency_jump && tr.latency_jump.n === h.n;
      const op = harmless ? 0.45 : 1;
      s += `<text x="0" y="${yy + 14}" style="fill:var(--text)">${h.n}</text>`;
      s += `<text x="20" y="${yy + 14}">${esc(h.ip || "* no reply")}</text>`;
      s += `<text x="${L - 8}" y="${yy + 14}" text-anchor="end">${kindName[h.kind] || ""}</text>`;
      if (med != null) {
        const w = Math.max(2, (med / max) * (W - L - R));
        const col = h.kind === "home" ? "var(--line-a)" : h.kind === "isp" ? "var(--fair)" : "var(--faint)";
        s += `<rect x="${L}" y="${yy + 3}" width="${w}" height="14" rx="3" fill="${col}" opacity="${op}"/>`;
        s += `<text x="${L + w + 6}" y="${yy + 14}">${med.toFixed(med < 10 ? 1 : 0)} ms</text>`;
      }
      const loss = h.loss;
      if (loss != null && loss > 0) {
        const label = origin ? `✗ ${Math.round(loss * 100)}% lost · starts here` : harmless ? `${Math.round(loss * 100)}% (harmless)` : `${Math.round(loss * 100)}% lost`;
        s += `<text x="${W}" y="${yy + 14}" text-anchor="end" style="fill:${origin ? "var(--poor)" : "var(--muted)"};opacity:${op}">${label}</text>`;
      } else if (jump) {
        s += `<text x="${W}" y="${yy + 14}" text-anchor="end" style="fill:var(--fair)">⚠ +${Math.round(tr.latency_jump.ms)} ms</text>`;
      }
    });
    s += `<text x="${L}" y="${H - 2}">0</text><text x="${W - R}" y="${H - 2}" text-anchor="end">${max} ms</text>`;
    return s + "</svg>";
  }

  /* Wi-Fi channel map: networks per channel, yours highlighted, best marked. */
  function channelChart(w) {
    const band = w.band;
    const per = Object.fromEntries(Object.entries(w.per_channel || {}).map(([k, v]) => [+k, v]));
    let chans;
    if (band === "2.4") chans = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13];
    else {
      const set = new Set([36, 40, 44, 48, 149, 153, 157, 161, w.channel, w.best_channel]);
      Object.keys(per).forEach((c) => { if ((+c > 14) === (band !== "2.4")) set.add(+c); });
      chans = [...set].filter((c) => c && c > 14).sort((a, b) => a - b);
    }
    const W = 680, H = 170, B = 36, T = 18;
    const max = Math.max(3, ...chans.map((c) => per[c] || 0));
    const bw = (W / chans.length) * 0.6;
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Nearby networks per channel">`;
    chans.forEach((c, i) => {
      const cx = (i + 0.5) * (W / chans.length);
      const n = per[c] || 0;
      const h = (n / max) * (H - B - T);
      const worthIt = (w.neighbours_on_channel || 0) - (w.best_channel_neighbours || 0) >= 3;
      const mine = c === w.channel, best = worthIt && c === w.best_channel && c !== w.channel;
      s += `<rect x="${cx - bw / 2}" y="${H - B - h}" width="${bw}" height="${Math.max(h, 1)}" rx="3" fill="${mine ? "var(--accent)" : "var(--faint)"}" opacity="${mine ? 1 : 0.5}"/>`;
      if (best) s += `<rect x="${cx - bw / 2}" y="${T}" width="${bw}" height="${H - B - T}" rx="3" fill="none" stroke="var(--good)" stroke-dasharray="4 3"/>`;
      if (n) s += `<text x="${cx}" y="${H - B - h - 4}" text-anchor="middle">${n}</text>`;
      s += `<text x="${cx}" y="${H - B + 14}" text-anchor="middle" style="${mine || best ? "fill:var(--text);font-weight:600" : ""}">${c}</text>`;
      if (mine) s += `<text x="${cx}" y="${H - 4}" text-anchor="middle" style="fill:var(--accent)">you</text>`;
      if (best) s += `<text x="${cx}" y="${H - 4}" text-anchor="middle" style="fill:var(--good)">best</text>`;
    });
    return s + "</svg>";
  }

  /* DNS comparison: horizontal bars, your resolver first. */
  function dnsChart(list) {
    const items = list.filter((d) => d);
    const max = niceMax(Math.max(...items.map((d) => d.median || 0), 20));
    const W = 680, rowH = 30, L = 170, H = items.length * rowH;
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="DNS lookup times">`;
    items.forEach((d, i) => {
      const yy = i * rowH;
      const mine = i === 0;
      s += `<text x="0" y="${yy + 18}" style="${mine ? "fill:var(--text);font-weight:600" : ""}">${esc(d.label)}</text>`;
      if (d.median != null) {
        const w = Math.max(2, (d.median / max) * (W - L - 80));
        s += `<rect x="${L}" y="${yy + 6}" width="${w}" height="16" rx="3" fill="${mine ? "var(--accent)" : "var(--faint)"}" opacity="${mine ? 1 : 0.6}"/>`;
        s += `<text x="${L + w + 6}" y="${yy + 18}">${Math.round(d.median)} ms${d.fail ? ` · ${Math.round(d.fail * 100)}% failed` : ""}</text>`;
      } else s += `<text x="${L}" y="${yy + 18}" style="fill:var(--poor)">✗ no answer</text>`;
    });
    return s + "</svg>";
  }

  /* Throughput over time. */
  function throughputChart(series, color) {
    const W = 330, H = 70;
    if (!series || !series.length) return "";
    const tMax = series[series.length - 1][0] || 1;
    const max = niceMax(Math.max(...series.map((p) => p[1]), 1));
    const d = series.map((p, i) => `${i ? "L" : "M"}${((p[0] / tMax) * W).toFixed(1)},${(H - (p[1] / max) * (H - 8)).toFixed(1)}`).join("");
    return `<svg class="chart" viewBox="0 0 ${W} ${H + 14}"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/><text x="0" y="${H + 12}">0s</text><text x="${W}" y="${H + 12}" text-anchor="end">${max} Mbps peak scale</text></svg>`;
  }

  /* History heatmap: day of week x hour, coloured by quality (with a text legend). */
  function heatmap(grid) {
    const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    let s = '<div class="heat" role="table" aria-label="Connection quality by hour">';
    s += "<div></div>" + Array.from({ length: 24 }, (_, h) => `<div style="text-align:center">${h % 3 === 0 ? h : ""}</div>`).join("");
    grid.forEach((row, d) => {
      s += `<div>${days[d]}</div>`;
      row.forEach((v, h) => {
        const bg = v == null ? "var(--unknown-bg)" : v >= 75 ? "var(--good)" : v >= 50 ? "var(--fair)" : "var(--poor)";
        const op = v == null ? 1 : 0.35 + 0.65 * (v >= 75 ? (v - 75) / 25 : v >= 50 ? 1 - (v - 50) / 25 : 1 - v / 50);
        s += `<div class="cell" style="background:${bg};opacity:${op.toFixed(2)}" title="${days[d]} ${h}:00 · ${v == null ? "no data" : "quality " + v}"></div>`;
      });
    });
    return s + "</div>";
  }

  window.Charts = { delayChart, delayLegend, hopChart, channelChart, dnsChart, throughputChart, heatmap, esc };
})();
