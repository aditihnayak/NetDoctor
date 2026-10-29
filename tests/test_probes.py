"""Parsers and helpers (no network)."""
import json

from netdoctor.probes.trace import parse_traceroute
from netdoctor.probes.util import ip_kind, jitter, percentile, summarize
from netdoctor.render.share import anonymise
from netdoctor.lab_runner import Net, build_run
from netdoctor.wifi import analyse_channels
from netdoctor.wifi import linux as wl
from netdoctor.wifi import macos as wm
from netdoctor.wifi import windows as ww
from netdoctor.orchestrator.watch import quality_score

MAC_TRACE = """traceroute to 1.1.1.1 (1.1.1.1), 16 hops max, 40 byte packets
 1  192.168.0.1  3.430 ms
 2  103.189.134.6  4.295 ms
 3  *
 4  172.16.168.1  45.091 ms
 5  1.1.1.1  27.928 ms
"""

WIN_TRACE = """Tracing route to one.one.one.one [1.1.1.1]
over a maximum of 20 hops:

  1    <1 ms    <1 ms    <1 ms  192.168.1.1
  2     8 ms     7 ms     9 ms  10.20.0.1
  3     *        *        *     Request timed out.
  4    12 ms    11 ms    12 ms  1.1.1.1

Trace complete.
"""


def test_parse_traceroute_mac():
    hops = parse_traceroute(MAC_TRACE)
    assert [h["ip"] for h in hops] == ["192.168.0.1", "103.189.134.6", None, "172.16.168.1", "1.1.1.1"]
    assert hops[0]["rtt_ms"] == 3.43


def test_parse_tracert_windows():
    hops = parse_traceroute(WIN_TRACE)
    assert [h["ip"] for h in hops] == ["192.168.1.1", "10.20.0.1", None, "1.1.1.1"]


def test_stats():
    s = summarize([1.0, 2.0, None, 3.0])
    assert s["median"] == 2.0 and s["loss"] == 0.25 and s["n"] == 4
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert jitter([1, 3, 1]) == 2
    assert summarize([])["median"] is None


def test_ip_kind():
    assert ip_kind("192.168.1.1") == "private"
    assert ip_kind("100.72.1.1") == "cgnat"
    assert ip_kind("1.1.1.1") == "public"
    assert ip_kind(None) == "unknown"


def test_macos_wifi_parse():
    data = {"SPAirPortDataType": [{"spairport_airport_interfaces": [{
        "_name": "en0",
        "spairport_current_network_information": {
            "_name": "Home", "spairport_network_channel": "6 (2GHz, 20MHz)",
            "spairport_signal_noise": "-74 dBm / -90 dBm", "spairport_network_rate": 72},
        "spairport_airport_other_local_wireless_networks": [
            {"_name": "Home-5G", "spairport_network_channel": "36 (5GHz, 80MHz)"},
            {"_name": "A", "spairport_network_channel": "6 (2GHz, 20MHz)"},
            {"_name": "B", "spairport_network_channel": "4 (2GHz, 20MHz)"},
        ]}]}]}
    w = wm.parse(data)
    assert w["rssi_dbm"] == -74 and w["snr_db"] == 16 and w["channel"] == 6 and w["band"] == "2.4"
    w.update(analyse_channels(w))
    assert w["neighbours_on_channel"] == 2           # 6 and overlapping 4
    assert w["best_channel"] in (1, 11)
    assert w["alt_5ghz_ssid"] == "Home-5G"


def test_linux_wifi_parse():
    link = "Connected to aa:bb\n\tSSID: Home\n\tfreq: 5180\n\tsignal: -58 dBm\n\ttx bitrate: 866.7 MBit/s 80MHz"
    d = wl.parse_iw_link(link)
    assert d == {"ssid": "Home", "channel": 36, "band": "5", "rssi_dbm": -58, "rate_mbps": 866.7, "width_mhz": 80}
    cur, nets = wl.parse_nmcli("yes:Home:36:5180 MHz:84\nno:Other\\:Net:6:2437 MHz:40\n")
    assert cur["channel"] == 36 and nets[0]["ssid"] == "Other:Net" and nets[0]["rssi_dbm"] == -80


def test_windows_wifi_parse():
    out = """    State                  : connected
    SSID                   : Home
    Radio type             : 802.11ac
    Channel                : 44
    Receive rate (Mbps)    : 866.7
    Signal                 : 80%
"""
    d = ww.parse_interfaces(out)
    assert d["channel"] == 44 and d["band"] == "5" and d["rssi_dbm"] == -60 and d["ssid"] == "Home"


def test_anonymise_removes_identifiers():
    r = build_run(Net())
    r.context["public_ip"] = "81.2.69.160"
    s = json.dumps(anonymise(r))
    assert "81.2.69.160" not in s and "192.168.1.1" not in s and '"ssid"' not in s
    assert "1.1.1.1" in s  # public test targets are kept


def test_watch_quality_score():
    good, issue = quality_score({"internet_ms": 20, "jitter_ms": 1, "loss": 0, "dns_ms": 15, "web_ms": 100, "router_ms": 3})
    assert good >= 90 and issue is None
    bad, issue = quality_score({"internet_ms": 220, "jitter_ms": 60, "loss": 0.08, "dns_ms": 300, "web_ms": None, "router_ms": 3})
    assert bad < 50 and issue
    dead, issue = quality_score({"internet_ms": None})
    assert dead == 0 and issue
