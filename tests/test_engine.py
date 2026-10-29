"""Diagnosis engine: fault-injection scenarios, honesty rules, replay."""
import dataclasses
import json
from pathlib import Path

import pytest

from netdoctor.engine import diagnose, lag_grade
from netdoctor.lab_runner import ISP_VERDICTS, SCENARIOS, Net, build_run, evaluate
from netdoctor.models.run import Run

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario(sc):
    v = build_run(sc.net).verdict
    assert v["culprit"] in sc.expected, f"{sc.name}: got {v['culprit']} ({v['scores']})"


def test_lab_accuracy_target():
    res = evaluate()
    assert sum(r["ok"] for r in res) / len(res) >= 0.9


@pytest.mark.parametrize("sc", [s for s in SCENARIOS if not (s.expected & ISP_VERDICTS)], ids=lambda s: s.name)
def test_never_blames_isp_falsely_across_seeds(sc):
    """Rule of honesty: a false 'it's your ISP' is the most damaging mistake."""
    for seed in range(1, 41):
        v = build_run(dataclasses.replace(sc.net, seed=seed)).verdict
        assert v["culprit"] not in ISP_VERDICTS, f"{sc.name} seed {seed}: blamed ISP"


def test_isp_loss_robust_across_seeds():
    sc = next(s for s in SCENARIOS if s.name == "Loss starting at first ISP hop")
    hits = sum(build_run(dataclasses.replace(sc.net, seed=s)).verdict["culprit"] == "isp" for s in range(1, 61))
    assert hits / 60 >= 0.75


def test_single_target_loss_is_not_enough_to_blame_isp():
    v = build_run(Net(loss_hop=2, loss_rate=0.08, loss_targets=("1.1.1.1",), link="wired")).verdict
    assert v["culprit"] != "isp" or v["confidence_label"] in ("Low",)


def test_isp_verdict_needs_clean_home_network():
    # Same ISP loss, but the Wi-Fi is also a mess: don't pin it on the ISP.
    v = build_run(Net(loss_hop=2, loss_rate=0.08, rssi=-78, gw_spike_rate=0.3, gw_spike_ms=200, gw_loss=0.04)).verdict
    assert v["culprit"] not in ISP_VERDICTS


def test_vpn_lowers_isp_confidence():
    r = build_run(Net(loss_hop=2, loss_rate=0.10, link="wired"))
    base = r.verdict["confidence"]
    r.context["vpn"] = True
    v = diagnose(r)
    assert v["culprit"] != "isp" or v["confidence"] < base


@pytest.mark.parametrize("extra,grade", [(2, "A+"), (20, "A"), (45, "B"), (100, "C"), (300, "D"), (900, "F"), (None, None)])
def test_lag_grades(extra, grade):
    assert lag_grade(extra) == grade


def test_verdict_shape():
    v = build_run(Net()).verdict
    for key in ("culprit", "title", "message", "confidence", "confidence_label", "segments", "actions", "key_numbers"):
        assert key in v
    assert set(v["segments"]) == {"wifi", "router", "isp", "internet", "dns", "website"}
    for s in v["segments"].values():
        assert s["status"] in ("good", "fair", "poor", "unknown")


def test_wifi_actions_are_specific():
    v = build_run(Net(rssi=-62, channel=6, neighbours=9, gw_spike_rate=0.2, gw_spike_ms=120)).verdict
    ids = [a["id"] for a in v["actions"]]
    assert any(i.startswith("change_channel:") for i in ids), ids
    assert "channel 6" in " ".join(a["text"] for a in v["actions"])


def test_engine_is_pure_and_deterministic():
    r = build_run(Net(bloat_ul=260))
    assert diagnose(r) == diagnose(Run.from_dict(json.loads(r.to_json())))


def test_replay_real_run_fixture():
    run = Run.load(FIXTURES / "real_macos_wifi.json")
    v = diagnose(run)
    assert v["culprit"] == "none"
    assert v["key_numbers"]["lag_grade"] == "A+"
    assert v["segments"]["wifi"]["status"] == "good"


def test_empty_run_is_inconclusive_not_a_guess():
    v = diagnose(Run(context={"link": "wifi", "gateway": "192.168.1.1"}))
    assert v["culprit"] in ("inconclusive", "offline")
