"""Services for the namespace lab: a web/load server and a DNS server.

  python3 fake_internet.py web --port 80 [--delay 0]
  python3 fake_internet.py dns --port 53 [--delay 0]

web:  GET /        small page (after --delay seconds: a "slow website")
      GET /big     streams 25 MB (load test download)
      POST /up     reads and discards the body (load test upload)
      GET /hotspot "Success" (captive-portal check)
dns:  answers every A query with 203.0.113.10 after --delay ms (NXDOMAIN-free)
"""
import argparse
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = b"\0" * 65536


def web(port: int, delay: float) -> None:
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/big"):
                size = 25_000_000
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                sent = 0
                try:
                    while sent < size:
                        self.wfile.write(CHUNK)
                        sent += len(CHUNK)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if delay:
                time.sleep(delay)
            body = b"Success" if self.path.startswith("/hotspot") else b"<html>ok</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            try:
                while n > 0:
                    got = self.rfile.read(min(n, 65536))
                    if not got:
                        break
                    n -= len(got)
            except (ConnectionResetError, BrokenPipeError):
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def dns_server(port: int, delay_ms: float) -> None:
    import dns.message
    import dns.rrset

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))

    def answer(data, addr):
        time.sleep(delay_ms / 1000)
        try:
            q = dns.message.from_wire(data)
            r = dns.message.make_response(q)
            name = q.question[0].name.to_text()
            r.answer.append(dns.rrset.from_text(name, 5, "IN", "A", "203.0.113.10"))
            s.sendto(r.to_wire(), addr)
        except Exception:
            pass

    while True:
        data, addr = s.recvfrom(4096)
        threading.Thread(target=answer, args=(data, addr), daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["web", "dns"])
    ap.add_argument("--port", type=int)
    ap.add_argument("--delay", type=float, default=0)
    a = ap.parse_args()
    if a.kind == "web":
        web(a.port or 80, a.delay)
    else:
        dns_server(a.port or 53, a.delay)
    sys.exit(0)
