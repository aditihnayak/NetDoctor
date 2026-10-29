"""Windows Wi-Fi via `netsh wlan` (no admin; Windows 11 may require Location access)."""
from __future__ import annotations

import re

from ..probes.util import run_cmd


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"^\s*{name}\s*:\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else None


def _band(ch: int | None) -> str | None:
    if ch is None:
        return None
    return "2.4" if ch <= 14 else "5"


def parse_interfaces(out: str) -> dict:
    if not re.search(r"State\s*:\s*connected", out, re.I):
        return {}
    ch = _field(out, "Channel")
    sig = _field(out, "Signal")
    rate = _field(out, r"Receive rate \(Mbps\)")
    channel = int(ch) if ch and ch.isdigit() else None
    quality = int(sig.rstrip("%")) if sig and sig.rstrip("%").isdigit() else None
    return {
        "ssid": _field(out, "SSID"),
        "channel": channel,
        "band": _band(channel),
        "rssi_dbm": quality // 2 - 100 if quality is not None else None,
        "rate_mbps": float(rate) if rate else None,
        "phy": _field(out, "Radio type"),
    }


def parse_networks(out: str) -> list[dict]:
    nets = []
    ssid = None
    for block in re.split(r"\n(?=SSID \d+)", out):
        m = re.match(r"SSID \d+\s*:\s*(.*)", block.strip())
        if m:
            ssid = m.group(1).strip()
        for bss in re.split(r"\n\s*BSSID \d+", block)[1:]:
            ch = _field(bss, "Channel")
            sig = _field(bss, "Signal")
            channel = int(ch) if ch and ch.isdigit() else None
            q = int(sig.rstrip("%")) if sig and sig.rstrip("%").isdigit() else None
            nets.append({"ssid": ssid, "channel": channel, "band": _band(channel),
                         "rssi_dbm": q // 2 - 100 if q is not None else None})
    return nets


async def scan() -> dict:
    _, out = await run_cmd("netsh", "wlan", "show", "interfaces", timeout=5)
    cur = parse_interfaces(out)
    if not cur:
        err = "location_permission_needed" if "location" in out.lower() else "not_connected_to_wifi"
        return {"available": False, "error": err}
    _, nout = await run_cmd("netsh", "wlan", "show", "networks", "mode=bssid", timeout=8)
    nets = [n for n in parse_networks(nout) if not (n["ssid"] == cur["ssid"] and n["channel"] == cur["channel"])]
    return {"available": True, "noise_dbm": None, "snr_db": None, "networks": nets, **cur}
