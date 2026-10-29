"""IPv6 health: race IPv4 and IPv6 connections to the same dual-stack site.

Broken IPv6 is sneaky: the system prefers IPv6, waits for it to time out,
then falls back to IPv4. Pages work, but the first load is slow.
"""
from __future__ import annotations

import asyncio
import socket
import time

from ..models.run import Measurement
from .context import ipv6_configured


async def _connect(host: str, family: int, timeout: float) -> tuple[float | None, str | None]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, 443, family=family, type=socket.SOCK_STREAM), timeout)
    except Exception:
        return None, "no_address"
    if not infos:
        return None, "no_address"
    af, _, _, _, addr = infos[0]
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(addr[0], 443, family=af), timeout)
        w.close()
        return round((time.perf_counter() - t0) * 1000, 1), None
    except asyncio.TimeoutError:
        return None, "timeout"
    except OSError as e:
        return None, f"error: {e.strerror or e}"


async def probe_ipv6(host: str, timeout: float = 3.0) -> Measurement:
    configured = ipv6_configured()
    jobs = [_connect(host, socket.AF_INET, timeout)]
    if configured:
        jobs.append(_connect(host, socket.AF_INET6, timeout))
    res = await asyncio.gather(*jobs)
    v4_ms, v4_err = res[0]
    v6_ms, v6_err = res[1] if configured else (None, "not_configured")
    broken = bool(configured and v4_ms is not None and v6_ms is None and v6_err != "no_address")
    slow = bool(configured and v4_ms is not None and v6_ms is not None and v6_ms > v4_ms + 300)
    return Measurement(
        probe="ipv6",
        target=host,
        meta={
            "configured": configured,
            "v4_ms": v4_ms,
            "v4_error": v4_err,
            "v6_ms": v6_ms,
            "v6_error": v6_err,
            "broken": broken,
            "slow": slow,
        },
    )
