"""Wi-Fi scan: one interface, per-OS implementations.

Returns a plain dict:
  available, ssid, rssi_dbm, noise_dbm, snr_db, rate_mbps, channel, band,
  width_mhz, networks: [{ssid, channel, band, rssi_dbm}], error
"""
from __future__ import annotations

from ..models.run import Measurement
from ..probes.util import IS_LINUX, IS_MAC, IS_WINDOWS


async def scan_wifi() -> Measurement:
    try:
        if IS_MAC:
            from .macos import scan
        elif IS_LINUX:
            from .linux import scan
        elif IS_WINDOWS:
            from .windows import scan
        else:
            return Measurement(probe="wifi", meta={"available": False, "error": "unsupported_os"})
        meta = await scan()
    except Exception as e:  # never let Wi-Fi scanning break a checkup
        meta = {"available": False, "error": f"{type(e).__name__}: {e}"}
    if meta.get("available"):
        meta.update(analyse_channels(meta))
    return Measurement(probe="wifi", meta=meta)


def band_of(channel: int | None) -> str | None:
    if channel is None:
        return None
    if channel <= 14:
        return "2.4"
    if channel <= 177:
        return "5"
    return "6"


def _overlaps(a: int, b: int, band: str) -> bool:
    if band == "2.4":
        return abs(a - b) < 5
    return a == b


def analyse_channels(meta: dict) -> dict:
    """Count neighbours on/overlapping your channel; suggest a quieter one."""
    ch, band = meta.get("channel"), meta.get("band")
    nets = [n for n in meta.get("networks", []) if n.get("channel")]
    if not ch or not band:
        return {}
    same_band = [n for n in nets if n.get("band") == band]

    def crowd(c: int) -> int:
        return sum(1 for n in same_band if _overlaps(n["channel"], c, band))

    per_channel: dict[int, int] = {}
    for n in nets:
        per_channel[n["channel"]] = per_channel.get(n["channel"], 0) + 1

    candidates = [1, 6, 11] if band == "2.4" else [36, 40, 44, 48, 149, 153, 157, 161] if band == "5" else [ch]
    best = min(candidates, key=lambda c: (crowd(c), c != ch))
    current = crowd(ch)
    congested = current >= (6 if band == "2.4" else 4)
    out = {
        "neighbours_on_channel": current,
        "per_channel": {str(k): v for k, v in sorted(per_channel.items())},
        "best_channel": best,
        "best_channel_neighbours": crowd(best),
        "congested": congested,
    }
    # Is there a 5 GHz network with (roughly) the same name the user could switch to?
    ssid = meta.get("ssid")
    if band == "2.4" and ssid and not ssid.startswith("<"):
        base = ssid.lower().replace("2.4", "").replace("2g", "").strip(" -_")
        for n in nets:
            name = (n.get("ssid") or "")
            if n.get("band") in ("5", "6") and name and base and base in name.lower():
                out["alt_5ghz_ssid"] = name
                break
    return out


def signal_label(rssi: float | None) -> str | None:
    if rssi is None:
        return None
    if rssi >= -60:
        return "Good"
    if rssi >= -70:
        return "Fair"
    return "Poor"
