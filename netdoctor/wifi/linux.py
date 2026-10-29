"""Linux Wi-Fi via `iw` (current link) and `nmcli` (nearby networks). No root needed."""
from __future__ import annotations

import re
import shutil

from ..probes.util import run_cmd


def _freq_to_channel(freq: int) -> int | None:
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:
        return (freq - 5950) // 5
    return None


def _band(freq: int) -> str:
    return "2.4" if freq < 3000 else "5" if freq < 5925 else "6"


def parse_iw_link(out: str) -> dict:
    if "Not connected" in out or "Connected to" not in out:
        return {}
    d: dict = {}
    if m := re.search(r"SSID:\s*(.+)", out):
        d["ssid"] = m.group(1).strip()
    if m := re.search(r"freq:\s*(\d+)", out):
        f = int(float(m.group(1)))
        d["channel"], d["band"] = _freq_to_channel(f), _band(f)
    if m := re.search(r"signal:\s*(-?\d+)", out):
        d["rssi_dbm"] = int(m.group(1))
    if m := re.search(r"tx bitrate:\s*([\d.]+)", out):
        d["rate_mbps"] = float(m.group(1))
    if m := re.search(r"(\d+)MHz", out):
        d["width_mhz"] = int(m.group(1))
    return d


def parse_nmcli(out: str) -> tuple[dict, list[dict]]:
    current: dict = {}
    nets = []
    for line in out.splitlines():
        # ACTIVE:SSID:CHAN:FREQ:SIGNAL  (colons in SSIDs are escaped as \:)
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 5:
            continue
        active, ssid, chan, freq, signal = parts[:5]
        try:
            ch = int(chan)
            f = int(re.sub(r"\D", "", freq) or 0)
            rssi = int(signal) // 2 - 100  # nmcli reports 0-100 quality
        except ValueError:
            continue
        n = {"ssid": ssid.replace("\\:", ":"), "channel": ch, "band": _band(f) if f else None, "rssi_dbm": rssi}
        if active == "yes":
            current = dict(n)
        else:
            nets.append(n)
    return current, nets


async def scan() -> dict:
    cur: dict = {}
    nets: list[dict] = []
    iface = None
    if shutil.which("iw"):
        _, dev = await run_cmd("iw", "dev", timeout=3)
        if m := re.search(r"Interface\s+(\S+)", dev):
            iface = m.group(1)
            _, link = await run_cmd("iw", "dev", iface, "link", timeout=3)
            cur = parse_iw_link(link)
    if shutil.which("nmcli"):
        _, out = await run_cmd("nmcli", "-t", "-f", "ACTIVE,SSID,CHAN,FREQ,SIGNAL", "dev", "wifi", "list", timeout=8)
        ncur, nets = parse_nmcli(out)
        for k, v in ncur.items():
            cur.setdefault(k, v)
    if not cur:
        return {"available": False, "error": "not_connected_to_wifi_or_tools_missing"}
    return {"available": True, "iface": iface, "noise_dbm": None, "snr_db": None, "networks": nets, **cur}
