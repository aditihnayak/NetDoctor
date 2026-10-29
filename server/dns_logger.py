"""Authoritative DNS logger (optional).

Answers every query for *.<zone> with 192.0.2.1 and logs which resolver asked.
Delegate a zone (e.g. probe.netdoctor.example) to this server, then a client that
looks up <random>.probe.netdoctor.example reveals which resolver its ISP really
uses (useful to detect DNS interception and far-away resolvers).

Run:  python server/dns_logger.py --zone probe.netdoctor.example --port 53
"""
import argparse
import json
import socket
import time

import dns.message
import dns.rrset

ap = argparse.ArgumentParser()
ap.add_argument("--zone", required=True)
ap.add_argument("--port", type=int, default=53)
ap.add_argument("--log", default="dns_log.jsonl")
a = ap.parse_args()
zone = a.zone.rstrip(".").lower() + "."
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", a.port))
print(f"DNS logger for {zone} on :{a.port}")
with open(a.log, "a") as log:
    while True:
        data, addr = s.recvfrom(4096)
        try:
            q = dns.message.from_wire(data)
            name = q.question[0].name.to_text().lower()
            r = dns.message.make_response(q)
            if name.endswith(zone):
                r.answer.append(dns.rrset.from_text(name, 30, "IN", "A", "192.0.2.1"))
                log.write(json.dumps({"ts": time.time(), "resolver": addr[0], "name": name}) + "\n")
                log.flush()
            s.sendto(r.to_wire(), addr)
        except Exception:
            continue
