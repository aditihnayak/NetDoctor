"""NetDoctor server API (optional, stdlib only).

  GET  /probe?host=example.com   "Is it slow for us too?" — times the site from here
  POST /share                    receives an anonymised run (opt-in result sharing)
  GET  /health

Run:  python server/probe_api.py --port 8080 --data ./shared
Point the client at it:  NETDOCTOR_SERVER=https://your-server netdoctor youtube.com
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$", re.I)
MAX_SHARE = 2_000_000
DATA = Path("shared")
_rate: dict[str, list[float]] = {}


def time_site(host: str, timeout: float = 6.0) -> dict:
    t0 = time.perf_counter()
    try:
        addr = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4]
        if not _public(addr[0]):
            return {"error": "refused_private_address"}
        t1 = time.perf_counter()
        raw = socket.create_connection(addr[:2], timeout=timeout)
        t2 = time.perf_counter()
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        t3 = time.perf_counter()
        s.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: NetDoctor-server\r\nConnection: close\r\n\r\n".encode())
        s.recv(1)
        t4 = time.perf_counter()
        s.close()
        ttfb = (t4 - t3) * 1000
        return {"dns_ms": round((t1 - t0) * 1000, 1), "connect_ms": round((t2 - t1) * 1000, 1),
                "tls_ms": round((t3 - t2) * 1000, 1), "ttfb_ms": round(ttfb, 1), "slow": ttfb > 1500}
    except Exception as e:
        return {"error": type(e).__name__, "slow": True}


def _public(ip: str) -> bool:
    import ipaddress

    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast)


def _allow(ip: str, per_min: int = 30) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(ip, []) if now - t < 60]
    hits.append(now)
    _rate[ip] = hits
    return len(hits) <= per_min


class Handler(BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        b = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/health":
            return self._json({"ok": True})
        if u.path == "/probe":
            if not _allow(self.client_address[0]):
                return self._json({"error": "rate_limited"}, 429)
            host = (parse_qs(u.query).get("host") or [""])[0].strip().lower()
            if not HOST_RE.match(host):
                return self._json({"error": "bad_host"}, 400)
            return self._json({"host": host, **time_site(host)})
        return self._json({"error": "not_found"}, 404)

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/share":
            return self._json({"error": "not_found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_SHARE or not _allow(self.client_address[0], 10):
            return self._json({"error": "rejected"}, 413)
        try:
            run = json.loads(self.rfile.read(n))
            assert isinstance(run, dict) and run.get("version") == 1
        except Exception:
            return self._json({"error": "bad_json"}, 400)
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}.json").write_text(json.dumps(run))
        return self._json({"ok": True})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", default="shared")
    a = ap.parse_args()
    DATA = Path(a.data)
    print(f"NetDoctor API on :{a.port}")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
