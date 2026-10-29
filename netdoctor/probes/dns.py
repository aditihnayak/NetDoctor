"""DNS timing: your resolver vs public resolvers, using uncached lookups.

Each lookup asks for a random name (e.g. nd-8f3k2a.example.com), so no resolver
can answer from cache: we time the real work of looking a name up.
"""
from __future__ import annotations

import asyncio
import secrets
import time

import dns.asyncquery
import dns.exception
import dns.message
import dns.rcode

from ..config import Config
from ..models.run import Measurement
from .util import loss_of, summarize


async def _query(server: str, name: str, timeout: float = 2.0) -> float | None:
    q = dns.message.make_query(name, "A")
    t0 = time.perf_counter()
    try:
        r = await dns.asyncquery.udp(q, server, timeout=timeout)
    except Exception:
        return None
    ms = (time.perf_counter() - t0) * 1000
    if r.rcode() in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
        return round(ms, 2)
    return None


async def time_resolver(server: str, domain: str, count: int = 5) -> list[float | None]:
    out = []
    for _ in range(count):
        out.append(await _query(server, f"nd-{secrets.token_hex(5)}.{domain}"))
    return out


async def detect_interception() -> bool | None:
    """Ask a resolver address that doesn't exist (TEST-NET). Any answer means
    something on the path is silently redirecting DNS traffic."""
    q = dns.message.make_query("example.com", "A")
    try:
        await dns.asyncquery.udp(q, "192.0.2.53", timeout=1.2)
        return True
    except asyncio.TimeoutError:
        return False
    except dns.exception.Timeout:
        return False
    except Exception:
        return None


async def probe_dns(cfg: Config, system_servers: list[str], gateway: str | None) -> list[Measurement]:
    servers: list[tuple[str, str, bool]] = []
    seen = set()
    for s in system_servers[:2]:
        if s not in seen and ":" not in s:
            seen.add(s)
            servers.append(("Your DNS" if s != gateway else "Your router's DNS", s, True))
    for entry in cfg.public_dns:
        name, _, ip = entry.partition("=")
        if ip and ip not in seen:
            seen.add(ip)
            servers.append((name, ip, False))

    results = await asyncio.gather(*(time_resolver(ip, cfg.dns_probe_domain) for _, ip, _ in servers))
    out = []
    for (label, ip, is_system), samples in zip(servers, results):
        out.append(
            Measurement(
                probe="dns",
                target=ip,
                phase="idle",
                samples_ms=samples,
                loss=loss_of(samples),
                meta={"label": label, "system": is_system, "is_gateway": ip == gateway, "stats": summarize(samples)},
            )
        )
    intercepted = None if cfg.offline_lab else await detect_interception()
    out.append(Measurement(probe="dns_intercept", meta={"intercepted": intercepted}))
    return out
