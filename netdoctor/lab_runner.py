"""Fault-injection lab (simulated).

Builds synthetic measurement files for each fault scenario in PROJECT.md §15,
runs them through the (pure) engine and scores accuracy. Because the engine
never touches the network, a simulated run is diagnosed exactly like a real one.

The real-network version (Linux namespaces + tc netem) lives in lab/netns/.

  netdoctor lab          print the score table
  pytest                 the same scenarios as unit tests
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .engine import diagnose
from .models.run import Measurement, Run
from .probes.util import loss_of, summarize

ISP_VERDICTS = {"isp", "no_internet"}
ASN = 64500


@dataclass
class Net:
    """Knobs describing a simulated home network."""
    link: str = "wifi"
    gw_base: float = 2.5
    gw_jitter: float = 0.6
    gw_spike_rate: float = 0.0
    gw_spike_ms: float = 0.0
    gw_loss: float = 0.0
    net_base: float = 18.0
    net_jitter: float = 1.0
    net_busy_ms: float = 0.0          # random queueing already present at idle
    bloat_dl: float = 3.0             # extra internet delay under load, beyond any router queue
    bloat_ul: float = 5.0
    gw_bloat_dl: float = 0.5          # extra router delay under load
    gw_bloat_ul: float = 0.5
    net_dead: bool = False
    loss_hop: int | None = None       # 1-based index into the path where loss starts
    loss_rate: float = 0.0
    loss_targets: tuple = ("1.1.1.1", "8.8.8.8")
    middle_loss_hop: int | None = None  # rate-limited hop (harmless)
    dns_sys_ms: float = 15.0
    dns_sys_router: bool = False
    dns_public_ms: float = 14.0
    control_ttfb: float = 150.0
    target: str | None = None
    target_ttfb: float = 160.0
    target_error: str | None = None
    mtu_black_hole: bool = False
    ipv6: str = "ok"                  # ok / broken / none
    rssi: int = -52
    channel: int = 36
    neighbours: int = 1
    double_nat: bool = False
    captive: bool = False
    dl_mbps: float = 200.0
    ul_mbps: float = 40.0
    seed: int = 1


@dataclass
class Scenario:
    name: str
    net: Net
    expected: set[str]
    note: str = ""
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
def _samples(rng, n, base, jitter, spike_rate=0.0, spike_ms=0.0, loss=0.0, busy=0.0):
    out = []
    for _ in range(n):
        if rng.random() < loss:
            out.append(None)
            continue
        v = base + abs(rng.gauss(0, jitter))
        if busy:
            v += rng.uniform(0, busy)
        if spike_rate and rng.random() < spike_rate:
            v += rng.uniform(spike_ms * 0.4, spike_ms)
        out.append(round(v, 2))
    return out


def build_run(n: Net) -> Run:
    rng = random.Random(n.seed)
    gw_ip = "192.168.1.1"
    run = Run(tool_version="lab", options={"site": n.target, "load": True})
    run.context = {"os": "lab", "link": n.link, "vpn": False, "gateway": gw_ip,
                   "dns_servers": [gw_ip if n.dns_sys_router else "81.2.69.53"],
                   "captive_portal": n.captive, "online": not n.net_dead, "ipv6": n.ipv6 != "none",
                   "double_nat": n.double_nat, "cgnat": False, "asn": ASN, "isp": "Lab ISP"}
    if n.captive:
        run.add(Measurement(probe="icmp", target="gateway", phase="idle",
                            samples_ms=_samples(rng, 10, n.gw_base, n.gw_jitter)))
        run.verdict = diagnose(run)
        return run

    gw_extra = {"idle": 0, "download": n.gw_bloat_dl, "upload": n.gw_bloat_ul}
    net_extra = {"idle": 0, "download": n.bloat_dl, "upload": n.bloat_ul}
    counts = {"idle": 60, "download": 40, "upload": 40}
    for phase, cnt in counts.items():
        g = _samples(rng, cnt, n.gw_base + gw_extra[phase], n.gw_jitter + gw_extra[phase] * 0.15,
                     n.gw_spike_rate, n.gw_spike_ms, n.gw_loss)
        run.add(Measurement(probe="icmp", target="gateway", phase=phase, samples_ms=g, loss=loss_of(g),
                            meta={"host": gw_ip}))
        if n.net_dead:
            s = [None] * cnt
        else:
            base = n.net_base + net_extra[phase] + (n.gw_base + gw_extra[phase] - n.gw_base)
            path_loss = n.loss_rate if (n.loss_hop and "1.1.1.1" in n.loss_targets) else 0.0
            s = _samples(rng, cnt, base, n.net_jitter + net_extra[phase] * 0.15, n.gw_spike_rate,
                         n.gw_spike_ms, min(1.0, n.gw_loss + path_loss),
                         n.net_busy_ms if phase == "idle" else 0.0)
        run.add(Measurement(probe="icmp", target="internet", phase=phase, samples_ms=s, loss=loss_of(s),
                            meta={"host": "1.1.1.1"}))
        if phase == "idle":
            pl2 = n.loss_rate if (n.loss_hop and "8.8.8.8" in n.loss_targets) else 0.0
            s2 = [None] * cnt if n.net_dead else _samples(rng, cnt, n.net_base + 1, n.net_jitter, n.gw_spike_rate,
                                                           n.gw_spike_ms, min(1.0, n.gw_loss + pl2), n.net_busy_ms)
            run.add(Measurement(probe="icmp", target="internet2", phase="idle", samples_ms=s2, loss=loss_of(s2),
                                meta={"host": "8.8.8.8"}))

    if True:
        for d, mbps in (("download", n.dl_mbps), ("upload", n.ul_mbps)):
            run.add(Measurement(probe="load", phase=d, meta={"mbps": 0.0 if n.net_dead else mbps, "seconds": 5,
                                                              "failed": n.net_dead, "errors": [], "series": []}))

    # Route: home, [double NAT], 2 ISP hops, 2 internet hops, destination.
    for target in ("1.1.1.1", "8.8.8.8"):
        path = [(gw_ip, "home", n.gw_base)]
        if n.double_nat:
            path.append(("192.168.0.1", "home", n.gw_base + 0.8))
        path += [("198.51.100.1", "isp", n.gw_base + 5), ("198.51.100.9", "isp", n.gw_base + 7),
                 ("203.0.113.70", "internet", n.net_base - 4), ("203.0.113.90", "internet", n.net_base - 2),
                 (target, "internet", n.net_base)]
        hops = []
        for i, (ip, kind, base) in enumerate(path, start=1):
            loss = n.gw_loss
            if n.loss_hop and i >= n.loss_hop + (1 if n.double_nat else 0) and target in n.loss_targets:
                loss += n.loss_rate
            if n.middle_loss_hop and i == n.middle_loss_hop:
                loss += 0.4
            if n.net_dead and kind != "home":
                loss = 1.0
            s = _samples(rng, 20, base, n.gw_jitter, n.gw_spike_rate, n.gw_spike_ms, min(1.0, loss))
            hops.append({"n": i, "ip": ip, "kind": kind, "asn": ASN if kind == "isp" else 13335,
                         "samples_ms": s, "loss": loss_of(s), "stats": summarize(s),
                         **({"double_nat": True} if (n.double_nat and i == 2) else {})})
        run.add(Measurement(probe="trace", target=target, samples_ms=hops[-1]["samples_ms"],
                            loss=hops[-1]["loss"], meta={"tool": "lab", "hops": hops}))

    def dns_samples(ms):
        return [round(ms * rng.uniform(0.85, 1.2), 1) for _ in range(5)]

    sys_ip = gw_ip if n.dns_sys_router else "81.2.69.53"
    run.add(Measurement(probe="dns", target=sys_ip, phase="idle", samples_ms=dns_samples(n.dns_sys_ms),
                        meta={"label": "Your DNS", "system": True, "is_gateway": n.dns_sys_router}))
    for label, ip, f in (("Cloudflare", "1.1.1.1", 1.0), ("Google", "8.8.8.8", 1.3), ("Quad9", "9.9.9.9", 1.5)):
        run.add(Measurement(probe="dns", target=ip, phase="idle", samples_ms=dns_samples(n.dns_public_ms * f),
                            meta={"label": label, "system": False}))
    run.add(Measurement(probe="dns_intercept", meta={"intercepted": False}))

    for host in ("www.google.com", "www.cloudflare.com", "www.wikipedia.org"):
        run.add(Measurement(probe="http", target=host, phase="idle",
                            meta={"role": "control", "host": host, "ttfb_ms": round(n.control_ttfb * rng.uniform(0.8, 1.3))}))
    if n.target:
        meta = {"role": "target", "host": n.target}
        if n.target_error:
            meta["error"] = n.target_error
        else:
            meta["ttfb_ms"] = n.target_ttfb
        run.add(Measurement(probe="http", target=n.target, phase="idle", meta=meta))

    run.add(Measurement(probe="mtu", target="1.1.1.1", meta={
        "max_payload": 1372 if n.mtu_black_hole else 1472, "path_mtu": 1400 if n.mtu_black_hole else 1500,
        "frag_needed_seen": False, "black_hole": n.mtu_black_hole}))
    if n.ipv6 == "none":
        v6 = {"configured": False}
    else:
        v6 = {"configured": True, "v4_ms": 20.0, "v6_ms": None if n.ipv6 == "broken" else 21.0,
              "v6_error": "timeout" if n.ipv6 == "broken" else None, "broken": n.ipv6 == "broken", "slow": False}
    run.add(Measurement(probe="ipv6", meta=v6))

    if n.link == "wifi":
        from .wifi import analyse_channels, band_of

        nets = [{"ssid": f"n{i}", "channel": n.channel, "band": band_of(n.channel)} for i in range(n.neighbours)]
        quiet = 11 if n.channel <= 14 and n.channel != 11 else 1 if n.channel <= 14 else 149
        nets.append({"ssid": "far", "channel": quiet, "band": band_of(quiet)})
        meta = {"available": True, "ssid": "Home", "rssi_dbm": n.rssi, "noise_dbm": -92,
                "snr_db": n.rssi + 92, "channel": n.channel, "band": band_of(n.channel), "networks": nets}
        meta.update(analyse_channels(meta))
        run.add(Measurement(probe="wifi", meta=meta))

    run.duration_s = 30
    run.verdict = diagnose(run)
    return run


# ---------------------------------------------------------------------------
SCENARIOS: list[Scenario] = [
    Scenario("Clean network", Net(), {"none"}),
    Scenario("Large upload queue at ISP", Net(bloat_ul=260, bloat_dl=40), {"isp_bufferbloat"}),
    Scenario("Queue at local access point", Net(bloat_ul=15, gw_bloat_ul=175, bloat_dl=8, gw_bloat_dl=50), {"local_queue"}),
    Scenario("Loss starting at first ISP hop", Net(loss_hop=2, loss_rate=0.08, link="wired"), {"isp"}),
    Scenario("Loss at a middle hop only (harmless)", Net(middle_loss_hop=4), {"none"}, "must not blame ISP"),
    Scenario("Slow configured resolver", Net(dns_sys_ms=320, dns_public_ms=15), {"dns"}),
    Scenario("Slow router DNS proxy", Net(dns_sys_ms=260, dns_sys_router=True, dns_public_ms=15), {"router"}),
    Scenario("Slow target website", Net(target="slow.example", target_ttfb=4200), {"website"}),
    Scenario("Background traffic from another device", Net(net_busy_ms=140, bloat_dl=20, bloat_ul=30), {"busy_network"}),
    Scenario("Double NAT", Net(double_nat=True), {"router"}, "minor"),
    Scenario("Packets above 1400 bytes dropped silently", Net(mtu_black_hole=True), {"mtu"}),
    Scenario("IPv6 timing out", Net(ipv6="broken"), {"ipv6"}),
    Scenario("Fast, clean gigabit link", Net(link="wired", dl_mbps=940, ul_mbps=900, net_base=6, bloat_dl=2, bloat_ul=2), {"none"},
             "catches self-inflicted lag"),
    Scenario("Intermittent fault", Net(loss_hop=5, loss_rate=0.05, loss_targets=("1.1.1.1",), gw_spike_rate=0.05, gw_spike_ms=25),
             {"inconclusive", "none"}, "inconclusive or watch-mode suggestion"),
    # Extra Wi-Fi and connectivity scenarios.
    Scenario("Weak Wi-Fi signal with unstable link", Net(rssi=-77, gw_spike_rate=0.25, gw_spike_ms=180, gw_loss=0.03), {"wifi"}),
    Scenario("Crowded 2.4 GHz channel", Net(rssi=-62, channel=6, neighbours=9, gw_spike_rate=0.2, gw_spike_ms=120), {"wifi"}),
    Scenario("Internet outage (router fine)", Net(net_dead=True), {"no_internet"}),
    Scenario("Captive portal", Net(captive=True), {"captive"}),
]


def evaluate() -> list[dict]:
    results = []
    for sc in SCENARIOS:
        v = build_run(sc.net).verdict
        results.append({
            "name": sc.name, "expected": sorted(sc.expected), "got": v["culprit"],
            "confidence": v["confidence_label"], "ok": v["culprit"] in sc.expected,
            "false_isp": v["culprit"] in ISP_VERDICTS and not (sc.expected & ISP_VERDICTS),
        })
    return results


def main() -> int:
    from rich.console import Console
    from rich.table import Table

    res = evaluate()
    t = Table(title="NetDoctor fault-injection lab (simulated)", title_justify="left")
    for c in ("scenario", "expected", "verdict", "confidence", ""):
        t.add_column(c)
    for r in res:
        t.add_row(r["name"], " / ".join(r["expected"]), r["got"], r["confidence"],
                  "[green]✓[/green]" if r["ok"] else "[red]✗[/red]")
    c = Console()
    c.print(t)
    acc = sum(r["ok"] for r in res) / len(res)
    false_isp = sum(r["false_isp"] for r in res)
    c.print(f"Accuracy: [bold]{acc * 100:.0f}%[/bold] (target ≥ 90%)   False ISP blame: [bold]{false_isp}[/bold] (must be 0)")
    return 0 if acc >= 0.9 and false_isp == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
