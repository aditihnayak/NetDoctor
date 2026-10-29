"""Load generator that runs in its own process.

Stdlib only, so the spawned process starts fast. It saturates the link with
several parallel HTTPS streams and reports bytes moved through a shared
counter. Keeping this out of the main process means a busy download never
delays the timing of latency probes.
"""
from __future__ import annotations

import http.client
import os
import ssl
import threading
import urllib.parse

CHUNK = 64 * 1024


def _conn(url: str, timeout: float = 6.0):
    u = urllib.parse.urlsplit(url)
    path = u.path + (f"?{u.query}" if u.query else "")
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx), path, u.hostname
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout), path, u.hostname


def _download(url: str, counter, stop, errors) -> None:
    while not stop.is_set():
        try:
            conn, path, host = _conn(url)
            conn.request("GET", path, headers={"User-Agent": "NetDoctor/1.0", "Accept-Encoding": "identity"})
            r = conn.getresponse()
            if r.status >= 400:
                errors.append(f"HTTP {r.status}")
                stop.wait(0.5)
                continue
            while not stop.is_set():
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                with counter.get_lock():
                    counter.value += len(chunk)
            conn.close()
        except Exception as e:  # network errors: note and retry
            errors.append(type(e).__name__)
            stop.wait(0.3)


def _upload(url: str, counter, stop, errors, size: int = 200_000_000) -> None:
    payload = os.urandom(CHUNK)
    while not stop.is_set():
        try:
            conn, path, host = _conn(url)
            conn.putrequest("POST", path)
            conn.putheader("User-Agent", "NetDoctor/1.0")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            sent = 0
            while sent < size and not stop.is_set():
                conn.send(payload)
                sent += CHUNK
                with counter.get_lock():
                    counter.value += CHUNK
            if not stop.is_set():
                conn.getresponse().read()
            conn.close()
        except Exception as e:
            errors.append(type(e).__name__)
            stop.wait(0.3)


def worker_main(cmd_q, counter, stop, err_q) -> None:
    """Wait for ('download'|'upload', url, streams) commands until 'quit'."""
    while True:
        cmd = cmd_q.get()
        if not cmd or cmd[0] == "quit":
            return
        direction, url, streams = cmd
        stop.clear()
        errors: list[str] = []
        fn = _download if direction == "download" else _upload
        threads = [threading.Thread(target=fn, args=(url, counter, stop, errors), daemon=True) for _ in range(streams)]
        for t in threads:
            t.start()
        stop.wait()
        for t in threads:
            t.join(timeout=1.5)
        err_q.put(errors[:20])
