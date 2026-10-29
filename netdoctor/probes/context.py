"""Context probe: link type, gateway, DNS settings, VPN, captive portal, provider."""
from __future__ import annotations

import asyncio
import re
import socket
import urllib.request

from ..config import Config
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, SYSTEM, is_ip, run_cmd

VPN_IFACE = re.compile(r"^(utun|tun|tap|wg|ppp|ipsec|zt|tailscale|nordlynx|proton)", re.I)
VPN_WORDS = re.compile(r"vpn|wireguard|tap-|tunnel|openvpn|tailscale|zerotier|anyconnect|fortinet|nordlynx", re.I)


async def _route_mac(dest: str) -> tuple[str | None, str | None]:
    _, out = await run_cmd("route", "-n", "get", dest, timeout=2)
    gw = re.search(r"gateway:\s*(\S+)", out)
    iface = re.search(r"interface:\s*(\S+)", out)
    return (gw.group(1) if gw else None, iface.group(1) if iface else None)


async def _route_linux(dest: str) -> tuple[str | None, str | None]:
    _, out = await run_cmd("ip", "route", "get", dest, timeout=2)
    gw = re.search(r"\bvia\s+(\S+)", out)
    iface = re.search(r"\bdev\s+(\S+)", out)
    return (gw.group(1) if gw else None, iface.group(1) if iface else None)


async def _route_windows() -> tuple[str | None, str | None]:
    ps = (
        "Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | "
        "Select-Object -First 1 | ForEach-Object { $_.NextHop + '|' + $_.InterfaceAlias + '|' + "
        "(Get-NetAdapter -InterfaceIndex $_.ifIndex).InterfaceDescription }"
    )
    _, out = await run_cmd("powershell", "-NoProfile", "-Command", ps, timeout=6)
    parts = out.strip().splitlines()[-1].split("|") if out.strip() else []
    if len(parts) >= 2:
        return parts[0].strip() or None, "|".join(p.strip() for p in parts[1:])
    return None, None


async def _link_type(iface: str | None) -> str:
    if not iface:
        return "unknown"
    if IS_MAC:
        _, out = await run_cmd("networksetup", "-listallhardwareports", timeout=3)
        for block in out.split("\n\n"):
            if f"Device: {iface}" in block:
                port = re.search(r"Hardware Port:\s*(.+)", block)
                name = port.group(1).lower() if port else ""
                if "wi-fi" in name or "airport" in name:
                    return "wifi"
                return "wired"
        return "wired" if iface.startswith("en") else "other"
    if IS_LINUX:
        import os

        if os.path.isdir(f"/sys/class/net/{iface}/wireless") or iface.startswith(("wl", "wlan")):
            return "wifi"
        return "wired" if iface.startswith(("en", "eth")) else "other"
    if IS_WINDOWS:
        low = iface.lower()
        if any(w in low for w in ("wi-fi", "wireless", "wlan", "802.11")):
            return "wifi"
        return "wired"
    return "unknown"


def _dns_servers() -> list[str]:
    try:
        import dns.resolver

        return [ns for ns in dns.resolver.Resolver().nameservers if isinstance(ns, str)]
    except Exception:
        return []


def _http_get(url: str, timeout: float) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "NetDoctor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed URLs)
        return r.status, r.geturl(), r.read(4096).decode(errors="replace")


async def captive_portal(cfg: Config) -> bool | None:
    try:
        status, final_url, body = await asyncio.to_thread(_http_get, cfg.captive_url, 2.5)
    except Exception:
        return None  # unknown: probably no connectivity at all
    return not (status == 200 and cfg.captive_expect in body and final_url.rstrip("/") == cfg.captive_url.rstrip("/"))


async def public_info(cfg: Config) -> dict:
    """Public IP, Cloudflare location and the provider's network (ASN) name."""
    info: dict = {}
    try:
        _, _, body = await asyncio.to_thread(_http_get, cfg.trace_url, 2.5)
        kv = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        info.update(public_ip=kv.get("ip"), loc=kv.get("loc"), colo=kv.get("colo"))
    except Exception:
        return info
    ip = info.get("public_ip")
    if ip and is_ip(ip) and ":" not in ip:
        asn = await asn_lookup(ip)
        if asn:
            info["asn"] = asn
            info["isp"] = await asn_name(asn)
    return info


async def asn_lookup(ip: str, timeout: float = 1.5) -> int | None:
    """Origin ASN of an IPv4 address via Team Cymru's DNS service."""
    try:
        import dns.asyncresolver

        r = dns.asyncresolver.Resolver()
        r.lifetime = timeout
        q = ".".join(reversed(ip.split("."))) + ".origin.asn.cymru.com"
        ans = await r.resolve(q, "TXT")
        txt = ans[0].to_text().strip('"')
        return int(txt.split("|")[0].strip().split()[0])
    except Exception:
        return None


async def asn_name(asn: int, timeout: float = 1.5) -> str | None:
    try:
        import dns.asyncresolver

        r = dns.asyncresolver.Resolver()
        r.lifetime = timeout
        ans = await r.resolve(f"AS{asn}.asn.cymru.com", "TXT")
        name = ans[0].to_text().strip('"').split("|")[-1].strip()
        return re.sub(r",\s*[A-Z]{2}$", "", name)
    except Exception:
        return None


def ipv6_configured() -> bool:
    """True if the OS has a route to the IPv6 internet."""
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        s.connect(("2606:4700:4700::1111", 53))
        addr = s.getsockname()[0]
        return not addr.startswith("fe80")
    except OSError:
        return False
    finally:
        s.close()


async def probe_context(cfg: Config) -> dict:
    target = cfg.ping_targets[0]
    gateway = iface = None
    vpn = False
    if IS_MAC:
        (gw_t, if_t), (gateway, iface) = await asyncio.gather(_route_mac(target), _route_mac("default"))
        if if_t and VPN_IFACE.match(if_t):
            vpn = True
        if iface and VPN_IFACE.match(iface):
            vpn = True
    elif IS_LINUX:
        gateway, iface = await _route_linux(target)
        if iface and VPN_IFACE.match(iface):
            vpn = True
            _, out = await run_cmd("ip", "route", "show", "default", timeout=2)
            m = re.search(r"default via (\S+) dev (\S+)", out)
            if m:
                gateway = m.group(1)
    elif IS_WINDOWS:
        gateway, iface = await _route_windows()
        vpn = bool(iface and VPN_WORDS.search(iface))

    if gateway and not is_ip(gateway):
        gateway = None

    link_task = asyncio.create_task(_link_type(iface.split("|")[0] if iface else None))
    tasks = [captive_portal(cfg)]
    if not cfg.offline_lab:
        tasks.append(public_info(cfg))
    results = await asyncio.gather(*tasks)
    captive = results[0]
    pub = results[1] if len(results) > 1 else {}
    link = await link_task

    return {
        "os": SYSTEM,
        "link": link,
        "iface": iface.split("|")[0] if iface else None,
        "vpn": vpn,
        "gateway": gateway,
        "dns_servers": _dns_servers(),
        "captive_portal": captive,
        "online": captive is not None,
        "ipv6": ipv6_configured(),
        "double_nat": None,  # filled in from the route trace
        "cgnat": None,
        **pub,
    }
