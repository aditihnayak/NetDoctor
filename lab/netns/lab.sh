#!/usr/bin/env bash
# NetDoctor fault-injection lab on real Linux networking (namespaces + tc netem).
#
#   client ── router ── isp ── internet
#   192.168.1.0/24   198.51.100.0/30   203.0.113.0/30   1.1.1.1, 8.8.8.8, 9.9.9.9, 203.0.113.10-11
#
# Usage (Linux, root):  sudo lab/netns/lab.sh [scenario|all]
# Needs: iproute2, iptables, traceroute, python3 with netdoctor installed (uv sync).
# Scenarios not reproducible here (Wi-Fi radio, IPv6, double NAT) are covered by
# the simulated lab: `netdoctor lab`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
NS=(nd-client nd-router nd-isp nd-net)
PIDS=()

ex() { ip netns exec "$1" "${@:2}"; }

cleanup() {
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  PIDS=()
  for n in "${NS[@]}"; do ip netns del "$n" 2>/dev/null || true; done
  rm -rf /etc/netns/nd-client
}

link() { # link nsA ifA ipA nsB ifB ipB
  ip link add "$2" type veth peer name "$5"
  ip link set "$2" netns "$1"; ip link set "$5" netns "$4"
  ex "$1" ip addr add "$3" dev "$2"; ex "$1" ip link set "$2" up
  ex "$4" ip addr add "$6" dev "$5"; ex "$4" ip link set "$5" up
}

bg() { "$@" >/dev/null 2>&1 & PIDS+=($!); }

setup() {
  cleanup
  for n in "${NS[@]}"; do ip netns add "$n"; ex "$n" ip link set lo up; done
  link nd-client c0 192.168.1.100/24 nd-router r0 192.168.1.1/24
  link nd-router r1 198.51.100.2/30 nd-isp i0 198.51.100.1/30
  link nd-isp i1 203.0.113.1/30 nd-net n0 203.0.113.2/30
  for a in 1.1.1.1 8.8.8.8 9.9.9.9 203.0.113.10 203.0.113.11; do ex nd-net ip addr add "$a/32" dev lo; done
  ex nd-isp ip addr add 198.51.100.53/32 dev lo
  for n in nd-router nd-isp; do ex "$n" sysctl -qw net.ipv4.ip_forward=1; done
  ex nd-client ip route add default via 192.168.1.1
  ex nd-router ip route add default via 198.51.100.1
  ex nd-router iptables -t nat -A POSTROUTING -o r1 -j MASQUERADE
  ex nd-isp ip route add default via 203.0.113.2
  ex nd-net ip route add default via 203.0.113.1
  ex nd-client sysctl -qw net.ipv4.ping_group_range="0 2147483647"
  mkdir -p /etc/netns/nd-client
  echo "nameserver ${RESOLVER:-198.51.100.53}" > /etc/netns/nd-client/resolv.conf
  bg ex nd-net "$PY" "$HERE/fake_internet.py" web --port 80
  bg ex nd-net "$PY" "$HERE/fake_internet.py" dns --port 53
  bg ex nd-isp "$PY" "$HERE/fake_internet.py" dns --port 53 --delay "${ISP_DNS_DELAY:-5}"
  sleep 1
}

run_nd() { # run_nd <expected> [netdoctor args...]
  local expected="$1"; shift
  local out
  out=$(ex nd-client env NETDOCTOR_LAB=1 NETDOCTOR_HOME=/tmp/nd-lab \
      NETDOCTOR_DOWNLOAD_URL=http://203.0.113.10/big NETDOCTOR_UPLOAD_URL=http://203.0.113.10/up \
      NETDOCTOR_CONTROL_SITES=http://203.0.113.10 NETDOCTOR_CAPTIVE_URL=http://203.0.113.10/hotspot \
      NETDOCTOR_DUALSTACK_HOST=203.0.113.10 NETDOCTOR_ISP_PREFIXES=198.51.100.0/24 \
      NETDOCTOR_LOAD_STREAMS=4 \
      "$PY" -m netdoctor --json "$@")
  local got
  got=$(echo "$out" | "$PY" -c 'import json,sys; v=json.load(sys.stdin)["verdict"]; print(v["culprit"], v["confidence_label"])')
  if [[ " $expected " == *" ${got%% *} "* ]]; then echo "  ✓ $got"; PASS=$((PASS+1)); else echo "  ✗ got $got, expected $expected"; FAIL=$((FAIL+1)); fi
}

scenario() {
  local name="$1"
  echo "== $name"
  case "$name" in
    clean) setup; run_nd "none" ;;
    isp-bufferbloat)  # big dumb upload queue at the modem/ISP edge
      setup
      ex nd-router tc qdisc add dev r1 root tbf rate 20mbit burst 32kbit latency 600ms
      ex nd-isp tc qdisc add dev i0 root tbf rate 60mbit burst 32kbit latency 300ms
      run_nd "isp_bufferbloat" ;;
    local-queue)  # queue on the client's own link (like a congested access point)
      setup
      ex nd-client tc qdisc add dev c0 root tbf rate 15mbit burst 32kbit latency 600ms
      ex nd-router tc qdisc add dev r0 root tbf rate 40mbit burst 32kbit latency 400ms
      run_nd "local_queue" ;;
    isp-loss)
      setup
      ex nd-isp tc qdisc add dev i0 root netem loss 8%
      ex nd-isp tc qdisc add dev i1 root netem loss 4%
      run_nd "isp inconclusive" ;;
    middle-hop-loss)  # ISP router rate-limits its own ICMP replies: harmless
      setup
      ex nd-isp iptables -A OUTPUT -p icmp -m statistic --mode random --probability 0.5 -j DROP
      run_nd "none" ;;
    slow-dns)
      ISP_DNS_DELAY=300 setup; run_nd "dns" ;;
    slow-website)
      setup
      bg ex nd-net "$PY" "$HERE/fake_internet.py" web --port 8080 --delay 4
      ex nd-net iptables -t nat -A PREROUTING -d 203.0.113.11 -p tcp --dport 80 -j REDIRECT --to-port 8080
      run_nd "website" http://203.0.113.11 ;;
    busy)  # another device downloading flat out through a bloated queue
      setup
      ex nd-isp tc qdisc add dev i0 root tbf rate 30mbit burst 32kbit latency 400ms
      ip netns add nd-other; ex nd-other ip link set lo up
      link nd-other o0 192.168.2.100/24 nd-router r2 192.168.2.1/24
      ex nd-other ip route add default via 192.168.2.1
      bg ex nd-other bash -c 'while true; do curl -s -o /dev/null http://203.0.113.10/big; done'
      sleep 3
      run_nd "busy_network isp_bufferbloat"
      ip netns del nd-other 2>/dev/null || true ;;
    mtu-blackhole)  # drop >1400-byte packets silently, no "too big" messages
      setup
      ex nd-isp iptables -A FORWARD -m length --length 1401:65535 -j DROP
      ex nd-isp iptables -A OUTPUT -p icmp --icmp-type fragmentation-needed -j DROP
      run_nd "mtu" ;;
    gigabit) setup; run_nd "none" ;;
    *) echo "unknown scenario $name"; exit 2 ;;
  esac
  cleanup
}

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)."; exit 1; }
trap cleanup EXIT
PASS=0; FAIL=0
ALL=(clean isp-bufferbloat local-queue isp-loss middle-hop-loss slow-dns slow-website busy mtu-blackhole gigabit)
if [[ "${1:-all}" == "all" ]]; then for s in "${ALL[@]}"; do scenario "$s"; done; else scenario "$1"; fi
echo; echo "passed $PASS, failed $FAIL"
[[ $FAIL -eq 0 ]]
