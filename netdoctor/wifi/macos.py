"""macOS Wi-Fi via system_profiler (no admin needed; names may be hidden
unless the terminal/app has Location permission)."""
from __future__ import annotations

import json
import re

from ..probes.util import run_cmd

CH_RE = re.compile(r"(\d+)\s*\((\d)GHz(?:,\s*(\d+)MHz)?\)")
SN_RE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")


def _channel(s: str | None) -> tuple[int | None, str | None, int | None]:
    m = CH_RE.search(s or "")
    if not m:
        return None, None, None
    ghz = m.group(2)
    band = {"2": "2.4", "5": "5", "6": "6"}.get(ghz, ghz)
    return int(m.group(1)), band, int(m.group(3)) if m.group(3) else None


def _signal(s: str | None) -> tuple[int | None, int | None]:
    m = SN_RE.search(s or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def parse(data: dict) -> dict:
    ifaces = data.get("SPAirPortDataType", [{}])[0].get("spairport_airport_interfaces", [])
    for iface in ifaces:
        cur = iface.get("spairport_current_network_information")
        if not cur:
            continue
        ch, band, width = _channel(cur.get("spairport_network_channel"))
        rssi, noise = _signal(cur.get("spairport_signal_noise"))
        nets = []
        for n in iface.get("spairport_airport_other_local_wireless_networks", []):
            nch, nband, _ = _channel(n.get("spairport_network_channel"))
            nrssi, _ = _signal(n.get("spairport_signal_noise"))
            nets.append({"ssid": n.get("_name"), "channel": nch, "band": nband, "rssi_dbm": nrssi})
        return {
            "available": True,
            "iface": iface.get("_name"),
            "ssid": cur.get("_name"),
            "rssi_dbm": rssi,
            "noise_dbm": noise,
            "snr_db": (rssi - noise) if rssi is not None and noise is not None else None,
            "rate_mbps": cur.get("spairport_network_rate"),
            "phy": cur.get("spairport_network_phymode"),
            "channel": ch,
            "band": band,
            "width_mhz": width,
            "networks": nets,
            "names_hidden": (cur.get("_name") or "").startswith("<"),
        }
    return {"available": False, "error": "not_connected_to_wifi"}


async def scan() -> dict:
    code, out = await run_cmd("system_profiler", "SPAirPortDataType", "-json", timeout=15)
    if code != 0:
        return {"available": False, "error": "system_profiler_failed"}
    return parse(json.loads(out))
