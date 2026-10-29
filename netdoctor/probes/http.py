"""Website timing: DNS, connect, TLS and first-byte times, split out like curl -w."""
from __future__ import annotations

import asyncio
import socket
import ssl
import time

from ..models.run import Measurement

UA = "Mozilla/5.0 (NetDoctor/1.0; +https://github.com/netdoctor)"


def normalise_site(site: str) -> str:
    s = site.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    return s.split("/")[0].split(":")[0].lower()


async def time_site(host: str, role: str, timeout: float = 6.0, max_bytes: int = 512_000) -> Measurement:
    plain = host.startswith("http://")  # plain HTTP: only used by the lab
    host = normalise_site(host)
    port = 80 if plain else 443
    meta: dict = {"role": role, "host": host}
    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout)
    except Exception as e:
        meta["error"] = "name_not_found" if isinstance(e, socket.gaierror) else "dns_timeout"
        return Measurement(probe="http", target=host, phase="idle", meta=meta)
    t_dns = time.perf_counter()
    meta["dns_ms"] = round((t_dns - t0) * 1000, 1)

    # Prefer IPv4 here; IPv6 is raced separately by the ipv6 probe.
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    family, _, _, _, addr = infos[0]
    meta["ip"] = addr[0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    writer = None
    try:
        await asyncio.wait_for(loop.sock_connect(sock, addr), timeout)
        t_conn = time.perf_counter()
        meta["connect_ms"] = round((t_conn - t_dns) * 1000, 1)

        ctx = None if plain else ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(sock=sock, ssl=ctx, server_hostname=None if plain else host), timeout
        )
        t_tls = time.perf_counter()
        meta["tls_ms"] = round((t_tls - t_conn) * 1000, 1)

        writer.write(
            f"GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {UA}\r\nAccept: */*\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        first = await asyncio.wait_for(reader.read(1), timeout)
        t_fb = time.perf_counter()
        meta["ttfb_ms"] = round((t_fb - t_tls) * 1000, 1)
        head = first + await asyncio.wait_for(reader.read(64), timeout)
        try:
            meta["status"] = int(head.split(b" ")[1])
        except Exception:
            pass
        total = len(head)
        try:
            while total < max_bytes:
                chunk = await asyncio.wait_for(reader.read(65536), max(0.5, timeout - (time.perf_counter() - t0)))
                if not chunk:
                    break
                total += len(chunk)
        except asyncio.TimeoutError:
            pass
        meta["bytes"] = total
        meta["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except asyncio.TimeoutError:
        meta["error"] = "timeout"
    except ssl.SSLError as e:
        meta["error"] = f"tls_error: {e.reason or e}"
    except OSError as e:
        meta["error"] = f"connect_error: {e.strerror or e}"
    finally:
        try:
            if writer:
                writer.close()
            else:
                sock.close()
        except Exception:
            pass
    return Measurement(probe="http", target=host, phase="idle", meta=meta)


async def probe_http(controls: list[str], site: str | None) -> list[Measurement]:
    jobs = [time_site(h, "control") for h in controls]
    if site:
        jobs.append(time_site(site if site.startswith("http://") else normalise_site(site), "target"))
    return list(await asyncio.gather(*jobs))
