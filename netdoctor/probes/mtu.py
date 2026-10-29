"""Path-MTU probe: find the largest packet that gets through unfragmented.

A healthy network either delivers a large packet or replies "too big"
(fragmentation needed). A black hole does neither: large packets just vanish,
which makes some sites hang while others load.
"""
from __future__ import annotations

import re

from ..models.run import Measurement
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, run_cmd

FRAG_RE = re.compile(r"frag(mentation)? needed|needs to be fragmented|message too long|mtu\s*=?\s*\d+", re.I)
MTU_RE = re.compile(r"mtu\s*=?\s*(\d+)", re.I)


def _cmd(host: str, size: int) -> list[str]:
    if IS_MAC:
        return ["ping", "-D", "-s", str(size), "-c", "1", "-t", "2", host]
    if IS_LINUX:
        return ["ping", "-M", "do", "-s", str(size), "-c", "1", "-W", "1", host]
    if IS_WINDOWS:
        return ["ping", "-f", "-l", str(size), "-n", "1", "-w", "1000", host]
    return ["ping", "-c", "1", host]


def _ok(code: int, out: str) -> bool:
    if IS_WINDOWS:
        return "TTL=" in out.upper()
    return code == 0 and bool(re.search(r"1 (packets )?received|bytes from", out)) and not FRAG_RE.search(out)


async def _try(host: str, size: int) -> tuple[bool, bool, int | None]:
    """(delivered, got_too_big_reply, reported_mtu)"""
    code, out = await run_cmd(*_cmd(host, size), timeout=3)
    m = MTU_RE.search(out)
    return _ok(code, out), bool(FRAG_RE.search(out)), int(m.group(1)) if m else None


async def probe_mtu(host: str) -> Measurement:
    tested: list[list] = []
    frag_seen = False
    reported = None

    small_ok, _, _ = await _try(host, 56)
    tested.append([56, small_ok])
    if not small_ok:
        return Measurement(probe="mtu", target=host, meta={"tested": tested, "error": "no_reply_to_small_packets"})

    full_ok, frag, rep = await _try(host, 1472)  # 1472 + 28 header = 1500
    tested.append([1472, full_ok])
    frag_seen |= frag
    reported = reported or rep
    max_payload = 1472 if full_ok else None

    if not full_ok:
        lo, hi = 1200, 1472
        ok, frag, rep = await _try(host, lo)
        tested.append([lo, ok])
        frag_seen |= frag
        reported = reported or rep
        if not ok:
            lo, hi = 576, 1200
        for _ in range(4):
            mid = (lo + hi) // 2
            ok, frag, rep = await _try(host, mid)
            tested.append([mid, ok])
            frag_seen |= frag
            reported = reported or rep
            if ok:
                lo = mid
            else:
                hi = mid
            if hi - lo <= 16:
                break
        max_payload = lo

    path_mtu = (max_payload + 28) if max_payload else None
    # Black hole: large packets lost with no "too big" message at all.
    black_hole = (not full_ok) and (not frag_seen) and (max_payload or 0) < 1400
    return Measurement(
        probe="mtu",
        target=host,
        meta={
            "tested": tested,
            "max_payload": max_payload,
            "path_mtu": path_mtu,
            "reported_mtu": reported,
            "frag_needed_seen": frag_seen,
            "black_hole": black_hole,
        },
    )
