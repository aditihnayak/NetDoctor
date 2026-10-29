"""Test targets. Every value can be overridden with an environment variable,
which is how the fault-injection lab points NetDoctor at its own fake internet."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else default


@dataclass
class Config:
    # Internet latency targets (anycast, answer ping reliably).
    ping_targets: list[str] = field(default_factory=lambda: _env_list("NETDOCTOR_PING_TARGETS", ["1.1.1.1", "8.8.8.8"]))
    # Public resolvers to compare against: "Name=ip".
    public_dns: list[str] = field(
        default_factory=lambda: _env_list(
            "NETDOCTOR_PUBLIC_DNS", ["Cloudflare=1.1.1.1", "Google=8.8.8.8", "Quad9=9.9.9.9"]
        )
    )
    # Domain used for uncached lookups: <random>.<domain>.
    dns_probe_domain: str = os.environ.get("NETDOCTOR_DNS_DOMAIN", "example.com")
    # Well-known sites used as a control when timing websites.
    control_sites: list[str] = field(
        default_factory=lambda: _env_list(
            "NETDOCTOR_CONTROL_SITES", ["www.google.com", "www.cloudflare.com", "www.wikipedia.org"]
        )
    )
    # Load test endpoints (Cloudflare's public speed test by default).
    download_url: str = os.environ.get("NETDOCTOR_DOWNLOAD_URL", "https://speed.cloudflare.com/__down?bytes=25000000")
    upload_url: str = os.environ.get("NETDOCTOR_UPLOAD_URL", "https://speed.cloudflare.com/__up")
    load_streams: int = int(os.environ.get("NETDOCTOR_LOAD_STREAMS", "6"))
    # Captive portal check.
    captive_url: str = os.environ.get("NETDOCTOR_CAPTIVE_URL", "http://captive.apple.com/hotspot-detect.html")
    captive_expect: str = os.environ.get("NETDOCTOR_CAPTIVE_EXPECT", "Success")
    # Public IP lookup.
    trace_url: str = os.environ.get("NETDOCTOR_TRACE_URL", "https://www.cloudflare.com/cdn-cgi/trace")
    # Dual-stack host for the IPv4 / IPv6 race.
    dualstack_host: str = os.environ.get("NETDOCTOR_DUALSTACK_HOST", "www.google.com")
    # Optional NetDoctor server ("is it slow for us too" API + result sharing).
    server_url: str | None = os.environ.get("NETDOCTOR_SERVER") or None
    # Skip lookups that need the public internet (lab use).
    offline_lab: bool = os.environ.get("NETDOCTOR_LAB") == "1"
    # Address ranges that belong to your ISP (lab use; normally found via ASN lookup).
    isp_prefixes: list[str] = field(default_factory=lambda: _env_list("NETDOCTOR_ISP_PREFIXES", []))
