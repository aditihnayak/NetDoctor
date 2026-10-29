"""Latency probes that work without admin rights.

Preferred: unprivileged ICMP (icmplib, works on macOS and most Linux distros).
Fallback: TCP connect timing. A refused connection still costs exactly one
round trip, so even a closed port on the router gives a valid delay sample.
"""
from __future__ import annotations

import asyncio
import errno
import time

from ..models.run import Measurement
from .util import loss_of

try:
    from icmplib import async_ping
    from icmplib.exceptions import ICMPLibError, SocketPermissionError
except Exception:  # pragma: no cover
    async_ping = None
    ICMPLibError = SocketPermissionError = Exception

TCP_PORTS = (443, 80, 53)


class Pinger:
    def __init__(self) -> None:
        self.method: str | None = None  # 'icmp' or 'tcp'
        self._lock = asyncio.Lock()

    async def detect(self, host: str) -> str:
        async with self._lock:
            if self.method:
                return self.method
            if async_ping is not None:
                try:
                    await async_ping(host, count=1, timeout=1, privileged=False)
                    self.method = "icmp"
                    return self.method
                except SocketPermissionError:
                    pass
                except (ICMPLibError, OSError):
                    # Socket works, host just didn't answer: ICMP is usable.
                    self.method = "icmp"
                    return self.method
            self.method = "tcp"
            return self.method

    async def once(self, host: str, timeout: float = 1.0) -> float | None:
        """One delay sample in ms, or None if lost."""
        method = self.method or await self.detect(host)
        if method == "icmp":
            try:
                r = await async_ping(host, count=1, timeout=timeout, privileged=False)
                return round(r.rtts[0], 2) if r.is_alive and r.rtts else None
            except (ICMPLibError, OSError):
                return None
        return await tcp_rtt(host, timeout)

    async def series(self, host: str, count: int = 10, interval: float = 0.1, timeout: float = 1.0) -> list[float | None]:
        """Send `count` probes `interval` apart without waiting for each answer."""
        tasks = []
        for i in range(count):
            tasks.append(asyncio.create_task(self.once(host, timeout)))
            if i < count - 1:
                await asyncio.sleep(interval)
        return list(await asyncio.gather(*tasks))

    async def measure(self, host: str, label: str, phase: str, count: int = 10, interval: float = 0.1) -> Measurement:
        samples = await self.series(host, count, interval)
        return Measurement(
            probe="icmp" if self.method == "icmp" else "tcp_ping",
            target=label,
            phase=phase,
            samples_ms=samples,
            loss=loss_of(samples),
            meta={"host": host, "method": self.method},
        )


async def tcp_rtt(host: str, timeout: float = 1.0) -> float | None:
    loop = asyncio.get_running_loop()
    for port in TCP_PORTS:
        t0 = time.perf_counter()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            ms = (time.perf_counter() - t0) * 1000
            w.close()
            return round(ms, 2)
        except ConnectionRefusedError:
            return round((time.perf_counter() - t0) * 1000, 2)
        except asyncio.TimeoutError:
            return None
        except OSError as e:
            if e.errno in (errno.ECONNREFUSED,):
                return round((time.perf_counter() - t0) * 1000, 2)
            continue
    _ = loop
    return None
