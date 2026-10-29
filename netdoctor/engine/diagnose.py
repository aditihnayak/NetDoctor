"""Evidence rules, scoring and verdicts.

diagnose(run) is a pure function: the same measurements always give the same
verdict. Each candidate verdict gets a score 0..1 from its evidence; the best
one wins if it is clearly ahead, otherwise we say "inconclusive".

Rule of honesty: a false "it's your ISP" verdict is the most damaging mistake,
so ISP verdicts need more evidence (higher bars) than any other.
"""
from __future__ import annotations

from ..models.run import Run
from .features import GRADE_MEANING, dns_label, extract, lag_grade, rssi_label

ENGINE_VERSION = 1
ISP_VERDICTS = {"isp", "no_internet"}
SEGMENTS = ["wifi", "router", "isp", "internet", "dns", "website"]
SEGMENT_NAMES = {"wifi": "Wi-Fi", "router": "Router", "isp": "ISP", "internet": "Internet",
                 "dns": "DNS", "website": "Websites"}

TITLES = {
    "wifi": "Your Wi-Fi is the main problem",
    "local_queue": "Your Wi-Fi or router gets congested when the connection is busy",
    "router": "Your router is slowing things down",
    "isp": "The problem is with your internet provider",
    "isp_bufferbloat": "Your connection lags when it's busy",
    "dns": "Looking up website names is slow",
    "mtu": "Some sites may hang because large data packets get lost",
    "ipv6": "A connection setting is making first page loads slow",
    "busy_network": "Another device is using a lot of your connection",
    "website": "Your connection is fine. The website itself is slow",
    "website_missing": "That website name doesn't exist",
    "internet": "There's a problem out on the wider internet",
    "captive": "This network wants you to sign in first",
    "offline": "You're not connected to a network",
    "no_internet": "Your router is reachable, but the internet isn't",
    "none": "Your connection looks healthy",
    "inconclusive": "We couldn't pinpoint the problem this time",
}

# Which segment each verdict blames (for the path map).
BLAME = {"wifi": "wifi", "local_queue": "router", "router": "router", "isp": "isp",
         "isp_bufferbloat": "isp", "dns": "dns", "mtu": "internet", "ipv6": "router",
         "busy_network": "router", "website": "website", "website_missing": "website",
         "internet": "internet", "captive": "router", "offline": "wifi", "no_internet": "isp"}


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ramp(x: float | None, a: float, b: float) -> float:
    """0 at a, 1 at b, linear between."""
    if x is None:
        return 0.0
    return clamp((x - a) / (b - a))


def _fmt_ms(x):
    if x is None:
        return "?"
    return f"{x:.0f} ms" if x >= 10 else f"{x:.1f} ms"


# ---------------------------------------------------------------------------
class Diagnosis:
    def __init__(self, F: dict) -> None:
        self.F = F
        self.scores: dict[str, float] = {}
        self.evidence: dict[str, list[tuple[str, str]]] = {}
        self.actions: dict[str, list[tuple[str, str]]] = {}

    def ev(self, verdict: str, eid: str, text: str) -> None:
        self.evidence.setdefault(verdict, []).append((eid, text))

    def act(self, verdict: str, aid: str, text: str) -> None:
        self.actions.setdefault(verdict, []).append((aid, text))

    # ---- individual rules ------------------------------------------------
    def rule_connectivity(self) -> None:
        F = self.F
        gw_idle, net_idle = F["gw"]["idle"], F["net"]["idle"]
        if F["captive"]:
            self.scores["captive"] = 1.0
            self.ev("captive", "captive_portal", "Web requests are being redirected to a sign-in page.")
            self.act("captive", "open_portal", "Open any website in your browser and complete the network's sign-in page.")
            return
        gw_dead = gw_idle is not None and gw_idle["loss"] == 1.0
        net_dead = net_idle is not None and net_idle["loss"] == 1.0
        if not F["gateway"] and (net_idle is None or net_dead):
            self.scores["offline"] = 1.0
            self.ev("offline", "no_gateway", "Your device has no route to a router.")
            self.act("offline", "reconnect", "Reconnect to your Wi-Fi network or check the network cable.")
            return
        if gw_dead and net_dead:
            self.scores["offline"] = 0.9
            self.ev("offline", "gateway_unreachable", "Your router doesn't respond at all.")
            self.act("offline", "restart_router", "Restart your router (unplug it for 30 seconds), then reconnect.")
            return
        if gw_idle and gw_idle["loss"] < 0.5 and net_dead and F["online"] is False:
            self.scores["no_internet"] = 0.85
            self.ev("no_internet", "internet_unreachable",
                    "Your router answers, but nothing on the internet does.")
            self.act("no_internet", "restart_router",
                     "Restart your modem and router. If the internet light stays red or off, check your provider's outage page.")
            self.act("no_internet", "contact_isp", "If it's still down after a restart, contact your provider: it's likely an outage.")

    def rule_wifi(self) -> None:
        F = self.F
        if F["link"] != "wifi":
            return
        gw = F["gw"]["idle"]
        w = F["wifi"] or {}
        rssi, snr = w.get("rssi_dbm"), w.get("snr_db")
        s_gw = 0.0
        if gw:
            spikes = (gw["p95"] or 0) - (gw["median"] or 0)
            s_gw = max(ramp(spikes, 10, 60), ramp(gw["loss"], 0.01, 0.06), ramp(gw["median"], 6, 40))
            if s_gw > 0.25:
                self.ev("wifi", "gateway_jitter_high",
                        f"Delays to your router jump from {_fmt_ms(gw['min'])} to {_fmt_ms(gw['max'])}"
                        + (f", with {gw['loss']*100:.0f}% of probes lost" if gw["loss"] >= 0.01 else "") + ".")
        s_sig = max(ramp(-(rssi or -50), 65, 78), ramp(25 - (snr if snr is not None else 40), 0, 15))
        if rssi is not None and rssi < -70:
            self.ev("wifi", "rssi_weak", f"Your Wi-Fi signal is weak ({rssi} dBm).")
        elif rssi is not None and rssi < -60:
            self.ev("wifi", "rssi_fair", f"Your Wi-Fi signal is only fair ({rssi} dBm).")
        cong = w.get("neighbours_on_channel") or 0
        s_cong = 1.0 if w.get("congested") else ramp(cong, 2, 8) * 0.6
        if w.get("congested"):
            self.ev("wifi", "channel_congested",
                    f"{cong} nearby networks share or overlap your channel ({w.get('channel')}).")
        score = 0.6 * s_gw + 0.25 * s_sig + 0.15 * s_cong
        if s_gw > 0.5 and (s_sig > 0.5 or s_cong > 0.5):
            score += 0.15
        if s_gw < 0.2:
            score = min(score, 0.42)  # weak signal that isn't hurting right now
        self.scores["wifi"] = clamp(score)
        self._s_gw = s_gw

        # Actions, most effective first.
        band = w.get("band")
        alt = w.get("alt_5ghz_ssid")
        if s_sig > 0.3 or s_gw > 0.4:
            if band == "2.4" and alt:
                self.act("wifi", "move_closer_or_5ghz", f'Move closer to your router, or switch to the 5 GHz network "{alt}".')
            elif band == "2.4":
                self.act("wifi", "move_closer_or_5ghz", "Move closer to your router, or connect to its 5 GHz network if it has one.")
            else:
                self.act("wifi", "move_closer", "Move closer to your router, or remove obstacles (walls, metal, aquariums) between you.")
        best = w.get("best_channel")
        if best and w.get("channel") and best != w.get("channel") and cong - (w.get("best_channel_neighbours") or 0) >= 3:
            self.act("wifi", f"change_channel:{best}",
                     f"Change your router from channel {w['channel']} ({cong} neighbours) to channel {best} "
                     f"({w.get('best_channel_neighbours', 0)} neighbours).")
        self.act("wifi", "wired_or_mesh", "For a desk or TV that never moves, use a network cable. For a big home, consider a mesh Wi-Fi system.")

    def rule_local_queue(self) -> None:
        F = self.F
        lb, nb = F["local_bloat_worst"], F["bloat_worst"]
        if lb is None:
            return
        share_ok = nb is None or nb < 30 or lb >= 0.5 * nb
        score = ramp(lb, 20, 150) * (1.0 if share_ok else 0.4)
        self.scores["local_queue"] = score
        if score > 0.2:
            direction = max(F["local_bloat"], key=lambda k: F["local_bloat"][k] or -1)
            idle = F["gw"]["idle"]["median"] if F["gw"]["idle"] else None
            self.ev("local_queue", "gateway_bloat",
                    f"Delay to your router rises from {_fmt_ms(idle)} to {_fmt_ms((idle or 0) + lb)} while {direction}ing.")
            self.act("local_queue", "reduce_heavy_uploads", "Pause big uploads or downloads (cloud backups, game updates) during calls.")
            self.act("local_queue", "update_firmware", "Update your router's firmware; older firmware often has oversized buffers.")
            self.act("local_queue", "enable_sqm", self._sqm_text())
            if F["link"] == "wifi":
                self.act("local_queue", "move_closer_or_5ghz", "Move closer to the router or use 5 GHz: a stronger signal empties the queue faster.")

    def _sqm_text(self) -> str:
        load = self.F["load"]
        dl = (load.get("download") or {}).get("mbps")
        ul = (load.get("upload") or {}).get("mbps")
        if dl and ul:
            return (f'Turn on "Smart Queue", "SQM" or "QoS" in your router and set it to about 90% of your measured '
                    f"speed ({dl*0.9:.0f} Mbps down / {ul*0.9:.0f} Mbps up).")
        return 'Turn on "Smart Queue", "SQM" or "QoS" in your router, set to about 90% of your plan speed.'

    def rule_bufferbloat(self) -> None:
        F = self.F
        nb, lb = F["bloat_worst"], F["local_bloat_worst"] or 0
        if nb is None:
            return
        if nb < 30:
            s = 0.0
        elif nb < 60:
            s = 0.2 + 0.25 * ramp(nb, 30, 60)
        elif nb < 200:
            s = 0.55 + 0.2 * ramp(nb, 60, 200)
        elif nb < 400:
            s = 0.75 + 0.17 * ramp(nb, 200, 400)
        else:
            s = 0.95
        if nb >= 30 and lb >= 0.5 * nb:
            s *= 0.3  # the queue is inside the home: that's local_queue
        self.scores["isp_bufferbloat"] = s
        if s > 0.2:
            d = F["bloat_worst_dir"]
            idle = F["net"]["idle"]["median"] if F["net"]["idle"] else None
            self.ev("isp_bufferbloat", "internet_bloat",
                    f"Delay to the internet rises from {_fmt_ms(idle)} to {_fmt_ms((idle or 0) + nb)} while {d}ing "
                    f"(lag grade {F['grade']}).")
            self.act("isp_bufferbloat", "enable_sqm", self._sqm_text())
            self.act("isp_bufferbloat", "schedule_heavy",
                     "Schedule cloud backups and big downloads for the night, when nobody is on a call.")
            if (F["grade"] or "") in ("D", "F"):
                self.act("isp_bufferbloat", "ask_isp_modem",
                         "If your router has no Smart Queue option, ask your provider for a modem with better queue management (e.g. DOCSIS-PIE / AQM).")

    def rule_busy(self) -> None:
        F = self.F
        net = F["net"]["idle"]
        if not net or net["median"] is None:
            return
        spread = net["median"] - (net["min"] or net["median"])
        tail = (net["p95"] or 0) - (net["min"] or 0)
        gw_unstable = getattr(self, "_s_gw", 0) > 0.4
        score = max(ramp(spread, 15, 80), ramp(tail, 60, 250) * 0.8) * (0.4 if gw_unstable else 1.0)
        self.scores["busy_network"] = score
        if score > 0.2:
            self.ev("busy_network", "idle_latency_high",
                    f"Even before our load test, delay to the internet was {_fmt_ms(net['median'])} "
                    f"(it can be as low as {_fmt_ms(net['min'])}): the line is already busy.")
            self.act("busy_network", "check_other_devices",
                     "Check other devices for big downloads, cloud backups (iCloud, Google Photos, OneDrive), game updates or video uploads.")
            self.act("busy_network", "enable_sqm", self._sqm_text())
            self.act("busy_network", "run_again", "Run the checkup again once those finish to see your connection's true quality.")

    def rule_isp(self) -> None:
        F = self.F
        gw = F["gw"]["idle"]
        gw_clean = gw is not None and gw["loss"] < 0.01 and ((gw["p95"] or 0) - (gw["median"] or 0)) < 20
        traces = [t for t in F["traces"] if t.get("final_loss") is not None]
        ls = F.get("loss_split") or {}
        severity = max(ls.get("final_loss") or 0, ls.get("loss_from_origin") or 0)
        lossy = severity >= 0.03 and ls.get("origin")
        agree = 1.0 if (ls.get("lossy_targets", 0) >= 2 or ls.get("targets") == 1) else 0.45
        if ls.get("ambiguous"):
            agree *= 0.7  # can't tell exactly where the loss starts: be careful
        s_loss = 0.0
        if lossy and ls["origin"] == "isp":
            worst = severity
            s_loss = ramp(worst, 0.02, 0.10) * agree
            hop = next((t["loss_origin"] for t in traces if (t.get("loss_origin") or {}).get("kind") == "isp"), None)
            where = f"at hop {hop['n']} ({hop['ip']}), " if hop else ""
            self.ev("isp", "loss_starts_at_isp",
                    f"Packet loss starts {where}inside your provider's network, and continues all the way to "
                    f"the internet ({worst*100:.0f}% lost; your home network has {ls['by_segment'].get('home', 0)*100:.0f}%).")
        s_lat = 0.0
        jumps = [t for t in traces if (t.get("latency_jump") or {}).get("kind") == "isp" and (t.get("final_median") or 0) >= 100]
        if jumps:
            j = jumps[0]["latency_jump"]
            s_lat = ramp(j["ms"], 50, 200) * 0.7
            self.ev("isp", "latency_jump_at_isp",
                    f"Delay jumps by {j['ms']:.0f} ms at hop {j['n']} ({j['ip']}), inside your provider's network, and stays high.")
        score = max(s_loss, s_lat)
        if not gw_clean:
            score *= 0.3  # trouble at home explains it better
        if F["vpn"]:
            score *= 0.6
        self.scores["isp"] = score
        if score > 0.2:
            self.act("isp", "watch_mode", "Run `netdoctor watch` for a few hours so you have a record of when it happens.")
            self.act("isp", "send_isp_report", "Send your provider the ISP report (it shows exactly where the loss starts).")
            self.act("isp", "restart_modem", "Restart your modem once; if the loss persists, it's on the provider's side.")

        if lossy and ls["origin"] in ("internet", "destination"):
            worst = severity
            only_dest = ls["origin"] == "destination"
            self.scores["internet"] = (ramp(worst, 0.02, 0.10) * 0.8 * agree * (0.6 if only_dest else 1.0)
                                       * (1 if gw_clean else 0.4))
            self.ev("internet", "loss_beyond_isp",
                    f"Packet loss ({worst*100:.0f}%) starts beyond your provider"
                    + (", at the test sites themselves." if only_dest else ", out on the wider internet."))
            self.act("internet", "try_later", "This is usually temporary and outside anyone at home's control. Try again later.")
            self.act("internet", "watch_mode", "If it keeps happening, run `netdoctor watch` and share the ISP report with your provider.")

    def rule_dns(self) -> None:
        F = self.F
        sysd, best = F["dns_system"], F["dns_best_public"]
        if not sysd:
            return
        med, fail = sysd["median"], sysd["fail"] or 0
        slow = ramp(med, 60, 250)
        fails = ramp(fail, 0.1, 0.5)
        better = best is not None and med is not None and best["median"] < 0.6 * med
        s = max(slow, fails) * (1.0 if better or fails > 0 else 0.5)
        is_router = sysd["is_gateway"] or sysd["kind"] == "private"
        key = "router" if is_router else "dns"
        if s > 0.2:
            who = "your router's DNS" if is_router else "your DNS provider"
            msg = f"Name lookups through {who} ({sysd['ip']}) take {_fmt_ms(med)}"
            if fail:
                msg += f" and {fail*100:.0f}% fail"
            if best:
                msg += f"; {best['label']} ({best['ip']}) answers in {_fmt_ms(best['median'])}"
            self.ev(key, "dns_slow", msg + ".")
            if best:
                text = (f"Switch your DNS to {best['label']} ({best['ip']}). In your router's settings this fixes it for "
                        f"every device; on one device, change it in the network settings.")
                if F["dns_intercepted"]:
                    text += (" Note: your provider redirects DNS traffic, so this may not take effect; "
                             "turn on DNS-over-HTTPS (Secure DNS) in your browser instead.")
                self.act(key, "change_dns", text)
        self.scores[f"{key}_dns"] = s
        if key == "dns":
            self.scores["dns"] = s

    def rule_router(self) -> None:
        F = self.F
        parts = [self.scores.pop("router_dns", 0.0)]
        gw = F["gw"]["idle"]
        if F["link"] == "wired" and gw:
            spikes = (gw["p95"] or 0) - (gw["median"] or 0)
            s = max(ramp(gw["median"], 5, 30), ramp(spikes, 15, 60), ramp(gw["loss"], 0.01, 0.05))
            parts.append(s)
            if s > 0.25:
                self.ev("router", "wired_gateway_slow",
                        f"Even over a cable, your router answers slowly or unevenly (median {_fmt_ms(gw['median'])}, "
                        f"up to {_fmt_ms(gw['max'])}).")
                self.act("router", "restart_router", "Restart your router; if it keeps happening, it may be overheating or failing.")
        if F["double_nat"]:
            parts.append(0.42)
            self.ev("router", "double_nat", "There are two routers in a row doing NAT (double NAT).")
            self.act("router", "remove_double_nat",
                     "Put your provider's modem in \"bridge mode\", or set your own router to \"access point mode\". "
                     "This helps gaming, video calls and port forwarding.")
        self.scores["router"] = max(parts)

    def rule_mtu(self) -> None:
        m = self.F["mtu"]
        if not m:
            return
        if m.get("black_hole"):
            s = 0.7
            if self.F["controls_failed"] or (self.F["load"].get("download") or {}).get("failed"):
                s = 0.85
            self.scores["mtu"] = s
            self.ev("mtu", "mtu_black_hole",
                    f"Packets up to {m.get('max_payload')} bytes get through, larger ones silently disappear "
                    "(no \"too big\" message comes back).")
            self.act("mtu", "mss_clamp", 'In your router, turn on "MSS clamping" (sometimes "Clamp MSS to PMTU").')
            if self.F["vpn"]:
                self.act("mtu", "check_vpn_mtu", f"Your VPN may need a lower MTU: try {m.get('path_mtu') or 1400} in its settings.")
            else:
                self.act("mtu", "set_mtu", f"Or set the router's WAN MTU to {m.get('path_mtu') or 1400}.")

    def rule_ipv6(self) -> None:
        v = self.F["ipv6"]
        if not v or not v.get("configured"):
            return
        if v.get("broken"):
            self.scores["ipv6"] = 0.75
            self.ev("ipv6", "ipv6_broken", f"IPv6 connections fail ({v.get('v6_error')}) while IPv4 connects in {_fmt_ms(v.get('v4_ms'))}.")
        elif v.get("slow"):
            self.scores["ipv6"] = 0.45
            self.ev("ipv6", "ipv6_slow", f"IPv6 connects in {_fmt_ms(v.get('v6_ms'))} vs {_fmt_ms(v.get('v4_ms'))} over IPv4.")
        if self.scores.get("ipv6"):
            self.act("ipv6", "fix_ipv6", "In your router, update the firmware or turn IPv6 off (it's usually under WAN or Internet settings).")
            self.act("ipv6", "contact_isp_ipv6", "If turning it off helps, ask your provider to fix IPv6 on your line.")

    def rule_website(self) -> None:
        F = self.F
        t = F["http_target"]
        if not t:
            return
        host = t.get("host")
        if t.get("error") == "name_not_found":
            self.scores["website_missing"] = 0.8 if F["controls_failed"] == 0 else 0.3
            self.ev("website_missing", "name_not_found", f'No website called "{host}" could be found.')
            self.act("website_missing", "check_spelling", "Check the spelling of the address.")
            return
        cm = F["controls_ttfb"]
        if t.get("error"):
            s = 0.75 if F["controls_failed"] == 0 else 0.25
            self.ev("website", "target_unreachable", f"{host} didn't respond ({t['error']}), while other sites loaded normally.")
        else:
            tt = t.get("ttfb_ms")
            if tt is None or cm is None:
                return
            ratio = tt / max(cm, 1)
            s = ramp(ratio, 3, 8) * 0.8 if tt > 800 else 0.0
            if s > 0:
                s = max(s, 0.5)
                self.ev("website", "target_slow", f"{host} took {_fmt_ms(tt)} to start answering; well-known sites took {_fmt_ms(cm)}.")
        sv = F["server_view"]
        if sv and sv.get("slow") and s > 0:
            s = min(1.0, s + 0.15)
            self.ev("website", "slow_from_server", f"{host} is slow from our servers too.")
        self.scores["website"] = s
        if s > 0:
            self.act("website", "try_later", "Nothing to fix at home: try again later, or check the site's status page.")

    # ---- segment statuses ------------------------------------------------
    def segments(self) -> dict:
        F, S = self.F, self.scores
        seg: dict[str, dict] = {}
        gw = F["gw"]["idle"]
        w = F["wifi"]

        # Wi-Fi
        if F["link"] != "wifi":
            seg["wifi"] = {"status": "good" if F["link"] == "wired" else "unknown",
                           "summary": "Not in use: you're on a wired connection" if F["link"] == "wired" else "Couldn't tell how you're connected"}
        elif not w and not gw:
            seg["wifi"] = {"status": "unknown", "summary": "Couldn't measure Wi-Fi"}
        else:
            sc = S.get("wifi", 0)
            parts = []
            rssi = (w or {}).get("rssi_dbm")
            if rssi is not None:
                lab = rssi_label(rssi)
                parts.append({"Good": "Strong", "Fair": "OK", "Poor": "Weak"}[lab] + f" signal ({rssi} dBm)")
            if gw and getattr(self, "_s_gw", 0) > 0.25:
                parts.append(f"Delays to your router jump from {_fmt_ms(gw['min'])} to {_fmt_ms(gw['max'])}")
            elif gw:
                parts.append(f"steady link to your router ({_fmt_ms(gw['median'])})")
            if (w or {}).get("congested"):
                parts.append(f"crowded channel ({w['neighbours_on_channel']} neighbours)")
            status = "poor" if sc >= 0.55 else "fair" if (sc >= 0.3 or rssi_label(rssi) == "Fair" or (w or {}).get("congested")) else "good"
            seg["wifi"] = {"status": status, "summary": _sentence(parts)}

        # Router
        if not gw:
            seg["router"] = {"status": "unknown", "summary": "Couldn't reach your router"}
        else:
            r, lq = S.get("router", 0), S.get("local_queue", 0)
            status = "poor" if max(r, lq) >= 0.55 else "fair" if (max(r, lq) >= 0.3 or F["double_nat"]) else "good"
            if lq >= 0.3:
                summary = "Gets congested when the connection is busy"
            elif r >= 0.3 and self.evidence.get("router"):
                summary = self.evidence["router"][0][1].rstrip(".")
            elif F["double_nat"]:
                summary = "Works, but there's double NAT (two routers in a row)"
            else:
                summary = "No problems found"
            seg["router"] = {"status": status, "summary": summary}

        # ISP
        net = F["net"]["idle"]
        grade = F["grade"]
        if not net:
            seg["isp"] = {"status": "unknown", "summary": "Couldn't measure"}
        else:
            isp = S.get("isp", 0)
            if isp >= 0.55 or S.get("no_internet"):
                status, summary = "poor", (self.evidence.get("isp") or [("", "Connection problems inside your provider's network")])[0][1].rstrip(".")
            elif grade in ("D", "F"):
                status, summary = "poor", f"Slows down a lot while {F['bloat_worst_dir']}ing (lag grade {grade})"
            elif grade == "C":
                status, summary = "fair", f"Fine at idle, but slows down while {F['bloat_worst_dir']}ing (lag grade C)"
            elif isp >= 0.2 or S.get("busy_network", 0) >= 0.4:
                status, summary = "fair", "Some packet loss inside your provider's network" if isp >= 0.2 else "Already busy before the test"
            else:
                status = "good"
                summary = f"No problems found ({_fmt_ms(net['median'])} to the internet" + (f", lag grade {grade})" if grade else ")")
            seg["isp"] = {"status": status, "summary": summary}

        # Internet path
        traces = [t for t in F["traces"] if t.get("final_loss") is not None]
        if not traces:
            seg["internet"] = {"status": "unknown", "summary": "Couldn't trace the route"}
        else:
            worst = max(t["final_loss"] for t in traces)
            beyond = [t for t in traces if (t.get("loss_origin") or {}).get("kind") == "internet"]
            n = len(traces)
            strong = beyond and _agreement(beyond, traces) >= 0.9
            if beyond and worst > 0.02 and strong:
                status, summary = "poor", f"Packet loss beyond your provider ({worst*100:.0f}%)"
            elif beyond and worst > 0.02:
                status, summary = "fair", f"Some packet loss to {beyond[0]['target']} ({worst*100:.0f}%), other sites clean"
            elif beyond and worst >= 0.005:
                status, summary = "fair", f"Slight packet loss ({worst*100:.1f}%)"
            elif S.get("mtu", 0) >= 0.5:
                status, summary = "poor", "Large packets get lost on the way"
            else:
                status, summary = "good", f"Clean path to {n} test site{'s' if n > 1 else ''}"
            seg["internet"] = {"status": status, "summary": summary}

        # DNS
        d = F["dns_system"]
        if not d or d["median"] is None:
            seg["dns"] = {"status": "unknown" if not d else "poor",
                          "summary": "Couldn't test" if not d else "Your DNS server isn't answering"}
        else:
            lab = dns_label(d["median"])
            status = {"Good": "good", "Fair": "fair", "Poor": "poor"}[lab]
            if (d["fail"] or 0) >= 0.2:
                status = "poor"
            summary = {"good": "Fast", "fair": "A bit slow", "poor": "Slow"}[status] + f" ({_fmt_ms(d['median'])})"
            if F["dns_intercepted"]:
                summary += "; your provider redirects DNS lookups"
            seg["dns"] = {"status": status, "summary": summary}

        # Websites
        t, cm = F["http_target"], F["controls_ttfb"]
        if t:
            if t.get("error"):
                seg["website"] = {"status": "poor", "summary": f"{t.get('host')} didn't respond"}
            else:
                tt = t.get("ttfb_ms") or 0
                st = "poor" if S.get("website", 0) >= 0.5 else "fair" if tt > 1000 else "good"
                seg["website"] = {"status": st, "summary": f"{t.get('host')} answered in {_fmt_ms(tt)}"}
        elif F["http_controls"]:
            failed = F["controls_failed"]
            total = len(F["http_controls"])
            if failed == total:
                seg["website"] = {"status": "poor", "summary": "Websites aren't loading"}
            elif failed or (cm or 0) > 1000:
                seg["website"] = {"status": "fair", "summary": f"Some sites slow or failing ({failed}/{total} failed)"}
            else:
                seg["website"] = {"status": "good", "summary": f"Loading normally ({_fmt_ms(cm)} to first byte)"}
        else:
            seg["website"] = {"status": "unknown", "summary": "Not tested"}

        # Context overrides.
        if F["vpn"]:
            for k in ("isp", "internet"):
                if seg[k]["status"] == "good":
                    seg[k]["summary"] += " (through your VPN)"
        return seg

    # ---- verdict ----------------------------------------------------------
    def decide(self) -> dict:
        F = self.F
        for rule in (self.rule_connectivity, self.rule_wifi, self.rule_local_queue, self.rule_bufferbloat,
                     self.rule_busy, self.rule_isp, self.rule_dns, self.rule_router, self.rule_mtu,
                     self.rule_ipv6, self.rule_website):
            rule()
        scores = {k: round(v, 3) for k, v in self.scores.items() if not k.endswith("_dns") and v > 0}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top, top_s = ranked[0] if ranked else ("none", 0.0)
        second_s = ranked[1][1] if len(ranked) > 1 else 0.0

        # Coverage: did we actually measure the basics?
        coverage = sum([F["gw"]["idle"] is not None, F["net"]["idle"] is not None, bool(F["traces"]),
                        F["dns_system"] is not None, F["load_ran"]]) / 5

        if top_s < 0.35:
            if coverage < 0.4:
                culprit, conf = "inconclusive", 0.3
            else:
                culprit = "none"
                conf = 0.6 + 0.35 * coverage
                if top_s >= 0.2:
                    conf = min(conf, 0.7)  # something minor we couldn't explain
        else:
            culprit = top
            margin = top_s - second_s
            conf = clamp(top_s - max(0.0, 0.2 - margin) * 0.75)
            if culprit in ISP_VERDICTS:
                conf -= 0.1  # ISP verdicts need stronger evidence
                if F["vpn"]:
                    conf -= 0.1
            if conf < 0.4 and culprit not in ("captive", "offline"):
                culprit = "inconclusive"

        label = _conf_label(conf, culprit)
        if culprit == "inconclusive":
            label = "Inconclusive"

        # Evidence + actions for the winner, then anything else worth noting.
        ev = list(self.evidence.get(culprit, []))
        actions = list(self.actions.get(culprit, []))
        also = []
        for k, s in ranked:
            if k == culprit or s < 0.35:
                continue
            also.append({"id": k, "title": TITLES.get(k, k), "score": s,
                         "evidence": [t for _, t in self.evidence.get(k, [])]})
            actions += self.actions.get(k, [])[:1]
        if culprit == "inconclusive":
            actions = [("run_again", "Run the checkup again, ideally while the problem is happening."),
                       ("watch_mode", "Turn on watch mode (`netdoctor watch`) to catch problems that come and go.")]
            for k, _ in ranked[:2]:
                ev += self.evidence.get(k, [])
        elif culprit == "none":
            actions = [("watch_mode", "If things still feel slow at certain times, turn on watch mode (`netdoctor watch`) to catch it.")]
            actions += [a for k in ranked[:3] for a in self.actions.get(k[0], [])[:1] if k[1] >= 0.25]

        seen, uniq = set(), []
        for aid, text in actions:
            base = aid.split(":")[0]
            if base not in seen:
                seen.add(base)
                uniq.append({"id": aid, "text": text})

        segs = self.segments()
        if culprit in BLAME and segs[BLAME[culprit]]["status"] in ("good", "fair") and culprit not in ("none", "inconclusive"):
            segs[BLAME[culprit]]["status"] = "poor"

        title = TITLES[culprit]
        if culprit == "router" and [e for e, _ in ev] == ["double_nat"]:
            title = "Minor: your network has two routers in a row (double NAT)"
        return {
            "engine_version": ENGINE_VERSION,
            "culprit": culprit,
            "blame_segment": BLAME.get(culprit),
            "title": title,
            "message": self.message(culprit, ev),
            "confidence": round(conf, 2),
            "confidence_label": label,
            "evidence": [e for e, _ in ev],
            "evidence_text": [t for _, t in ev],
            "actions": uniq[:5],
            "also": also,
            "segments": segs,
            "key_numbers": self.key_numbers(),
            "scores": scores,
            "coverage": round(coverage, 2),
        }

    def message(self, culprit: str, ev: list[tuple[str, str]]) -> str:
        F = self.F
        txt = " ".join(t for _, t in ev[:2])
        if culprit == "wifi":
            tail = " Your internet connection itself is mostly fine." if self.scores.get("isp", 0) < 0.3 else ""
            return (txt + tail).strip()
        if culprit == "none":
            g = F["grade"]
            bits = ["Everything we measured is within healthy ranges."]
            if g:
                bits.append(f"Lag under load: grade {g} ({GRADE_MEANING[g].lower()}).")
            return " ".join(bits)
        if culprit == "inconclusive":
            return ("The evidence was weak or pointed in different directions, so we won't guess. "
                    + (txt or "Try again while the problem is happening."))
        if culprit == "website":
            return txt + " Your own connection is fine."
        if culprit == "isp_bufferbloat":
            return txt + " Your connection is fast, but its queue overfills when busy, which makes calls and games lag."
        return txt or TITLES[culprit] + "."

    def key_numbers(self) -> dict:
        F = self.F
        dl = (F["load"].get("download") or {}).get("mbps")
        ul = (F["load"].get("upload") or {}).get("mbps")
        w = F["wifi"] or {}
        d = F["dns_system"] or {}
        return {
            "download_mbps": dl,
            "upload_mbps": ul,
            "lag_grade": F["grade"],
            "lag_extra_ms": F["bloat_worst"],
            "lag_meaning": GRADE_MEANING.get(F["grade"] or ""),
            "wifi_rssi_dbm": w.get("rssi_dbm") if F["link"] == "wifi" else None,
            "wifi_label": rssi_label(w.get("rssi_dbm")) if F["link"] == "wifi" else None,
            "dns_ms": d.get("median"),
            "dns_label": dns_label(d.get("median")),
            "idle_ms": (F["net"]["idle"] or {}).get("median"),
            "router_ms": (F["gw"]["idle"] or {}).get("median"),
        }


def _agreement(lossy: list[dict], traces: list[dict]) -> float:
    """How much to trust loss seen on some targets.

    Loss seen on only one of several targets, or only at the destination itself
    (which may just be rate-limiting our probes), is weak evidence.
    """
    f = 1.0 if (len(lossy) >= 2 or len(traces) == 1) else 0.45
    if all((t.get("loss_origin") or {}).get("ip") == t["target"] for t in lossy):
        f *= 0.6
    return f


def _conf_label(conf: float, culprit: str) -> str:
    hi, med = (0.85, 0.65) if culprit in ISP_VERDICTS else (0.75, 0.55)
    if conf >= hi:
        return "High"
    if conf >= med:
        return "Medium"
    return "Low"


def _sentence(parts: list[str]) -> str:
    if not parts:
        return "No problems found"
    fixed = [parts[0][0].upper() + parts[0][1:]] + [p[0].lower() + p[1:] for p in parts[1:]]
    return ". ".join(fixed) if len(fixed) > 1 and any(len(p) > 40 for p in fixed) else ", ".join(fixed)


def diagnose(run: Run) -> dict:
    """Measurements in, verdict out. No network access."""
    F = extract(run)
    return Diagnosis(F).decide()


def grade_for(extra_ms):
    return lag_grade(extra_ms)
