"""Terminal renderers: the plain verdict and the technical details report."""
from __future__ import annotations

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from ..engine import SEGMENT_NAMES, SEGMENTS, extract
from ..models.run import Run

ICON = {"good": "✓", "fair": "⚠", "poor": "✗", "unknown": "?"}
STYLE = {"good": "green", "fair": "yellow", "poor": "red", "unknown": "dim"}
CONF_STYLE = {"High": "bold", "Medium": "", "Low": "dim", "Inconclusive": "dim"}
SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float | None], width: int = 48) -> str:
    vals = [v for v in values]
    if not vals:
        return ""
    if len(vals) > width:  # bucket by max so spikes stay visible
        step = len(vals) / width
        vals = [max((x for x in vals[int(i * step):int((i + 1) * step)] if x is not None), default=None)
                for i in range(width)]
    good = [v for v in vals if v is not None]
    if not good:
        return "·" * len(vals)
    lo, hi = min(good), max(good)
    span = (hi - lo) or 1
    return "".join("×" if v is None else SPARK[min(7, int((v - lo) / span * 7.999))] for v in vals)


def verdict_text(run: Run) -> Group:
    v = run.verdict or {}
    parts: list = []
    culprit = v.get("culprit")
    title = v.get("title", "")
    if culprit == "none":
        head = Text("✓ ", style="green bold") + Text(title, style="bold")
    elif culprit == "inconclusive":
        head = Text("? ", style="bold") + Text(title, style="bold")
    else:
        head = Text("Most likely cause: ", style="bold") + Text(_cause_name(v), style="bold red")
    label = v.get("confidence_label", "")
    head += Text(f"  ({label.lower()} confidence)" if label != "Inconclusive" else "", style=CONF_STYLE.get(label, ""))
    parts.append(head)
    if culprit not in ("none", "inconclusive") and title:
        parts.append(Text(title + ".", style="bold"))
    if v.get("message"):
        parts.append(Text(v["message"], style="dim"))
    parts.append(Text(""))

    t = Table.grid(padding=(0, 2))
    t.add_column(width=1)
    t.add_column(width=9)
    t.add_column()
    for key in SEGMENTS:
        s = v.get("segments", {}).get(key, {"status": "unknown", "summary": ""})
        st = s["status"]
        t.add_row(Text(ICON[st], style=STYLE[st]), Text(SEGMENT_NAMES[key], style="bold"), s["summary"])
    parts.append(t)

    k = v.get("key_numbers", {})
    nums = []
    if k.get("download_mbps") is not None:
        nums.append(f"{k['download_mbps']:.0f} Mbps ↓  {k.get('upload_mbps') or 0:.0f} Mbps ↑")
    if k.get("lag_grade"):
        nums.append(f"Lag grade {k['lag_grade']}")
    if k.get("wifi_rssi_dbm") is not None:
        nums.append(f"Wi-Fi {k['wifi_rssi_dbm']} dBm ({k['wifi_label']})")
    if k.get("dns_ms") is not None:
        nums.append(f"DNS {k['dns_ms']:.0f} ms ({k['dns_label']})")
    if nums:
        parts += [Text(""), Text("   ·   ".join(nums), style="cyan")]

    if v.get("actions"):
        parts += [Text(""), Text("What to do", style="bold")]
        for i, a in enumerate(v["actions"], 1):
            parts.append(Text(f"  {i}. {a['text']}"))
    if v.get("also"):
        parts += [Text(""), Text("Also noticed", style="bold")]
        for a in v["also"][:3]:
            parts.append(Text(f"  • {a['title']}", style="yellow"))
    return Group(*parts)


def _cause_name(v: dict) -> str:
    return {
        "wifi": "your Wi-Fi", "local_queue": "your Wi-Fi / router under load", "router": "your router",
        "isp": "your internet provider", "isp_bufferbloat": "lag when your connection is busy",
        "dns": "slow DNS", "mtu": "lost large packets (MTU)", "ipv6": "broken IPv6",
        "busy_network": "another device using the connection", "website": "the website itself",
        "website_missing": "the website address", "internet": "the wider internet",
        "captive": "a network sign-in page", "offline": "no network connection",
        "no_internet": "an internet outage",
    }.get(v.get("culprit"), v.get("culprit", "?"))


def details_text(run: Run) -> Group:
    """Full technical report: every measurement, per-hop table, raw values."""
    F = extract(run)
    v = run.verdict or {}
    ctx = run.context
    out: list = []

    def h(title: str) -> None:
        out.append(Text(""))
        out.append(Text(title, style="bold underline"))

    h("Context")
    t = Table.grid(padding=(0, 2))
    for key in ("link", "iface", "gateway", "vpn", "isp", "asn", "public_ip", "colo", "dns_servers",
                "captive_portal", "double_nat", "cgnat", "ipv6", "os"):
        if key in ctx:
            t.add_row(Text(key, style="dim"), str(ctx[key]))
    out.append(t)

    h("Delay (ms)  ·  median / p95 / max  ·  loss")
    t = Table(box=None, pad_edge=False)
    for c in ("target", "phase", "median", "p95", "max", "jitter", "loss"):
        t.add_column(c, justify="right" if c not in ("target", "phase") else "left")
    for key, label in (("gw", "router"), ("net", "internet")):
        for phase, s in F[key].items():
            if s:
                t.add_row(label, phase, *(f"{s[x]:.1f}" if s[x] is not None else "-" for x in ("median", "p95", "max", "jitter")),
                          f"{s['loss']*100:.1f}%")
    out.append(t)
    if F["bloat_worst"] is not None:
        out.append(Text(f"Extra delay under load: download +{F['bloat']['download'] or 0:.0f} ms, "
                        f"upload +{F['bloat']['upload'] or 0:.0f} ms → grade {F['grade']}  "
                        f"(at router: +{F['local_bloat']['download'] or 0:.0f} / +{F['local_bloat']['upload'] or 0:.0f} ms)"))
    tl = run.first("timeline")
    if tl:
        for key in tl.meta.get("targets", {}):
            pts = [p[2] for p in tl.meta["points"] if p[1] == key]
            out.append(Text(f"{key:>9} ", style="dim") + Text(sparkline(pts), style="cyan"))

    for m in run.find("load"):
        errs = f"  errors: {', '.join(m.meta['errors'])}" if m.meta.get("errors") else ""
        out.append(Text(f"{m.phase:>9}: {m.meta['mbps']:.1f} Mbps over {m.meta['seconds']:.1f} s{errs}"))

    for tr in F["traces"]:
        h(f"Route to {tr['target']}")
        t = Table(box=None, pad_edge=False)
        for c in ("hop", "address", "segment", "median", "loss", "note"):
            t.add_column(c)
        for hop in tr["hops"]:
            s = hop.get("stats") or {}
            loss = hop.get("loss")
            note = ""
            if hop["n"] in tr.get("harmless_loss_hops", []):
                note = "loss here doesn't continue: harmless"
            if (tr.get("loss_origin") or {}).get("n") == hop["n"]:
                note = "← loss starts here"
            if (tr.get("latency_jump") or {}).get("n") == hop["n"]:
                note = f"← delay jumps +{tr['latency_jump']['ms']:.0f} ms"
            t.add_row(str(hop["n"]), hop.get("ip") or "*", hop.get("kind", ""),
                      f"{s['median']:.1f}" if s.get("median") is not None else "-",
                      f"{loss*100:.0f}%" if loss is not None else "-",
                      Text(note, style="dim" if "harmless" in note else "yellow"))
        out.append(t)

    h("DNS (uncached lookups)")
    t = Table(box=None, pad_edge=False)
    for c in ("resolver", "address", "median ms", "failed"):
        t.add_column(c)
    for d in ([F["dns_system"]] if F["dns_system"] else []) + F["dns_public"]:
        t.add_row(d["label"], d["ip"], f"{d['median']:.0f}" if d["median"] is not None else "-", f"{(d['fail'] or 0)*100:.0f}%")
    out.append(t)
    if F["dns_intercepted"]:
        out.append(Text("⚠ DNS interception: a resolver address that doesn't exist answered. Your provider or router "
                        "redirects DNS traffic, so public-resolver timings may be of the same interceptor.", style="yellow"))

    h("Websites")
    t = Table(box=None, pad_edge=False)
    for c in ("site", "role", "dns", "connect", "tls", "first byte", "total", "status"):
        t.add_column(c)
    for meta in ([F["http_target"]] if F["http_target"] else []) + F["http_controls"]:
        f = lambda k: f"{meta[k]:.0f}" if meta.get(k) is not None else "-"  # noqa: E731
        t.add_row(meta.get("host", "?"), meta.get("role", ""), f("dns_ms"), f("connect_ms"), f("tls_ms"), f("ttfb_ms"),
                  f("total_ms"), str(meta.get("error") or meta.get("status", "")))
    out.append(t)

    if F["wifi"]:
        w = F["wifi"]
        h("Wi-Fi")
        out.append(Text(f"Signal {w.get('rssi_dbm')} dBm, noise {w.get('noise_dbm')} dBm, SNR {w.get('snr_db')} dB, "
                        f"rate {w.get('rate_mbps')} Mbps, channel {w.get('channel')} ({w.get('band')} GHz, "
                        f"{w.get('width_mhz') or '?'} MHz), {w.get('neighbours_on_channel', 0)} neighbours on/overlapping it."))
        pc = w.get("per_channel") or {}
        if pc:
            out.append(Text("Networks per channel: " + "  ".join(
                f"{'[' if str(w.get('channel')) == c else ''}{c}:{n}{']' if str(w.get('channel')) == c else ''}"
                for c, n in pc.items())))
        if w.get("best_channel"):
            out.append(Text(f"Quietest channel: {w['best_channel']} ({w.get('best_channel_neighbours')} neighbours)"))
    elif F["wifi_error"]:
        h("Wi-Fi")
        out.append(Text(f"Not available: {F['wifi_error']}", style="dim"))

    if F["mtu"]:
        h("MTU / IPv6")
        m = F["mtu"]
        out.append(Text(f"Largest unfragmented ping payload: {m.get('max_payload')} bytes (path MTU {m.get('path_mtu')}). "
                        f"\"Too big\" replies: {'yes' if m.get('frag_needed_seen') else 'no'}. "
                        f"Black hole: {'YES' if m.get('black_hole') else 'no'}."))
    if F["ipv6"]:
        i = F["ipv6"]
        if i.get("configured"):
            out.append(Text(f"IPv4 connect {i.get('v4_ms')} ms vs IPv6 {i.get('v6_ms') or i.get('v6_error')}."
                            + (" IPv6 is BROKEN." if i.get("broken") else "")))
        else:
            out.append(Text("IPv6: not configured on this network (that's fine)."))

    h("Diagnosis")
    out.append(Text(f"Verdict: {v.get('culprit')}  confidence {v.get('confidence')} ({v.get('confidence_label')})  "
                    f"coverage {v.get('coverage')}"))
    out.append(Text("Scores: " + ", ".join(f"{k} {s:.2f}" for k, s in sorted((v.get("scores") or {}).items(), key=lambda x: -x[1]))))
    for e in v.get("evidence_text", []):
        out.append(Text(f"  • {e}"))
    out.append(Text(f"\nRun took {run.duration_s:.1f} s · started {run.started_at}", style="dim"))
    return Group(*out)


def print_run(run: Run, details: bool = False, console: Console | None = None) -> None:
    c = console or Console()
    c.print(verdict_text(run))
    if details:
        c.print(details_text(run))
