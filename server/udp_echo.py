"""UDP echo for MTU / jitter tests (optional). Echoes each datagram back to its sender.

Run:  python server/udp_echo.py --port 7007
"""
import argparse
import socket

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7007)
a = ap.parse_args()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", a.port))
print(f"UDP echo on :{a.port}")
while True:
    data, addr = s.recvfrom(65535)
    s.sendto(data[:9000], addr)
