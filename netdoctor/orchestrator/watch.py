"""Watch mode: lightweight checks every few minutes, building a history.

Each check takes ~3 seconds and uses almost no data: 20 pings to the router
and the internet, one uncached DNS lookup and one website timing. No load test.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Callable

from ..config import Config
from ..probes.context import probe_context
from ..probes.dns import time_resolver
from ..probes.http import time_site
from ..probes.ping import Pinger
from ..probes.util import summarize


def quality_score(s: dict) -> tuple[int, str | None]:
    """0-100 score and the main issue (None if fine)."""
    score = 100.0
    issues = []
    lat, jit, loss = s.get("internet_ms"), s.get("jitter_ms") or 0, s.get("loss") or 0
    if lat is None:
        return 0, "No internet connection"
    score -= max(0, lat - 40) * 0.25
    score -= max(0, jit - 5) * 0.8
    score -= loss * 400
    if s.get("router_ms") is not None and s["router_ms"] > 20:
        score -= (s["router_ms"] - 20) * 0.5
        issues.append(f"Slow link to router ({s['router_ms']:.0f} ms)")
    if s.get("dns_ms") is None:
        score -= 15
        issues.append("DNS not answering")
    elif s["dns_ms"] > 150:
        score -= 10
        issues.append(f"Slow DNS ({s['dns_ms']:.0f} ms)")
    if s.get("web_ms") is None:
        score -= 15
        issues.append("Website test failed")
    if loss >= 0.05:
        issues.insert(0, f"Packet loss {loss * 100:.0f}%")
    if jit > 30:
        issues.insert(0, f"Unstable delay (jitter {jit:.0f} ms)")
    if lat > 150:
        issues.insert(0, f"High delay ({lat:.0f} ms)")
    score = int(max(0, min(100, round(score))))
    return score, (issues[0] if issues and score < 75 else None)


async def watch_once(cfg: Config | None = None, gateway: str | None = None) -> dict:
    cfg = cfg or Config()
    if gateway is None:
        gateway = (await probe_context(cfg)).get("gateway")
    pinger = Pinger()
    await pinger.detect(cfg.ping_targets[0])
    jobs = [pinger.series(cfg.ping_targets[0], 20, 0.1)]
    if gateway:
        jobs.append(pinger.series(gateway, 20, 0.1))
    import dns.resolver

    system_dns = next((n for n in dns.resolver.Resolver().nameservers if ":" not in str(n)), "1.1.1.1")
    jobs.append(time_resolver(str(system_dns), cfg.dns_probe_domain, 1))
    jobs.append(time_site(cfg.control_sites[0], "control", timeout=5, max_bytes=50_000))
    res = await asyncio.gather(*jobs)
    net = summarize(res[0])
    gw = summarize(res[1]) if gateway else None
    dns_ms = res[-2][0]
    web = res[-1].meta
    sample = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "router_ms": gw["median"] if gw else None,
        "internet_ms": net["median"],
        "jitter_ms": net["jitter"],
        "loss": net["loss"],
        "dns_ms": dns_ms,
        "web_ms": None if web.get("error") else web.get("ttfb_ms"),
    }
    sample["score"], sample["issue"] = quality_score(sample)
    return sample


class Watcher:
    """Background watch loop (used by the desktop app)."""

    def __init__(self, store, interval_min: float = 5, on_sample: Callable[[dict], None] | None = None):
        self.store, self.interval_min, self.on_sample = store, interval_min, on_sample
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="netdoctor-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                s = asyncio.run(watch_once())
                self.store.add_watch(s)
                self.last = s
                if self.on_sample:
                    self.on_sample(s)
            except Exception:
                pass
            self._stop.wait(self.interval_min * 60)
