"""Runs a checkup in timed phases within a ~30 second budget.

  0-2 s    Context      link type, gateway, DNS settings, VPN, captive portal
  2-10 s   Idle         ping router + internet, route trace, DNS, websites (Wi-Fi scan in background)
  10-24 s  Under load   download then upload (separate process) while delay is sampled every 100 ms
  24-30 s  Edge cases   MTU, IPv4/IPv6 race, Wi-Fi results, diagnosis

Idle measurements always finish before load starts so they aren't distorted.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .. import __version__
from ..config import Config
from ..engine import diagnose
from ..models.run import Measurement, Run
from ..probes.context import probe_context
from ..probes.dns import probe_dns
from ..probes.http import probe_http
from ..probes.ipv6 import probe_ipv6
from ..probes.mtu import probe_mtu
from ..probes.ping import Pinger
from ..probes.trace import trace
from ..probes.util import loss_of
from ..wifi import scan_wifi

Event = dict[str, Any]
EventFn = Callable[[Event], None]


class Sampler:
    """Pings router and internet every `interval` s, tagging samples with the current phase."""

    def __init__(self, pinger: Pinger, targets: dict[str, str], t0: float, emit: EventFn, interval: float = 0.1):
        self.pinger, self.targets, self.t0, self.emit, self.interval = pinger, targets, t0, emit, interval
        self.phase = "idle"
        self.points: list[list] = []
        self.phases: list[list] = []
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    def set_phase(self, phase: str) -> None:
        now = round(time.perf_counter() - self.t0, 2)
        if self.phases:
            self.phases[-1][2] = now
        self.phases.append([phase, now, None])
        self.phase = phase

    async def _one(self, key: str, host: str, phase: str) -> None:
        t = round(time.perf_counter() - self.t0, 2)
        ms = await self.pinger.once(host, timeout=1.0)
        self.points.append([t, key, ms, phase])
        self.emit({"type": "sample", "t": t, "target": key, "ms": ms, "phase": phase})

    async def _loop(self) -> None:
        while True:
            for key, host in self.targets.items():
                task = asyncio.create_task(self._one(key, host, self.phase))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self.set_phase("idle")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._pending:
            await asyncio.wait(self._pending, timeout=1.5)
        if self.phases:
            self.phases[-1][2] = round(time.perf_counter() - self.t0, 2)

    def measurements(self) -> list[Measurement]:
        out = []
        method = self.pinger.method or "icmp"
        for key, host in self.targets.items():
            for phase in ("idle", "download", "upload"):
                pts = sorted((p for p in self.points if p[1] == key and p[3] == phase), key=lambda p: p[0])
                if phase != "idle":
                    pts = [p for p in pts if p[0] >= self._phase_start(phase) + 0.8]  # skip ramp-up
                if not pts:
                    continue
                samples = [p[2] for p in pts]
                out.append(Measurement(probe="icmp" if method == "icmp" else "tcp_ping", target=key, phase=phase,
                                       samples_ms=samples, loss=loss_of(samples), meta={"host": host, "method": method}))
        out.append(Measurement(probe="timeline", meta={
            "points": [p[:3] for p in sorted(self.points)], "phases": self.phases, "targets": self.targets}))
        return out

    def _phase_start(self, phase: str) -> float:
        for name, a, _ in self.phases:
            if name == phase:
                return a
        return 0.0


async def run_checkup(site: str | None = None, load: bool = True, on_event: EventFn | None = None,
                      cfg: Config | None = None) -> Run:
    cfg = cfg or Config()
    emit: EventFn = on_event or (lambda e: None)
    t0 = time.perf_counter()
    run = Run(tool_version=__version__, options={"site": site, "load": load})
    pinger = Pinger()

    def step(segment: str, state: str, label: str | None = None) -> None:
        emit({"type": "step", "segment": segment, "state": state, "label": label,
              "t": round(time.perf_counter() - t0, 1)})

    def interim() -> None:
        """Run the (pure) engine on what we have so far to colour the path map live."""
        try:
            v = diagnose(run)
            emit({"type": "segments", "segments": v["segments"]})
        except Exception:
            pass

    # Start the load process early so its start-up cost isn't on the clock.
    loadgen = None
    if load:
        from ..probes.load import LoadGenerator

        loadgen = LoadGenerator(cfg)
        loadgen.start()

    wifi_task: asyncio.Task | None = None
    try:
        # ---- Phase 1: context -------------------------------------------
        emit({"type": "phase", "phase": "context", "label": "Looking at your connection"})
        step("wifi", "testing", "Checking your Wi-Fi")
        ctx = await probe_context(cfg)
        run.context = ctx
        emit({"type": "context", "context": {k: ctx.get(k) for k in ("link", "vpn", "gateway", "isp", "captive_portal")}})
        if ctx.get("link") != "wifi":
            step("wifi", "done")

        gateway = ctx.get("gateway")
        targets = {"internet": cfg.ping_targets[0]}
        if len(cfg.ping_targets) > 1:
            targets["internet2"] = cfg.ping_targets[1]  # second target: more loss evidence
        if gateway:
            targets = {"gateway": gateway, **targets}
            await pinger.detect(gateway)
        else:
            await pinger.detect(cfg.ping_targets[0])

        if ctx.get("captive_portal"):
            # No point testing further: everything is being redirected to a sign-in page.
            run.add(await pinger.measure(gateway or cfg.ping_targets[0], "gateway" if gateway else "internet", "idle", 10))
            return _finish(run, t0, emit)

        # ---- Phase 2: idle baseline ---------------------------------------
        emit({"type": "phase", "phase": "idle", "label": "Measuring your connection at rest"})
        sampler = Sampler(pinger, targets, t0, emit)
        sampler.start()
        step("router", "testing", "Checking your router")
        step("isp", "testing", "Checking your provider")
        step("internet", "testing", "Tracing the route")
        step("dns", "testing", "Timing name lookups")
        step("website", "testing", "Timing websites")

        async def traced(target: str) -> Measurement:
            return await trace(target, pinger, gateway, ctx.get("asn"), offline=cfg.offline_lab,
                               isp_prefixes=cfg.isp_prefixes)

        async def dns_job():
            r = await probe_dns(cfg, ctx.get("dns_servers") or [], gateway)
            step("dns", "done")
            return r

        async def http_job():
            r = await probe_http(cfg.control_sites, site)
            if site and cfg.server_url:
                r.append(await _server_view(cfg, site))
            step("website", "done")
            return r

        idle_jobs = [asyncio.gather(*(traced(t) for t in cfg.ping_targets[:2])), dns_job(), http_job()]
        try:
            traces, dns_ms, http_ms = await asyncio.wait_for(asyncio.gather(*idle_jobs), timeout=11)
        except asyncio.TimeoutError:
            traces, dns_ms, http_ms = [], [], []
            emit({"type": "warning", "text": "Some idle tests took too long and were skipped."})
        for m in [*traces, *dns_ms, *http_ms]:
            run.add(m)
        for t in traces:
            for h in t.meta.get("hops", []):
                if h.get("double_nat"):
                    run.context["double_nat"] = True
                if h.get("cgnat"):
                    run.context["cgnat"] = True
        if run.context.get("double_nat") is None:
            run.context["double_nat"] = False
        # Make sure we have at least ~6 s of idle samples.
        idle_elapsed = time.perf_counter() - t0 - sampler.phases[0][1]
        if idle_elapsed < 6:
            await asyncio.sleep(6 - idle_elapsed)
        for seg in ("router", "isp", "internet"):
            step(seg, "done")
        run.measurements = [m for m in run.measurements if m.probe != "timeline"] + sampler.measurements()
        interim()

        # ---- Phase 3: under load ----------------------------------------
        if loadgen:
            emit({"type": "phase", "phase": "load", "label": "Testing under load",
                  "note": "Your network may feel slower for a few seconds."})
            for direction in ("download", "upload"):
                sampler.set_phase(direction)
                step("isp", "testing", f"Testing under load: {direction}ing")
                m = await loadgen.run(direction, on_tick=lambda mbps, d=direction: emit(
                    {"type": "throughput", "direction": d, "mbps": round(mbps, 1)}))
                run.add(m)
                emit({"type": "load_done", "direction": direction, "mbps": m.meta["mbps"]})
            sampler.set_phase("after")
        await sampler.stop()
        run.measurements = [m for m in run.measurements if m.probe not in ("icmp", "tcp_ping", "timeline")]
        run.measurements += sampler.measurements()
        step("isp", "done")

        # ---- Phase 4: edge cases ----------------------------------------
        # The Wi-Fi scan runs last: scanning makes the radio hop channels, which
        # adds 100+ ms spikes to every delay sample taken at the same time.
        emit({"type": "phase", "phase": "edge", "label": "Checking edge cases"})
        if run.context.get("link") == "wifi":
            wifi_task = asyncio.create_task(scan_wifi())
        mtu_m, v6_m = await asyncio.gather(probe_mtu(cfg.ping_targets[0]), probe_ipv6(cfg.dualstack_host))
        run.add(mtu_m)
        run.add(v6_m)
        if wifi_task:
            try:
                wifi_m = await asyncio.wait_for(wifi_task, timeout=8)
            except asyncio.TimeoutError:
                wifi_m = Measurement(probe="wifi", meta={"available": False, "error": "scan_timeout"})
            run.add(wifi_m)
        step("wifi", "done")
        return _finish(run, t0, emit)
    finally:
        if loadgen:
            loadgen.close()
        if wifi_task and not wifi_task.done():
            wifi_task.cancel()


def _finish(run: Run, t0: float, emit: EventFn) -> Run:
    run.duration_s = time.perf_counter() - t0
    run.verdict = diagnose(run)
    emit({"type": "segments", "segments": run.verdict["segments"]})
    emit({"type": "done", "verdict": run.verdict})
    return run


async def _server_view(cfg: Config, site: str) -> Measurement:
    """Ask the NetDoctor server whether the site is slow from there too."""
    import json
    import urllib.parse
    import urllib.request

    url = f"{cfg.server_url.rstrip('/')}/probe?host={urllib.parse.quote(site)}"
    try:
        def get():
            with urllib.request.urlopen(url, timeout=6) as r:  # noqa: S310
                return json.loads(r.read())

        data = await asyncio.to_thread(get)
        return Measurement(probe="server_view", target=site, meta=data)
    except Exception as e:
        return Measurement(probe="server_view", target=site, meta={"error": str(e)})


def checkup(site: str | None = None, load: bool = True, on_event: EventFn | None = None,
            cfg: Config | None = None) -> Run:
    return asyncio.run(run_checkup(site, load, on_event, cfg))
