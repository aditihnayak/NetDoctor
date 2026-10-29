/* NetDoctor app: Home, Running, Results, Details, History, ISP report, Settings. */
(function () {
  const { esc } = Charts;
  const $app = document.getElementById("app");
  const SEGS = ["wifi", "router", "isp", "internet", "dns", "website"];
  const SEG_NAME = { wifi: "Wi-Fi", router: "Router", isp: "ISP", internet: "Internet", dns: "DNS", website: "Websites" };
  const ICON = { good: "✓", fair: "!", poor: "✕", unknown: "?" };
  const WORD = { good: "Good", fair: "Fair", poor: "Poor", unknown: "Unknown" };
  let state = { settings: {} };
  let es = null;

  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok && r.status !== 409) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  };

  // Plain text with `command` spans turned into <code>.
  const rich = (t) => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>");
  const icon = (st) => `<span class="ic ic-${st}" aria-hidden="true">${ICON[st] || ""}</span>`;
  const status = (st) => `<span class="st st-${st}">${icon(st)}${WORD[st]}</span>`;
  const fmtMs = (v) => (v == null ? "–" : v < 10 ? v.toFixed(1) : Math.round(v)) ;
  const when = (iso) => {
    if (!iso) return "";
    const d = new Date(iso), now = new Date();
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
    return days === 0 ? `today ${time}` : days === 1 ? `yesterday ${time}` : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
  };
  const verdictStatus = (c) => (c === "none" ? "good" : c === "inconclusive" ? "unknown" : "poor");

  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }

  function setNav(name) {
    document.querySelectorAll("[data-nav]").forEach((a) => a.toggleAttribute("aria-current", a.dataset.nav === name));
    if (a11yFocus) $app.focus({ preventScroll: true });
  }
  let a11yFocus = false;

  // ---------------------------------------------------------------- Home
  async function home() {
    setNav("home");
    const s = await api("/api/state");
    state.settings = s.settings;
    applyTheme(s.settings.theme);
    if (s.running) return (location.hash = "#/running");
    const w = s.watch || {};
    const last = s.last
      ? `<div class="card"><div class="last">${icon(verdictStatus(s.last.culprit))}<div class="spacer"><div class="small muted">Last check · ${esc(when(s.last.started_at))}</div><div>${esc(s.last.culprit === "none" ? "All good" : s.last.title)}</div></div><a class="btn" href="#/result/${s.last.id}">View</a></div></div>`
      : "";
    const watch = s.settings.watch_enabled
      ? `<p class="small muted" style="margin-top:16px">Watch mode: on · ${w.count ? `${w.count} checks this week` : "first check soon"}${w.last_issue ? ` · last issue ${esc(when(w.last_issue))}` : w.count ? " · no issues" : ""}</p>`
      : `<p class="small muted" style="margin-top:16px">Watch mode: off · <a href="#/settings">turn on</a> to catch problems that come and go</p>`;
    $app.innerHTML = `
      <section class="hero">
        <button class="primary big" id="go">Check my internet</button>
        <p class="sub muted">Takes about ${s.settings.load_test ? 30 : 15} seconds</p>
        <form class="site" id="siteform" autocomplete="off">
          <input type="text" id="site" placeholder="Also test a website (optional)" aria-label="Website to test, optional">
        </form>
      </section>
      ${last}
      ${watch}`;
    const start = async (e) => {
      e && e.preventDefault();
      const site = document.getElementById("site").value.trim();
      const r = await api("/api/check", { method: "POST", body: JSON.stringify({ site, load: s.settings.load_test }) });
      if (r.ok === false && !r.error) { /* already running */ }
      location.hash = "#/running";
    };
    document.getElementById("go").onclick = start;
    document.getElementById("siteform").onsubmit = start;
  }

  // ---------------------------------------------------------------- Running
  function running() {
    setNav("running");
    const budget = state.settings.load_test === false ? 15 : 30;
    const run = { seg: Object.fromEntries(SEGS.map((k) => [k, "waiting"])), statuses: {}, points: [], phases: [], phase: "Starting…", note: "", mbps: {}, t0: Date.now() };
    $app.innerHTML = `
      <div class="running-head"><h1 id="phase">Checking your connection…</h1><span class="timer" id="timer">0:00</span></div>
      <div class="progressbar" aria-hidden="true"><div id="bar"></div></div>
      <div class="card"><div class="path" id="path"></div></div>
      <div class="card">
        <div class="row"><h2 class="spacer">Delay right now</h2><span class="small muted" id="now"></span></div>
        <div id="live"></div>
        ${Charts.delayLegend(true)}
        <p class="caption">If the line jumps while downloading or uploading, calls and games would lag whenever your connection is busy.</p>
      </div>
      <div id="loadbox"></div>
      <div id="err"></div>`;
    const $path = document.getElementById("path");
    const drawPath = () => {
      $path.innerHTML = SEGS.map((k) => {
        const st = run.seg[k];
        const s = st === "done" ? run.statuses[k] || "good" : st;
        const word = st === "done" ? WORD[s] : st === "testing" ? "Testing…" : "Waiting";
        return `<div class="seg"><span class="ic ic-${s}" aria-hidden="true">${ICON[s] || ""}</span><span class="name">${SEG_NAME[k]}</span><span class="word ${st === "done" ? "st-" + s : "muted"}">${word}</span></div>`;
      }).join("");
    };
    let pending = false;
    const drawLive = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        const el = (Date.now() - run.t0) / 1000;
        document.getElementById("live").innerHTML = Charts.delayChart({ points: run.points, phases: run.phases }, { tMax: Math.max(budget, el), height: 200 });
        const last = [...run.points].reverse().find((p) => p[1] === "internet" && p[2] != null);
        document.getElementById("now").textContent = last ? `${Math.round(last[2])} ms to the internet` : "";
      });
    };
    const drawLoad = () => {
      const b = document.getElementById("loadbox");
      if (!Object.keys(run.mbps).length) return;
      b.innerHTML = `<div class="card"><div class="nums" style="grid-template-columns:repeat(2,1fr)">
        <div class="num"><div class="v throughput">${run.mbps.download != null ? Math.round(run.mbps.download) : "–"} <small>Mbps</small></div><div class="l">↓ Download</div></div>
        <div class="num"><div class="v throughput">${run.mbps.upload != null ? Math.round(run.mbps.upload) : "–"} <small>Mbps</small></div><div class="l">↑ Upload</div></div></div>
        ${run.note ? `<p class="note">${esc(run.note)}</p>` : ""}</div>`;
    };
    drawPath();
    drawLive();
    const timer = setInterval(() => {
      const el = (Date.now() - run.t0) / 1000;
      document.getElementById("timer") && (document.getElementById("timer").textContent = `0:${String(Math.floor(el)).padStart(2, "0")}`);
      const bar = document.getElementById("bar");
      if (bar) bar.style.width = `${Math.min(97, (el / budget) * 100)}%`;
      if (!document.getElementById("timer")) clearInterval(timer);
    }, 250);

    if (es) es.close();
    es = new EventSource("/api/events");
    es.onmessage = (m) => {
      const e = JSON.parse(m.data);
      if (e.type === "phase") {
        document.getElementById("phase").textContent = e.label + "…";
        run.note = e.note || "";
        if (e.phase === "load") { run.mbps = { download: 0 }; drawLoad(); }
      } else if (e.type === "step") { run.seg[e.segment] = e.state; drawPath(); }
      else if (e.type === "segments") { run.statuses = Object.fromEntries(Object.entries(e.segments).map(([k, v]) => [k, v.status])); drawPath(); }
      else if (e.type === "sample") {
        if (!run.phases.length || run.phases[run.phases.length - 1][0] !== e.phase) {
          if (run.phases.length) run.phases[run.phases.length - 1][2] = e.t;
          run.phases.push([e.phase, e.t, null]);
        }
        run.points.push([e.t, e.target, e.ms]);
        drawLive();
      } else if (e.type === "throughput" || e.type === "load_done") { run.mbps[e.direction] = e.mbps; drawLoad(); }
      else if (e.type === "saved") { es.close(); clearInterval(timer); location.hash = `#/result/${e.id}`; }
      else if (e.type === "error") { document.getElementById("err").innerHTML = `<div class="error">Something went wrong: ${esc(e.text)}. <a href="#/">Try again</a></div>`; }
      else if (e.type === "end") { es.close(); }
    };
    es.onerror = () => { /* the browser retries automatically */ };
  }

  // ---------------------------------------------------------------- Results
  const viewCache = {};
  async function getView(id) {
    if (!viewCache[id]) viewCache[id] = await api(`/api/run/${id}`);
    return viewCache[id];
  }

  function keyNumbers(k) {
    const tiles = [];
    tiles.push(`<div class="num"><div class="v">${k.download_mbps != null ? Math.round(k.download_mbps) : "–"}<small> / ${k.upload_mbps != null ? Math.round(k.upload_mbps) : "–"} Mbps</small></div><div class="l">Download / upload</div></div>`);
    tiles.push(`<div class="num"><div class="v">${k.lag_grade || "–"}</div><div class="l">Lag under load${k.lag_extra_ms != null ? ` (+${Math.round(k.lag_extra_ms)} ms)` : ""}</div></div>`);
    tiles.push(`<div class="num"><div class="v">${k.wifi_rssi_dbm != null ? k.wifi_rssi_dbm + "<small> dBm</small>" : "–"}</div><div class="l">Wi-Fi signal${k.wifi_label ? " · " + k.wifi_label : ""}</div></div>`);
    tiles.push(`<div class="num"><div class="v">${k.dns_ms != null ? Math.round(k.dns_ms) + "<small> ms</small>" : "–"}</div><div class="l">DNS speed${k.dns_label ? " · " + k.dns_label : ""}</div></div>`);
    return `<div class="nums">${tiles.join("")}</div>`;
  }

  async function result(id) {
    setNav("result");
    const v = await getView(id);
    const vd = v.verdict;
    const st = verdictStatus(vd.culprit);
    const segs = vd.segments;
    const strip = SEGS.map((k) => {
      const s = segs[k].status;
      return `<div class="seg ${vd.blame_segment === k && vd.culprit !== "none" ? "blame" : ""}"><span class="ic ic-${s}" aria-hidden="true">${ICON[s]}</span><span class="name">${SEG_NAME[k]}</span><span class="word st-${s}">${WORD[s]}</span></div>`;
    }).join("");
    const segList = SEGS.map((k) => `<div class="item">${icon(segs[k].status)}<span class="name">${SEG_NAME[k]}</span><span class="muted">${esc(segs[k].summary)}</span></div>`).join("");
    const also = (vd.also || []).length
      ? `<div class="card"><h2>Also noticed</h2><ul class="plain">${vd.also.map((a) => `<li>${esc(a.title)}${a.evidence[0] ? ` <span class="muted small">· ${esc(a.evidence[0])}</span>` : ""}</li>`).join("")}</ul></div>` : "";
    $app.innerHTML = `
      <div class="card verdict">
        <div class="head">${icon(st)}<h1>${esc(vd.title)}</h1><span class="badge ${vd.confidence_label}">${vd.confidence_label === "Inconclusive" ? "Inconclusive" : vd.confidence_label + " confidence"}</span></div>
        <p class="msg">${esc(vd.message)}</p>
        <p class="small muted" style="margin-top:12px">${esc(when(v.started_at))}${v.context.isp ? " · " + esc(v.context.isp) : ""}${v.context.link ? " · " + (v.context.link === "wifi" ? "Wi-Fi" : v.context.link) : ""}${v.context.vpn ? " · VPN" : ""}${v.options.site ? " · tested " + esc(v.options.site) : ""}</p>
      </div>
      <div class="card"><div class="path">${strip}</div><div class="seglist">${segList}</div></div>
      <div class="card">${keyNumbers(vd.key_numbers)}${vd.key_numbers.lag_meaning ? `<p class="caption">Lag grade ${vd.key_numbers.lag_grade}: ${esc(vd.key_numbers.lag_meaning)}.</p>` : ""}</div>
      ${v.timeline ? `<div class="card"><h2>Delay during the test</h2>${Charts.delayChart(v.timeline)}${Charts.delayLegend(!!v.context.gateway)}<p class="caption">${delayCaption(v)}</p></div>` : ""}
      <div class="card"><h2>What to do</h2><ol class="todo">${vd.actions.map((a) => `<li>${rich(a.text)}</li>`).join("")}</ol></div>
      ${also}
      <div class="row noprint" style="margin-top:16px">
        <a class="btn" href="#/details/${id}/path">Technical details</a>
        <a class="btn" href="#/report/${id}">Create ISP report</a>
        <span class="spacer"></span>
        <a class="btn" href="/api/run/${id}/json" download>Save JSON</a>
        <a class="btn primary" href="#/">Run again</a>
      </div>`;
  }

  function delayCaption(v) {
    const F = v.features;
    const b = F.bloat_worst, lb = F.local_bloat_worst;
    if (b == null) return "Delay to your router and to the internet while the test was running.";
    if (b < 30) return `Delay barely changed while your connection was busy (+${Math.round(b)} ms): calls and games will feel smooth.`;
    if (lb != null && lb >= 0.5 * b) return `Delay rose by ${Math.round(b)} ms while ${F.bloat_worst_dir}ing, and the delay to your router rose too, so the queue is inside your home.`;
    return `Delay rose by ${Math.round(b)} ms while ${F.bloat_worst_dir}ing, but stayed flat to your router, so the queue is at your provider's side of the modem.`;
  }

  // ---------------------------------------------------------------- Details
  const TABS = [["path", "Path"], ["load", "Load"], ["dns", "DNS"], ["wifi", "Wi-Fi"], ["mtu", "MTU / IPv6"], ["raw", "Raw JSON"]];
  async function details(id, tab = "path") {
    setNav("details");
    const v = await getView(id);
    const F = v.features;
    let body = "";
    if (tab === "path") {
      body = F.traces.length ? F.traces.map((tr) => `<div class="card"><h2>Route to ${esc(tr.target)}</h2>${tr.hops.length ? Charts.hopChart(tr) : `<p class="muted">${esc(tr.error || "No route data")}</p>`}
        <p class="caption">Each bar is a router along the way. Loss only matters if it continues to the last hop; routers often ignore our probes, so loss at a middle hop is usually harmless.</p></div>`).join("")
        : `<div class="card empty">No route trace in this run.</div>`;
      body += `<div class="card"><h2>Context</h2><table class="data"><tbody>${["link", "gateway", "isp", "asn", "vpn", "double_nat", "cgnat", "captive_portal", "ipv6"].filter((k) => k in v.context).map((k) => `<tr><td class="muted">${k.replace("_", " ")}</td><td>${v.context[k] == null ? '<span class="muted">unknown</span>' : v.context[k] === true ? "yes" : v.context[k] === false ? "no" : esc(String(v.context[k]))}</td></tr>`).join("")}</tbody></table></div>`;
    } else if (tab === "load") {
      const rows = ["idle", "download", "upload"].map((p) => [p, F.gw[p], F.net[p]]);
      body = `<div class="card"><h2>Delay during the test</h2>${v.timeline ? Charts.delayChart(v.timeline) + Charts.delayLegend(!!v.context.gateway) : "<p class='muted'>No timeline.</p>"}</div>
        <div class="card scroll"><table class="data"><thead><tr><th>Phase</th><th>Router median</th><th>Router p95</th><th>Internet median</th><th>Internet p95</th><th>Internet loss</th></tr></thead><tbody>
        ${rows.map(([p, g, n]) => `<tr><td>${p}</td><td>${fmtMs(g && g.median)}</td><td>${fmtMs(g && g.p95)}</td><td>${fmtMs(n && n.median)}</td><td>${fmtMs(n && n.p95)}</td><td>${n ? (n.loss * 100).toFixed(1) + "%" : "–"}</td></tr>`).join("")}
        </tbody></table><p class="caption">Extra delay under load: +${fmtMs(F.bloat.download)} ms downloading, +${fmtMs(F.bloat.upload)} ms uploading → grade ${F.grade || "–"}.</p></div>
        <div class="card"><div class="nums" style="grid-template-columns:repeat(2,1fr)">${["download", "upload"].map((d) => { const m = F.load[d]; return `<div><div class="num"><div class="v">${m ? Math.round(m.mbps) : "–"} <small>Mbps</small></div><div class="l">${d === "download" ? "↓ Download" : "↑ Upload"}${m && m.errors && m.errors.length ? " · " + esc(m.errors.join(", ")) : ""}</div></div>${m ? Charts.throughputChart(m.series, d === "download" ? "var(--line-a)" : "var(--line-b)") : ""}</div>`; }).join("")}</div></div>`;
    } else if (tab === "dns") {
      const list = [F.dns_system, ...F.dns_public].filter(Boolean);
      body = `<div class="card"><h2>Name lookup time (uncached)</h2>${list.length ? Charts.dnsChart(list) : "<p class='muted'>No DNS data.</p>"}
        <p class="caption">Each lookup asks for a made-up name so no server can answer from memory. Your current DNS is the first bar.</p></div>
        ${F.dns_intercepted ? `<div class="card"><p>${icon("fair")} <b>Your provider redirects DNS traffic.</b> We asked a server address that doesn't exist and still got an answer. Changing DNS servers in your settings may have no effect; DNS-over-HTTPS (Secure DNS) in your browser gets around it.</p></div>` : ""}`;
    } else if (tab === "wifi") {
      const w = F.wifi;
      if (!w) body = `<div class="card empty">${F.link === "wifi" ? `Couldn't read Wi-Fi details (${esc(F.wifi_error || "unknown")}). On macOS, names stay hidden unless this app has Location access; signal and channel still work.` : "You're on a wired connection, so there's no Wi-Fi to check."}</div>`;
      else body = `<div class="card"><h2>Nearby networks per channel (${w.band} GHz)</h2>${Charts.channelChart(w)}
          <p class="caption">${w.congested ? `Your channel (${w.channel}) is crowded: ${w.neighbours_on_channel} networks share or overlap it.` : `Your channel (${w.channel}) has ${w.neighbours_on_channel} other network${w.neighbours_on_channel === 1 ? "" : "s"} on or overlapping it.`}${w.best_channel && w.best_channel !== w.channel && w.neighbours_on_channel - w.best_channel_neighbours >= 3 ? ` Channel ${w.best_channel} is much quieter (${w.best_channel_neighbours} networks).` : " No need to change channel."}</p></div>
        <div class="card"><table class="data"><tbody>
          <tr><td class="muted">Network</td><td>${w.names_hidden ? '<span class="muted">Hidden by macOS (give your terminal Location access to see names)</span>' : esc(w.ssid || "–")}</td></tr>
          <tr><td class="muted">Signal</td><td>${w.rssi_dbm ?? "–"} dBm</td></tr>
          <tr><td class="muted">Noise / SNR</td><td>${w.noise_dbm ?? "–"} dBm / ${w.snr_db ?? "–"} dB</td></tr>
          <tr><td class="muted">Link rate</td><td>${w.rate_mbps ?? "–"} Mbps</td></tr>
          <tr><td class="muted">Channel</td><td>${w.channel} (${w.band} GHz${w.width_mhz ? ", " + w.width_mhz + " MHz wide" : ""})</td></tr>
          <tr><td class="muted">Standard</td><td>${esc(w.phy || "–")}</td></tr>
        </tbody></table></div>`;
    } else if (tab === "mtu") {
      const m = F.mtu || {}, i = F.ipv6 || {};
      body = `<div class="card"><h2>Packet size (MTU)</h2>${F.mtu ? `<p>${icon(m.black_hole ? "poor" : "good")} Largest packet that gets through: <b>${m.path_mtu || "?"} bytes</b>. ${m.black_hole ? "Larger packets vanish without any warning: this can make some sites hang." : m.frag_needed_seen ? "Larger packets are correctly refused with a \"too big\" message, so your devices adapt automatically." : "Full-size packets get through."}</p>
        <table class="data" style="margin-top:12px"><thead><tr><th>Payload tested</th><th>Result</th></tr></thead><tbody>${(m.tested || []).map(([s, ok]) => `<tr><td>${s} bytes</td><td>${ok ? status("good").replace("Good", "Delivered") : status("fair").replace("Fair", "Not delivered")}</td></tr>`).join("")}</tbody></table>` : "<p class='muted'>Not tested.</p>"}</div>
        <div class="card"><h2>IPv6</h2>${!i.configured ? "<p>IPv6 isn't set up on this network. That's fine: everything works over IPv4.</p>" : `<p>${icon(i.broken ? "poor" : i.slow ? "fair" : "good")} IPv4 connects in ${fmtMs(i.v4_ms)} ms; IPv6 ${i.v6_ms != null ? "in " + fmtMs(i.v6_ms) + " ms" : "failed (" + esc(i.v6_error) + ")"}.</p>`}</div>`;
    } else if (tab === "raw") {
      const raw = await (await fetch(`/api/run/${id}/json`)).text();
      body = `<div class="card"><div class="row" style="margin-bottom:12px"><span class="spacer muted small">The complete measurement file, for scripts and bug reports. Replay it with <code>netdoctor replay</code>.</span><a class="btn" href="/api/run/${id}/json" download>Download</a></div><pre class="raw">${esc(raw)}</pre></div>`;
    }
    $app.innerHTML = `<div class="row" style="margin-top:8px"><a href="#/result/${id}" class="small">← Back to results</a></div>
      <h1 style="margin-top:12px">Technical details</h1>
      <nav class="tabs" role="tablist">${TABS.map(([k, l]) => `<a role="tab" href="#/details/${id}/${k}" aria-selected="${k === tab}">${l}</a>`).join("")}</nav>
      ${body}`;
  }

  // ---------------------------------------------------------------- History
  async function history() {
    setNav("history");
    const h = await api("/api/history");
    const hasHeat = h.heatmap.some((r) => r.some((c) => c != null));
    const sum = h.summary || {};
    const runs = h.runs.length
      ? `<div class="runs">${h.runs.map((r) => `<div class="r"><input type="checkbox" class="cmp" value="${r.id}" aria-label="Select for comparison">
          <div><a href="#/result/${r.id}">${esc(r.culprit === "none" ? "All good" : r.title || "Checkup")}</a><div class="small muted">${esc(when(r.started_at))} · ${esc(r.confidence || "")} confidence</div></div>
          <div class="small muted" style="text-align:right">${r.download != null ? Math.round(r.download) + " / " + Math.round(r.upload || 0) + " Mbps" : ""}${r.grade ? "<br>lag " + r.grade : ""}</div></div>`).join("")}</div>`
      : `<div class="empty">No checkups yet. <a href="#/">Run your first one</a>.</div>`;
    $app.innerHTML = `
      <h1 style="margin-top:8px">History</h1>
      <div class="card"><h2>Connection quality by hour</h2>
        ${hasHeat ? Charts.heatmap(h.heatmap) + `<div class="legend" style="margin-top:8px"><span>${icon("good")} good</span><span>${icon("fair")} fair</span><span>${icon("poor")} poor</span><span>${icon("unknown")} no data</span></div>
        <p class="caption">${sum.count} watch checks in the last 2 weeks, ${sum.bad} with problems.${sum.worst_hours && sum.worst_hours.length ? " Worst times: " + sum.worst_hours.join(", ") + "." : ""}</p>`
        : `<p class="muted">Turn on <a href="#/settings">watch mode</a> and NetDoctor will run a tiny check every few minutes. After a day or two, this shows patterns like "slow every evening 8–11 pm".</p>`}
      </div>
      <div class="card"><div class="row"><h2 class="spacer">Past checkups</h2><button id="cmpbtn" disabled>Compare two</button></div>${runs}</div>
      <div id="cmp"></div>`;
    const boxes = [...document.querySelectorAll(".cmp")];
    const btn = document.getElementById("cmpbtn");
    boxes.forEach((b) => (b.onchange = () => { btn.disabled = boxes.filter((x) => x.checked).length !== 2; }));
    btn.onclick = async () => {
      const ids = boxes.filter((x) => x.checked).map((x) => +x.value).sort((a, b) => a - b);
      const [a, b] = await Promise.all(ids.map(getView));
      const row = (label, f) => `<tr><td class="muted">${label}</td><td>${f(a)}</td><td>${f(b)}</td></tr>`;
      const k = (x) => x.verdict.key_numbers;
      document.getElementById("cmp").innerHTML = `<div class="card scroll"><h2>Comparison</h2><table class="data"><thead><tr><th></th><th>${esc(when(a.started_at))}</th><th>${esc(when(b.started_at))}</th></tr></thead><tbody>
        ${row("Finding", (x) => esc(x.verdict.title))}
        ${SEGS.map((s) => row(SEG_NAME[s], (x) => status(x.verdict.segments[s].status))).join("")}
        ${row("Download", (x) => (k(x).download_mbps != null ? Math.round(k(x).download_mbps) + " Mbps" : "–"))}
        ${row("Upload", (x) => (k(x).upload_mbps != null ? Math.round(k(x).upload_mbps) + " Mbps" : "–"))}
        ${row("Lag grade", (x) => k(x).lag_grade || "–")}
        ${row("Delay at rest", (x) => fmtMs(k(x).idle_ms) + " ms")}
        ${row("Router delay", (x) => fmtMs(k(x).router_ms) + " ms")}
        ${row("Wi-Fi signal", (x) => (k(x).wifi_rssi_dbm != null ? k(x).wifi_rssi_dbm + " dBm" : "–"))}
        ${row("DNS", (x) => fmtMs(k(x).dns_ms) + " ms")}
        </tbody></table></div>`;
    };
  }

  // ---------------------------------------------------------------- ISP report
  async function report(id) {
    setNav("report");
    const r = await api(`/api/run/${id}/isp-report`);
    $app.innerHTML = `<div class="row noprint" style="margin-top:8px"><a href="#/result/${id}" class="small">← Back to results</a></div>
      <div class="row noprint" style="margin-top:12px"><h1 class="spacer">ISP report</h1><button id="copy">Copy as text</button><button class="primary" id="pdf">Save as PDF</button></div>
      <p class="muted small noprint" style="margin-top:4px">Written for a support agent: where the problem starts, delay under load, measured vs plan speed. ${state.settings.plan_down_mbps ? "" : 'Add your plan speed in <a href="#/settings">Settings</a> to include it.'}</p>
      <div class="card"><pre class="raw" style="max-height:none;background:none;padding:0;white-space:pre-wrap">${esc(r.text)}</pre></div>`;
    document.getElementById("copy").onclick = async (e) => {
      try { await navigator.clipboard.writeText(r.text); e.target.textContent = "Copied ✓"; }
      catch { e.target.textContent = "Copy failed"; }
    };
    document.getElementById("pdf").onclick = () => {
      const w = window.open(`/api/run/${id}/isp-report.html`, "_blank");
      if (w) w.addEventListener("load", () => w.print());
      else window.print();
    };
  }

  // ---------------------------------------------------------------- Settings
  async function settings() {
    setNav("settings");
    const s = (await api("/api/state")).settings;
    state.settings = s;
    const toggle = (key, title, desc) => `<label class="switch"><span><span class="t">${title}</span><br><span class="d">${desc}</span></span><input type="checkbox" data-k="${key}" ${s[key] ? "checked" : ""}></label>`;
    $app.innerHTML = `<h1 style="margin-top:8px">Settings</h1>
      <div class="card">
        ${toggle("load_test", "Load test", "Briefly uses your full connection to measure lag under load. Turn off on metered connections or during calls.")}
        ${toggle("watch_enabled", "Watch mode", "Runs a tiny check (about 3 seconds, almost no data) every few minutes while NetDoctor is open, to find patterns.")}
        <label class="switch"><span><span class="t">Watch interval</span><br><span class="d">Minutes between watch checks.</span></span><input type="number" min="1" max="120" data-k="watch_interval_min" value="${s.watch_interval_min}" style="width:80px"></label>
        <label class="switch"><span><span class="t">Plan speed</span><br><span class="d">What you pay for, in Mbps (down / up). Used in the ISP report.</span></span><span class="row"><input type="number" min="0" data-k="plan_down_mbps" value="${s.plan_down_mbps ?? ""}" placeholder="down" style="width:80px"><input type="number" min="0" data-k="plan_up_mbps" value="${s.plan_up_mbps ?? ""}" placeholder="up" style="width:80px"></span></label>
        ${toggle("share_results", "Share anonymous results", "Opt-in. Sends results without IP addresses, network names or device identifiers, to improve the diagnosis. Only works when a NetDoctor server is configured.")}
        <label class="switch"><span><span class="t">Theme</span></span><select data-k="theme">${["system", "light", "dark"].map((t) => `<option ${s.theme === t ? "selected" : ""}>${t}</option>`).join("")}</select></label>
      </div>
      <p class="small muted" style="margin-top:16px">Everything runs on this computer. No account, no ads.</p>`;
    $app.querySelectorAll("[data-k]").forEach((el) => {
      el.onchange = async () => {
        const k = el.dataset.k;
        let val = el.type === "checkbox" ? el.checked : el.type === "number" ? (el.value === "" ? null : +el.value) : el.value;
        state.settings = await api("/api/settings", { method: "POST", body: JSON.stringify({ [k]: val }) });
        if (k === "theme") applyTheme(val);
      };
    });
  }

  // ---------------------------------------------------------------- router
  async function route() {
    if (es && !location.hash.startsWith("#/running")) { es.close(); es = null; }
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    try {
      if (parts[0] === "running") return running();
      if (parts[0] === "result") return await result(+parts[1]);
      if (parts[0] === "details") return await details(+parts[1], parts[2]);
      if (parts[0] === "history") return await history();
      if (parts[0] === "report") return await report(+parts[1]);
      if (parts[0] === "settings") return await settings();
      return await home();
    } catch (e) {
      $app.innerHTML = `<div class="error">Couldn't load this page: ${esc(e.message)}. Is NetDoctor still running?</div>`;
    } finally {
      a11yFocus = true;
      window.scrollTo(0, 0);
    }
  }

  window.addEventListener("hashchange", route);
  api("/api/state").then((s) => { state.settings = s.settings; applyTheme(s.settings.theme); }).catch(() => {}).finally(route);
})();
