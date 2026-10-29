"""Small shared helpers: subprocess with timeout, statistics, IP classification."""
from __future__ import annotations

import asyncio
import ipaddress
import platform
import statistics
from typing import Iterable

SYSTEM = platform.system().lower()  # 'darwin', 'linux', 'windows'
IS_MAC = SYSTEM == "darwin"
IS_LINUX = SYSTEM == "linux"
IS_WINDOWS = SYSTEM == "windows"


async def run_cmd(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    """Run a command, return (exit code, combined output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=1)
        except Exception:
            out = b""
        return 124, out.decode(errors="replace") if out else ""
    return proc.returncode or 0, out.decode(errors="replace")


# ---- statistics --------------------------------------------------------

def clean(samples: Iterable[float | None] | None) -> list[float]:
    return [s for s in (samples or []) if s is not None]


def loss_of(samples: list[float | None] | None) -> float | None:
    if not samples:
        return None
    return sum(1 for s in samples if s is None) / len(samples)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def jitter(values: list[float]) -> float | None:
    """Mean absolute difference between consecutive samples (RFC 3550 style)."""
    if len(values) < 2:
        return None
    return sum(abs(a - b) for a, b in zip(values, values[1:])) / (len(values) - 1)


def summarize(samples: list[float | None] | None) -> dict:
    vals = clean(samples)
    return {
        "n": len(samples or []),
        "median": _r(median(vals)),
        "min": _r(min(vals)) if vals else None,
        "p95": _r(percentile(vals, 95)),
        "max": _r(max(vals)) if vals else None,
        "jitter": _r(jitter(vals)),
        "loss": _r(loss_of(samples), 4),
    }


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


# ---- IP helpers ----------------------------------------------------------

CGNAT = ipaddress.ip_network("100.64.0.0/10")


def ip_kind(ip: str | None) -> str:
    """'private', 'cgnat', 'public', or 'unknown'."""
    if not ip:
        return "unknown"
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if a.version == 4 and a in CGNAT:
        return "cgnat"
    if a.is_private or a.is_link_local or a.is_loopback:
        return "private"
    return "public"


def is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False
