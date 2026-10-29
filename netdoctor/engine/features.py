"""Turn raw measurements into named features the rules can reason about.

Pure: no network, no clock, no randomness.
"""
from __future__ import annotations

from ..models.run import Run
from ..probes.util import ip_kind, median, summarize

PHASES = ("idle", "download", "upload")
LATENCY_PROBES = ("icmp", "tcp_ping")

# ---- thresholds (PROJECT.md §6.3) ----------------------------------------
GRADES = [(5, "A+"), (30, "A"), (60, "B"), (200, "C"), (400, "D")]


def lag_grade(extra_ms: float | None) -> str | None:
    if extra_ms is None:
        return None
    for limit, g in GRADES:
        if extra_ms < limit:
            return g
    return "F"


GRADE_MEANING = {
    "A+": "Perfect for calls and gaming",
    "A": "Great",
    "B": "Good, minor lag possible",
    "C": "Calls may stutter during big uploads or downloads",
    "D": "Noticeable lag whenever the connection is busy",
    "F": "Calls and games unusable while anything downloads",
}


def rssi_label(rssi):
    if rssi is None:
        return None
    return "Good" if rssi >= -60 else "Fair" if rssi >= -70 else "Poor"


def dns_label(ms):
    if ms is None:
        return None
    return "Good" if ms < 30 else "Fair" if ms <= 100 else "Poor"


def loss_label(loss):
    if loss is None:
        return None
    return "Good" if loss < 0.005 else "Fair" if loss <= 0.02 else "Poor"


# ---- extraction ----------------------------------------------------------

def _series(run: Run, target: str, phase: str) -> list[float | None]:
    out: list[float | None] = []
    for m in run.measurements:
        if m.probe in LATENCY_PROBES and m.target == target and m.phase == phase and m.samples_ms:
            out.extend(m.samples_ms)
    return out


def _trace_features(m, net_idle_samples: list | None) -> dict:
    hops = m.meta.get("hops", [])
    responding = [h for h in hops if h.get("ip") and h.get("samples_ms")]
    t = {"target": m.target, "hops": hops, "error": m.meta.get("error")}
    if not responding:
        t.update(final_loss=None, reachable=False)
        return t
    final = responding[-1]
    reached = final.get("ip") == m.target
    samples = list(final.get("samples_ms") or [])
    if net_idle_samples and reached:
        samples += net_idle_samples  # more samples = finer loss resolution
    fs = summarize(samples)
    final_loss = fs["loss"]
    if reached and final_loss == 1.0:
        # Destination doesn't answer probes at all: can't judge loss.
        t.update(final_loss=None, reachable=None)
        return t
    t.update(final_loss=final_loss, final_median=fs["median"], reachable=reached, final_samples=len(samples))

    # Where does loss start? Pick the split point where pooled loss after it,
    # minus pooled loss before it, is largest. Pooling keeps one unlucky hop
    # (few samples) from moving the answer. Hops that never answer pings are
    # ignored: they just don't reply to probes.
    origin = None
    if final_loss and final_loss >= 0.02:
        usable = [h for h in responding[:-1] if (h.get("loss") or 0) < 1.0]
        chain = usable + [final]
        counts = []
        for h in usable:
            sm = h.get("samples_ms") or []
            counts.append((sum(1 for x in sm if x is None), len(sm)))
        counts.append((round(final_loss * len(samples)), len(samples)))
        best, best_score = len(chain) - 1, -1.0
        for i in range(len(chain)):
            lost_a = sum(c[0] for c in counts[i:])
            n_a = sum(c[1] for c in counts[i:]) or 1
            lost_b = sum(c[0] for c in counts[:i])
            n_b = sum(c[1] for c in counts[:i])
            after = lost_a / n_a
            before = lost_b / n_b if n_b else 0.0
            if after < 0.5 * final_loss:
                continue
            score = after - before
            if score > best_score + 0.005:
                best, best_score = i, score
        origin = chain[best]
    t["loss_origin"] = {"n": origin["n"], "ip": origin["ip"], "kind": origin.get("kind")} if origin else None

    # Where does delay jump? Must persist to the end to count.
    meds = [(h, (h.get("stats") or {}).get("median")) for h in responding]
    meds = [(h, md) for h, md in meds if md is not None]
    jump = None
    for i in range(1, len(meds)):
        before = min(md for _, md in meds[:i])
        h, md = meds[i]
        rest = [x for _, x in meds[i:]]
        if md - before >= 40 and min(rest) >= before + 0.75 * (md - before):
            jump = {"n": h["n"], "ip": h["ip"], "kind": h.get("kind"), "ms": round(md - before, 1)}
            break
    t["latency_jump"] = jump

    # Harmless middle-hop loss (rate-limited replies) for the per-hop chart note.
    t["harmless_loss_hops"] = [
        h["n"] for h in responding[:-1] if (h.get("loss") or 0) >= 0.1 and (final_loss or 0) < 0.02
    ]
    return t


def _pooled_loss_split(traces: list[dict]) -> dict | None:
    """Where does loss start, using every route at once?

    Both routes cross the same home and ISP network, so we pool probe counts by
    segment (home -> ISP -> internet -> destination) and find the split point
    where loss after it minus loss before it is largest.
    """
    usable = [t for t in traces if t.get("final_loss") is not None]
    if not usable:
        return None
    order = ["home", "isp", "internet", "destination"]
    buckets = {k: [0, 0] for k in order}
    for t in usable:
        responding = [h for h in t["hops"] if h.get("ip") and h.get("samples_ms")]
        for h in responding[:-1]:
            if (h.get("loss") or 0) >= 1.0 or h.get("kind") not in buckets:
                continue  # hop ignores pings entirely
            sm = h["samples_ms"]
            buckets[h["kind"]][0] += sum(1 for x in sm if x is None)
            buckets[h["kind"]][1] += len(sm)
        n = t.get("final_samples") or 20
        buckets["destination"][0] += round(t["final_loss"] * n)
        buckets["destination"][1] += n
    chain = [k for k in order if buckets[k][1]]
    final = buckets["destination"][0] / max(1, buckets["destination"][1])
    out = {"final_loss": round(final, 4), "targets": len(usable),
           "lossy_targets": sum(1 for t in usable if t["final_loss"] >= 0.02),
           "by_segment": {k: round(buckets[k][0] / buckets[k][1], 4) for k in chain}, "origin": None}
    if final < 0.02:
        return out
    best, best_score, best_after = chain[-1], -1.0, final
    split_scores: dict[str, float] = {}
    for i, k in enumerate(chain):
        own = buckets[k][0] / buckets[k][1]
        la = sum(buckets[c][0] for c in chain[i:])
        na = sum(buckets[c][1] for c in chain[i:])
        lb = sum(buckets[c][0] for c in chain[:i])
        nb = sum(buckets[c][1] for c in chain[:i])
        after, before = la / na, (lb / nb if nb else 0.0)
        if after < 0.5 * final or own < 0.5 * final:
            continue  # loss can't start in a segment that has none of its own
        split_scores[k] = after - before
        if after - before > best_score:
            best, best_score, best_after = k, after - before, after
    out["origin"] = best
    # Ambiguous if a different segment explains the data almost as well.
    runner_up = max((v for k, v in split_scores.items() if k != best), default=-1.0)
    out["ambiguous"] = bool(best_score - runner_up < 0.02)
    out["loss_from_origin"] = round(best_after, 4)  # more samples than the destination alone
    return out


def extract(run: Run) -> dict:
    ctx = run.context or {}
    F: dict = {
        "link": ctx.get("link", "unknown"),
        "vpn": bool(ctx.get("vpn")),
        "online": ctx.get("online"),
        "captive": ctx.get("captive_portal"),
        "gateway": ctx.get("gateway"),
        "double_nat": ctx.get("double_nat"),
        "cgnat": ctx.get("cgnat"),
        "load_ran": False,
    }

    # Latency per phase to the router and the internet.
    F["gw"] = {p: summarize(_series(run, "gateway", p)) for p in PHASES}
    F["net"] = {p: summarize(_series(run, "internet", p)) for p in PHASES}
    for key in ("gw", "net"):
        for p in PHASES:
            if not F[key][p]["n"]:
                F[key][p] = None

    def extra(key: str, phase: str):
        a, b = F[key].get(phase), F[key].get("idle")
        if not a or not b or a["median"] is None or b["median"] is None:
            return None
        return max(0.0, round(a["median"] - b["median"], 1))

    F["bloat"] = {p: extra("net", p) for p in ("download", "upload")}
    F["local_bloat"] = {p: extra("gw", p) for p in ("download", "upload")}
    vals = [v for v in F["bloat"].values() if v is not None]
    F["bloat_worst"] = max(vals) if vals else None
    F["bloat_worst_dir"] = max(F["bloat"], key=lambda k: F["bloat"][k] or -1) if vals else None
    lvals = [v for v in F["local_bloat"].values() if v is not None]
    F["local_bloat_worst"] = max(lvals) if lvals else None
    F["grade"] = lag_grade(F["bloat_worst"])

    # Load test.
    F["load"] = {}
    for m in run.find("load"):
        F["load"][m.phase] = m.meta
        F["load_ran"] = True

    # Wi-Fi.
    w = run.first("wifi")
    F["wifi"] = w.meta if w and w.meta.get("available") else None
    F["wifi_error"] = w.meta.get("error") if w and not w.meta.get("available") else None

    # DNS.
    sys_dns, pub_dns = [], []
    for m in run.find("dns"):
        s = summarize(m.samples_ms)
        entry = {"ip": m.target, "label": m.meta.get("label", m.target), "median": s["median"],
                 "fail": s["loss"], "is_gateway": m.meta.get("is_gateway", False),
                 "kind": ip_kind(m.target)}
        (sys_dns if m.meta.get("system") else pub_dns).append(entry)
    F["dns_system"] = sys_dns[0] if sys_dns else None
    good_pub = [p for p in pub_dns if p["median"] is not None]
    F["dns_best_public"] = min(good_pub, key=lambda p: p["median"]) if good_pub else None
    F["dns_public"] = pub_dns
    di = run.first("dns_intercept")
    F["dns_intercepted"] = di.meta.get("intercepted") if di else None

    # Traces.
    # Idle samples per internet host (the sampler pings 1-2 internet targets).
    idle_by_host: dict[str, list] = {}
    for m in run.measurements:
        if m.probe in LATENCY_PROBES and m.phase == "idle" and m.target != "gateway" and m.meta.get("host"):
            idle_by_host.setdefault(m.meta["host"], []).extend(m.samples_ms or [])
    F["traces"] = [_trace_features(m, idle_by_host.get(m.target)) for m in run.find("trace")]
    F["loss_split"] = _pooled_loss_split(F["traces"])
    for t in F["traces"]:
        for h in t["hops"]:
            if h.get("double_nat"):
                F["double_nat"] = True
            if h.get("cgnat"):
                F["cgnat"] = True

    # Websites.
    target = None
    controls = []
    for m in run.find("http"):
        if m.meta.get("role") == "target":
            target = m.meta
        else:
            controls.append(m.meta)
    F["http_target"] = target
    F["http_controls"] = controls
    ok = [c["ttfb_ms"] for c in controls if c.get("ttfb_ms") is not None and not c.get("error")]
    F["controls_ttfb"] = median(ok)
    F["controls_failed"] = sum(1 for c in controls if c.get("error"))
    sv = run.first("server_view")
    F["server_view"] = sv.meta if sv else None

    m = run.first("mtu")
    F["mtu"] = m.meta if m else None
    m = run.first("ipv6")
    F["ipv6"] = m.meta if m else None
    return F
