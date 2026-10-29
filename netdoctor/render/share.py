"""Opt-in anonymised sharing.

Removes IP addresses (keeps only the provider name), Wi-Fi network names
and anything that identifies the device.
"""
from __future__ import annotations

import copy
import json
import re
import urllib.request

from ..models.run import Run

# IPv4, or IPv6 with 4+ groups or a "::" (so clock times like 23:20:03 aren't touched).
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b|[0-9a-f]{0,4}::[0-9a-f:]{1,35}", re.I)
PUBLIC_TARGETS = {"1.1.1.1", "8.8.8.8", "9.9.9.9", "192.0.2.53"}


DROP = {"public_ip", "ssid", "iface", "dns_servers", "colo", "alt_5ghz_ssid"}


class _Masker:
    """Replaces private/identifying IPs with stable placeholders (ip-1, ip-2, ...)
    so the run still replays: hops stay distinct, only the addresses are gone."""

    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def ip(self, v: str) -> str:
        if v in PUBLIC_TARGETS:
            return v
        return self.map.setdefault(v, f"ip-{len(self.map) + 1}")

    def scrub(self, obj):
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items() if k not in DROP}
        if isinstance(obj, list):
            return [self.scrub(x) for x in obj]
        if isinstance(obj, str):
            return IP_RE.sub(lambda m: self.ip(m.group(0)), obj)
        return obj


def anonymise(run: Run) -> dict:
    d = copy.deepcopy(run.to_dict())
    for m in d["measurements"]:
        if m.get("probe") == "wifi":
            for n in m.get("meta", {}).get("networks", []):
                n.pop("ssid", None)
    return _Masker().scrub(d)


def share(run: Run, server_url: str) -> bool:
    data = json.dumps(anonymise(run)).encode()
    req = urllib.request.Request(f"{server_url.rstrip('/')}/share", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310
            return r.status < 300
    except Exception:
        return False
