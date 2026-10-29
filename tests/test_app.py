"""Desktop app API + history store, using a temp home and a stored lab run."""
import json
import urllib.error
import urllib.request

import pytest


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import netdoctor.store.history as h

    h.HOME = tmp_path_factory.mktemp("ndhome")
    from netdoctor.app import server as srv
    from netdoctor.lab_runner import Net, build_run

    httpd, url = srv.serve(0)
    run_id = srv.STATE.history.add_run(build_run(Net(bloat_ul=260)))
    srv.STATE.history.add_watch({"ts": "2026-10-29T21:00:00+05:30", "internet_ms": 20, "score": 90})
    yield url, run_id
    httpd.shutdown()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read()


def test_state_and_pages(server):
    url, _ = server
    status, body = get(url + "api/state")
    assert status == 200 and json.loads(body)["last"]["culprit"] == "isp_bufferbloat"
    assert get(url)[0] == 200
    assert get(url + "static/app.js")[0] == 200


def test_run_view_and_reports(server):
    url, rid = server
    v = json.loads(get(f"{url}api/run/{rid}")[1])
    assert v["verdict"]["culprit"] == "isp_bufferbloat" and "features" in v
    rep = json.loads(get(f"{url}api/run/{rid}/isp-report")[1])
    assert "NetDoctor connection report" in rep["text"]
    assert b"<html" in get(f"{url}api/run/{rid}/isp-report.html")[1]
    assert json.loads(get(f"{url}api/run/{rid}/json")[1])["version"] == 1


def test_history(server):
    url, _ = server
    h = json.loads(get(url + "api/history")[1])
    assert len(h["runs"]) >= 1 and len(h["heatmap"]) == 7 and len(h["heatmap"][0]) == 24


def test_settings_roundtrip(server):
    url, _ = server
    req = urllib.request.Request(url + "api/settings", data=json.dumps({"plan_down_mbps": 300, "bogus": 1}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        s = json.loads(r.read())
    assert s["plan_down_mbps"] == 300 and "bogus" not in s


def test_other_origins_are_blocked(server):
    url, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url + "api/state", {"Origin": "http://evil.example"})
    assert e.value.code == 403


def test_static_path_traversal_blocked(server):
    url, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url + "static/..%2Fserver.py")
    assert e.value.code == 404
