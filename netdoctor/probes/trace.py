"""Route trace + MTR-style per-hop statistics.

1. Discover hops with the system traceroute (unprivileged on macOS/Linux/Windows).
2. Classify each hop: home, ISP (same network as your public IP, or CGNAT), internet.
3. Ping every responding hop many times at once to get per-hop delay and loss.
"""
from __future__ import annotations

import asyncio
import re
import shutil

from ..models.run import Measurement
from .context import asn_lookup
from .ping import Pinger
from .util import IS_WINDOWS, ip_kind, loss_of, run_cmd, summarize

IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
MS_RE = re.compile(r"<?(\d+(?:\.\d+)?)\s*ms")


def parse_traceroute(out: str) -> list[dict]:
    """Parse traceroute / tracert / tracepath output into [{n, ip, rtt_ms}]."""
    hops: dict[int, dict] = {}
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)[:?]?\s+(.*)$", line)
        if not m:
            continue
        n = int(m.group(1))
        rest = m.group(2)
        if n > 64 or "traceroute to" in line.lower():
            continue
        ipm = IP_RE.search(rest)
        rtts = [float(x) for x in MS_RE.findall(rest)]
        if n in hops and hops[n]["ip"]:
            continue  # tracepath prints a hop several times; keep the first answer
        hops[n] = {"n": n, "ip": ipm.group(1) if ipm else None, "rtt_ms": rtts[0] if rtts else None}
    return [hops[k] for k in sorted(hops)]


async def discover_hops(target: str, max_hops: int = 20) -> tuple[list[dict], str]:
    if IS_WINDOWS:
        _, out = await run_cmd("tracert", "-d", "-h", str(max_hops), "-w", "800", target, timeout=12)
        return parse_traceroute(out), "tracert"
    if shutil.which("traceroute"):
        _, out = await run_cmd(
            "traceroute", "-n", "-q", "1", "-w", "1", "-m", str(max_hops), target, timeout=9
        )
        return parse_traceroute(out), "traceroute"
    if shutil.which("tracepath"):
        _, out = await run_cmd("tracepath", "-n", "-m", str(max_hops), target, timeout=9)
        return parse_traceroute(out), "tracepath"
    return [], "none"


async def classify_hops(hops: list[dict], gateway: str | None, client_asn: int | None, offline: bool,
                        isp_prefixes: list[str] | None = None) -> None:
    """Tag each hop with kind: home / isp / internet / silent."""
    import ipaddress

    nets = [ipaddress.ip_network(p, strict=False) for p in (isp_prefixes or [])]
    asns: dict[str, int | None] = {}
    public_ips = [h["ip"] for h in hops if h["ip"] and ip_kind(h["ip"]) == "public"]
    if client_asn and not offline and public_ips:
        found = await asyncio.gather(*(asn_lookup(ip, timeout=1.2) for ip in public_ips))
        asns = dict(zip(public_ips, found))

    seen_public = 0
    prev = "home"
    left_home = False
    for h in hops:
        ip = h["ip"]
        kind = ip_kind(ip)
        h["asn"] = asns.get(ip) if ip else None
        if not ip:
            h["kind"] = "silent"
            continue
        if not left_home and (ip == gateway or (h["n"] == 1 and kind == "private")):
            h["kind"] = "home"
        elif not left_home and kind == "private" and ip.startswith("192.168."):
            # A second 192.168.x.x router right behind yours: double NAT.
            h["kind"] = "home"
            h["double_nat"] = True
        elif kind == "cgnat":
            h["kind"] = "isp"
            h["cgnat"] = True
            left_home = True
        elif kind == "public":
            left_home = True
            seen_public += 1
            if nets:
                h["kind"] = "isp" if any(ipaddress.ip_address(ip) in n for n in nets) else "internet"
            elif client_asn and h["asn"]:
                h["kind"] = "isp" if h["asn"] == client_asn else "internet"
            elif client_asn and h["asn"] is None and prev in ("isp", "home"):
                h["kind"] = "isp"
            elif not client_asn:
                h["kind"] = "isp" if seen_public <= 2 else "internet"
            else:
                h["kind"] = prev if prev != "home" else "isp"
        else:
            # Private address after leaving home: ISP or carrier internal network.
            left_home = True
            h["kind"] = prev if prev != "home" else "isp"
        prev = h["kind"]


async def trace(target: str, pinger: Pinger, gateway: str | None, client_asn: int | None,
                offline: bool = False, samples: int = 25, isp_prefixes: list[str] | None = None) -> Measurement:
    hops, tool = await discover_hops(target)
    if not hops:
        return Measurement(probe="trace", target=target, meta={"tool": tool, "hops": [], "error": "traceroute unavailable"})
    # Make sure the destination itself is the last hop.
    if not any(h["ip"] == target for h in hops):
        hops.append({"n": (hops[-1]["n"] + 1) if hops else 1, "ip": target, "rtt_ms": None})
    await classify_hops(hops, gateway, client_asn, offline, isp_prefixes)
    # Destination is always "internet" (or isp if it's inside the ISP).
    if hops[-1]["ip"] == target and hops[-1].get("kind") == "home":
        hops[-1]["kind"] = "internet"

    responding = [h for h in hops if h["ip"]]
    results = await asyncio.gather(*(pinger.series(h["ip"], count=samples, interval=0.1) for h in responding))
    for h, s in zip(responding, results):
        h["samples_ms"] = s
        h["loss"] = loss_of(s)
        h["stats"] = summarize(s)
    final = hops[-1]
    return Measurement(
        probe="trace",
        target=target,
        samples_ms=final.get("samples_ms"),
        loss=final.get("loss"),
        meta={"tool": tool, "hops": hops},
    )
