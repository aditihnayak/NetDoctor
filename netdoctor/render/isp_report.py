"""ISP report: a timestamped evidence summary written for a support agent."""
from __future__ import annotations

import html
from datetime import datetime

from ..engine import extract
from ..models.run import Run


def _fmt(x, unit="ms", nd=0):
    return "n/a" if x is None else f"{x:.{nd}f} {unit}"


def isp_report_lines(run: Run, plan_down: float | None = None, plan_up: float | None = None,
                     watch_summary: dict | None = None) -> list[tuple[str, list[str]]]:
    F = extract(run)
    v = run.verdict or {}
    ctx = run.context
    sections: list[tuple[str, list[str]]] = []

    sections.append(("Summary", [
        f"Test time: {run.started_at}",
        f"Provider: {ctx.get('isp') or 'unknown'}" + (f" (AS{ctx['asn']})" if ctx.get("asn") else ""),
        f"Connection: {ctx.get('link', 'unknown')}" + (" via VPN" if ctx.get("vpn") else ""),
        f"Finding: {v.get('title', 'n/a')} ({v.get('confidence_label', 'n/a')} confidence)",
        *(f"- {e}" for e in v.get("evidence_text", [])),
    ]))

    gw, net = F["gw"]["idle"], F["net"]["idle"]
    lines = []
    if gw:
        lines.append(f"Home router ({ctx.get('gateway')}): median {_fmt(gw['median'], nd=1)}, "
                     f"loss {gw['loss']*100:.1f}%. The home network is "
                     + ("healthy." if gw["loss"] < 0.01 and (gw["p95"] or 0) - (gw["median"] or 0) < 20 else "NOT fully healthy."))
    if net:
        lines.append(f"Internet ({(run.first('icmp', 'internet') or run.first('tcp_ping', 'internet')).meta.get('host') if net else ''}): "
                     f"median {_fmt(net['median'], nd=1)}, p95 {_fmt(net['p95'], nd=1)}, loss {net['loss']*100:.1f}%")
    if lines:
        sections.append(("Latency at idle", lines))

    for tr in F["traces"]:
        rows = []
        for h in tr["hops"]:
            s = h.get("stats") or {}
            mark = ""
            if (tr.get("loss_origin") or {}).get("n") == h["n"]:
                mark = "  <-- loss starts here"
            if (tr.get("latency_jump") or {}).get("n") == h["n"]:
                mark = f"  <-- delay jumps +{tr['latency_jump']['ms']:.0f} ms"
            rows.append(f"{h['n']:>2}  {h.get('ip') or '*':<16} {h.get('kind', ''):<9} "
                        f"{_fmt(s.get('median'), nd=1):>10}  loss {(h.get('loss') or 0)*100:>3.0f}%{mark}")
        rows.append("(Loss at a middle hop that does not continue to the end is normal router rate-limiting.)")
        sections.append((f"Route to {tr['target']}", rows))

    load = F["load"]
    if load:
        dl = (load.get("download") or {}).get("mbps")
        ul = (load.get("upload") or {}).get("mbps")
        rows = [f"Measured download: {_fmt(dl, 'Mbps', 1)}" + (f" (plan: {plan_down:.0f} Mbps, {dl / plan_down * 100:.0f}%)" if plan_down and dl else ""),
                f"Measured upload:   {_fmt(ul, 'Mbps', 1)}" + (f" (plan: {plan_up:.0f} Mbps, {ul / plan_up * 100:.0f}%)" if plan_up and ul else "")]
        if F["bloat_worst"] is not None:
            rows.append(f"Extra delay under load: +{F['bloat']['download'] or 0:.0f} ms downloading, "
                        f"+{F['bloat']['upload'] or 0:.0f} ms uploading (grade {F['grade']}).")
        sections.append(("Speed and delay under load", rows))

    if F["dns_system"]:
        d = F["dns_system"]
        rows = [f"Configured resolver {d['ip']}: {_fmt(d['median'])}, {(d['fail'] or 0)*100:.0f}% failed"]
        rows += [f"{p['label']} {p['ip']}: {_fmt(p['median'])}" for p in F["dns_public"]]
        if F["dns_intercepted"]:
            rows.append("DNS traffic to arbitrary addresses is answered (transparent DNS interception on the path).")
        sections.append(("DNS", rows))

    if watch_summary and watch_summary.get("count"):
        w = watch_summary
        sections.append(("Monitoring history", [
            f"{w['count']} checks between {w['first']} and {w['last']}",
            f"Checks with problems: {w['bad']} ({w['bad'] / w['count'] * 100:.0f}%)",
            *(f"Worst hours: {', '.join(w['worst_hours'])}" for _ in [0] if w.get("worst_hours")),
        ]))

    sections.append(("Requested action", [_ask(v.get("culprit"))]))
    return sections


def _ask(culprit: str | None) -> str:
    return {
        "isp": "Please investigate packet loss / latency inside your network starting at the hop marked above. "
               "My home network was verified healthy during the test.",
        "no_internet": "My router is reachable but there is no internet connectivity. Please check for an outage on my line.",
        "isp_bufferbloat": "Latency rises sharply when the line is loaded. Please check the modem's queue management / provisioning.",
        "internet": "Packet loss appears beyond your access network. Please check your upstream / peering towards the destinations above.",
        "dns": "Your DNS servers respond slowly compared to public resolvers. Please check resolver health.",
        "ipv6": "IPv6 connections fail while IPv4 works. Please check IPv6 provisioning on my line.",
    }.get(culprit or "", "For your information: measurements from my connection are attached above.")


def isp_report_text(run: Run, **kw) -> str:
    out = ["NetDoctor connection report", "=" * 27, f"Generated {datetime.now().astimezone().isoformat(timespec='seconds')}", ""]
    for title, rows in isp_report_lines(run, **kw):
        out += [title, "-" * len(title), *rows, ""]
    return "\n".join(out)


def isp_report_html(run: Run, **kw) -> str:
    body = []
    for title, rows in isp_report_lines(run, **kw):
        mono = title.startswith("Route")
        body.append(f"<h2>{html.escape(title)}</h2>")
        if mono:
            body.append("<pre>" + html.escape("\n".join(rows)) + "</pre>")
        else:
            body.append("<ul>" + "".join(f"<li>{html.escape(r)}</li>" for r in rows) + "</ul>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>NetDoctor ISP report</title>
<style>body{{font:14px/1.5 -apple-system,Segoe UI,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#111}}
h1{{font-size:22px}}h2{{font-size:15px;margin-top:24px;border-bottom:1px solid #ddd}}pre{{font-size:12px;background:#f6f6f6;padding:8px;overflow:auto}}</style>
</head><body><h1>NetDoctor connection report</h1>{''.join(body)}</body></html>"""
