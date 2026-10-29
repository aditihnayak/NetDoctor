"""Local history (SQLite) and settings (JSON), kept in ~/.netdoctor."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from ..models.run import Run

HOME = Path(os.environ.get("NETDOCTOR_HOME", Path.home() / ".netdoctor"))

DEFAULT_SETTINGS = {
    "load_test": True,
    "watch_interval_min": 5,
    "watch_enabled": False,
    "share_results": False,
    "theme": "system",
    "plan_down_mbps": None,
    "plan_up_mbps": None,
}


def _home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME


def load_settings() -> dict:
    p = _home() / "settings.json"
    s = dict(DEFAULT_SETTINGS)
    if p.exists():
        try:
            s.update(json.loads(p.read_text()))
        except Exception:
            pass
    return s


def save_settings(updates: dict) -> dict:
    s = load_settings()
    s.update({k: v for k, v in updates.items() if k in DEFAULT_SETTINGS})
    (_home() / "settings.json").write_text(json.dumps(s, indent=2))
    return s


class History:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_home() / "history.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT, culprit TEXT, title TEXT, confidence TEXT,
                download REAL, upload REAL, grade TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS watch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, router_ms REAL, internet_ms REAL, jitter_ms REAL, loss REAL,
                dns_ms REAL, web_ms REAL, score INTEGER, issue TEXT);
        """)

    # ---- checkups ---------------------------------------------------------
    def add_run(self, run: Run) -> int:
        v = run.verdict or {}
        k = v.get("key_numbers", {})
        cur = self.db.execute(
            "INSERT INTO runs (started_at, culprit, title, confidence, download, upload, grade, data) VALUES (?,?,?,?,?,?,?,?)",
            (run.started_at, v.get("culprit"), v.get("title"), v.get("confidence_label"),
             k.get("download_mbps"), k.get("upload_mbps"), k.get("lag_grade"), run.to_json(indent=None)))
        self.db.commit()
        return cur.lastrowid

    def runs(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, started_at, culprit, title, confidence, download, upload, grade FROM runs ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: int) -> Run | None:
        r = self.db.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.from_dict(json.loads(r["data"])) if r else None

    def last_run(self) -> tuple[int, Run] | None:
        r = self.db.execute("SELECT id, data FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return (r["id"], Run.from_dict(json.loads(r["data"]))) if r else None

    # ---- watch mode -------------------------------------------------------
    def add_watch(self, sample: dict) -> None:
        self.db.execute(
            "INSERT INTO watch (ts, router_ms, internet_ms, jitter_ms, loss, dns_ms, web_ms, score, issue) VALUES (?,?,?,?,?,?,?,?,?)",
            (sample.get("ts") or datetime.now().astimezone().isoformat(timespec="seconds"), sample.get("router_ms"),
             sample.get("internet_ms"), sample.get("jitter_ms"), sample.get("loss"), sample.get("dns_ms"),
             sample.get("web_ms"), sample.get("score"), sample.get("issue")))
        self.db.commit()

    def watch_samples(self, days: int = 14) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM watch WHERE ts >= ? ORDER BY ts", (_days_ago(days),)).fetchall()
        return [dict(r) for r in rows]

    def heatmap(self, days: int = 28) -> list[list[float | None]]:
        """7 x 24 grid (Mon..Sun x hour) of average quality score."""
        grid = [[[] for _ in range(24)] for _ in range(7)]
        for s in self.watch_samples(days):
            try:
                t = datetime.fromisoformat(s["ts"])
            except ValueError:
                continue
            if s["score"] is not None:
                grid[t.weekday()][t.hour].append(s["score"])
        return [[round(sum(c) / len(c)) if c else None for c in row] for row in grid]

    def watch_summary(self, days: int = 14) -> dict:
        s = self.watch_samples(days)
        if not s:
            return {"count": 0}
        bad = [x for x in s if x["issue"]]
        hours: dict[int, list[int]] = {}
        for x in s:
            try:
                hours.setdefault(datetime.fromisoformat(x["ts"]).hour, []).append(x["score"] or 0)
            except ValueError:
                pass
        worst = sorted(hours.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))[:3]
        return {
            "count": len(s), "bad": len(bad), "first": s[0]["ts"], "last": s[-1]["ts"],
            "last_issue": bad[-1]["ts"] if bad else None, "last_issue_text": bad[-1]["issue"] if bad else None,
            "worst_hours": [f"{h:02d}:00–{(h + 1) % 24:02d}:00" for h, v in worst if sum(v) / len(v) < 75],
        }


def _days_ago(days: int) -> str:
    from datetime import timedelta

    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
