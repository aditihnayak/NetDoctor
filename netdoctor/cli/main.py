"""netdoctor command line.

  netdoctor                    30-second checkup
  netdoctor youtube.com        ...including a specific website
  netdoctor --details          full technical report
  netdoctor --json             machine-readable output
  netdoctor --save run.json    save measurements
  netdoctor --isp-report       evidence summary for your ISP
  netdoctor --no-load          skip the load test
  netdoctor watch              lightweight checks every few minutes
  netdoctor replay run.json    re-run the diagnosis on saved measurements
  netdoctor history            past checkups and watch-mode heatmap
  netdoctor app                open the desktop app
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..engine import SEGMENT_NAMES, SEGMENTS, diagnose
from ..models.run import Run
from ..render.isp_report import isp_report_html, isp_report_text
from ..render.text import ICON, STYLE, print_run, sparkline

app = typer.Typer(add_completion=False, no_args_is_help=False, rich_markup_mode=None,
                  help="NetDoctor: a 30-second checkup that tells you, in plain words, why your internet is slow.")
console = Console()
err = Console(stderr=True)
SUBCOMMANDS = {"check", "watch", "replay", "history", "app", "lab", "--help", "-h", "--version"}


# ---- live progress view -------------------------------------------------------
class Progress:
    LABELS = {"wifi": "Checking your Wi-Fi", "router": "Checking your router", "isp": "Checking your provider",
              "internet": "Tracing the route", "dns": "Timing name lookups", "website": "Timing websites"}

    def __init__(self, load: bool) -> None:
        self.state = {k: "waiting" for k in SEGMENTS}
        self.status: dict[str, str] = {}
        self.phase = "Starting"
        self.note = ""
        self.mbps: dict[str, float] = {}
        self.samples: dict[str, list] = {"gateway": [], "internet": []}
        self.t0 = time.time()
        self.budget = 30 if load else 14

    def on_event(self, e: dict) -> None:
        t = e["type"]
        if t == "phase":
            self.phase = e["label"]
            self.note = e.get("note", "")
        elif t == "step":
            self.state[e["segment"]] = e["state"]
            if e.get("label") and e["state"] == "testing":
                self.LABELS[e["segment"]] = e["label"]
        elif t == "segments":
            self.status = {k: v["status"] for k, v in e["segments"].items()}
        elif t == "sample":
            self.samples.setdefault(e["target"], []).append(e["ms"])
        elif t == "throughput":
            self.mbps[e["direction"]] = e["mbps"]
        elif t == "load_done":
            self.mbps[e["direction"]] = e["mbps"]

    def __rich__(self):
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(width=34)
        tbl.add_column()
        for k in SEGMENTS:
            st = self.state[k]
            label = self.LABELS[k]
            dots = "." * max(2, 32 - len(label))
            if st == "done":
                s = self.status.get(k)
                right = Text(f"{ICON[s]} {s}" if s else "done", style=STYLE.get(s, "green"))
            elif st == "testing":
                right = Text("…", style="cyan")
            else:
                right = Text("", style="dim")
            tbl.add_row(Text(f"{label} {dots}", style="dim" if st == "waiting" else ""), right)
        el = time.time() - self.t0
        head = Text(f"{self.phase}", style="bold") + Text(f"   {int(el)}s / ~{self.budget}s", style="dim")
        rows = [head, tbl]
        for d in ("download", "upload"):
            if d in self.mbps:
                frac = min(1.0, el / self.budget)
                rows.append(Text(f"{d.capitalize():>9}: {self.mbps[d]:6.1f} Mbps", style="cyan"))
        g = self.samples.get("internet", [])[-40:]
        if g:
            rows.append(Text("Delay now ", style="dim") + Text(sparkline(g, 40), style="magenta")
                        + Text(f"  {next((x for x in reversed(g) if x is not None), 0):.0f} ms", style="dim"))
        if self.note:
            rows.append(Text(self.note, style="yellow"))
        from rich.console import Group

        return Group(*rows)


def _save_history(run: Run) -> None:
    try:
        from ..store.history import History, load_settings

        History().add_run(run)
        s = load_settings()
        from ..config import Config

        cfg = Config()
        if s.get("share_results") and cfg.server_url:
            from ..render.share import share

            share(run, cfg.server_url)
    except Exception:
        pass


# ---- commands -----------------------------------------------------------------
@app.command("check")
def check(
    site: Optional[str] = typer.Argument(None, help="Also test this website, e.g. youtube.com"),
    details: bool = typer.Option(False, "--details", "-d", help="Full technical report"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    save: Optional[Path] = typer.Option(None, "--save", help="Save measurements to a file"),
    isp_report: bool = typer.Option(False, "--isp-report", help="Print an evidence summary for your ISP"),
    report_html: Optional[Path] = typer.Option(None, "--report-html", help="Write the ISP report as HTML (print to PDF)"),
    no_load: bool = typer.Option(False, "--no-load", help="Skip the load test (metered connections, during calls)"),
) -> None:
    """Run the standard 30-second checkup."""
    from ..orchestrator.checkup import run_checkup

    load = not no_load
    quiet = as_json or not console.is_terminal
    progress = Progress(load)
    if quiet:
        if not as_json:
            err.print("Running a checkup...", end=" ")
        run = asyncio.run(run_checkup(site, load, None))
    else:
        if load:
            console.print(Text("The load test briefly uses your full connection; others on your network may notice for ~12 s.", style="dim"))
        with Live(progress, console=console, refresh_per_second=8, transient=True):
            run = asyncio.run(run_checkup(site, load, progress.on_event))
    _save_history(run)
    if save:
        run.save(save)

    if as_json:
        print(run.to_json())
        return
    if not quiet:
        console.print(Text(f"Running a {run.duration_s:.0f}-second checkup... done.\n", style="dim"))
    else:
        err.print("done.")
    print_run(run, details=details, console=console)
    if isp_report:
        console.print()
        console.print(isp_report_text(run, **_plan()), highlight=False)
    if report_html:
        report_html.write_text(isp_report_html(run, **_plan()))
        console.print(Text(f"\nISP report written to {report_html} (open it and print to PDF).", style="dim"))
    if save:
        console.print(Text(f"\nMeasurements saved to {save}", style="dim"))
    if not details and not isp_report:
        console.print(Text("\nMore:  netdoctor --details     technical report\n"
                           "       netdoctor --isp-report  evidence you can send to your ISP\n"
                           "       netdoctor app           open the app", style="dim"))


def _plan() -> dict:
    try:
        from ..store.history import load_settings

        s = load_settings()
        return {"plan_down": s.get("plan_down_mbps"), "plan_up": s.get("plan_up_mbps")}
    except Exception:
        return {}


@app.command("replay")
def replay(
    path: Path = typer.Argument(..., help="A saved run.json"),
    details: bool = typer.Option(False, "--details", "-d"),
    as_json: bool = typer.Option(False, "--json"),
    isp_report: bool = typer.Option(False, "--isp-report"),
) -> None:
    """Re-run the diagnosis on saved measurements (no network access)."""
    run = Run.load(path)
    old = (run.verdict or {}).get("culprit")
    run.verdict = diagnose(run)
    if as_json:
        print(json.dumps(run.verdict, indent=2))
        return
    if old and old != run.verdict["culprit"]:
        console.print(Text(f"Note: this run was originally diagnosed as '{old}'; the current engine says "
                           f"'{run.verdict['culprit']}'.\n", style="yellow"))
    print_run(run, details=details, console=console)
    if isp_report:
        console.print()
        console.print(isp_report_text(run, **_plan()), highlight=False)


@app.command("watch")
def watch(
    interval: float = typer.Option(5, "--interval", "-i", help="Minutes between checks"),
    count: int = typer.Option(0, "--count", "-n", help="Stop after N checks (0 = run until Ctrl+C)"),
) -> None:
    """Run lightweight checks every few minutes and build a history."""
    from ..orchestrator.watch import watch_once
    from ..store.history import History

    h = History()
    console.print(f"Watch mode: a quick check every {interval:g} min. Press Ctrl+C to stop.\n")
    console.print(Text(f"{'time':<9}{'score':>6}  {'router':>7} {'internet':>9} {'jitter':>7} {'loss':>6} {'dns':>6}  issue", style="dim"))
    n = 0
    try:
        while True:
            s = asyncio.run(watch_once())
            h.add_watch(s)
            n += 1
            style = "green" if s["score"] >= 75 else "yellow" if s["score"] >= 50 else "red"
            f = lambda x, u="": f"{x:.0f}{u}" if x is not None else "-"  # noqa: E731
            console.print(Text(f"{s['ts'][11:19]:<9}") + Text(f"{s['score']:>6}", style=style)
                          + Text(f"  {f(s['router_ms']):>7} {f(s['internet_ms']):>9} {f(s['jitter_ms']):>7} "
                                 f"{(s['loss'] or 0) * 100:>5.0f}% {f(s['dns_ms']):>6}  {s['issue'] or ''}"))
            if count and n >= count:
                break
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        pass
    console.print(Text(f"\n{n} checks saved. See the pattern with: netdoctor history", style="dim"))


@app.command("history")
def history(limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """Show past checkups and the watch-mode heatmap."""
    from ..store.history import History

    h = History()
    runs = h.runs(limit)
    if runs:
        t = Table(title="Past checkups", title_justify="left", box=None)
        for c in ("#", "when", "finding", "confidence", "down", "up", "lag"):
            t.add_column(c)
        for r in runs:
            t.add_row(str(r["id"]), r["started_at"][:16].replace("T", " "), r["title"] or "", r["confidence"] or "",
                      f"{r['download']:.0f}" if r["download"] else "-", f"{r['upload']:.0f}" if r["upload"] else "-",
                      r["grade"] or "-")
        console.print(t)
    else:
        console.print("No checkups yet. Run: netdoctor")
    grid = h.heatmap()
    if any(c is not None for row in grid for c in row):
        console.print("\n[bold]Connection quality by hour[/bold] (watch mode, last 4 weeks)")
        console.print(Text("      " + "".join(f"{x:<3}" for x in range(0, 24, 3)).replace(" ", " "), style="dim"))
        for d, row in zip(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], grid):
            line = Text(f"{d}   ")
            for c in row:
                if c is None:
                    line += Text("·", style="dim")
                else:
                    line += Text("█", style="green" if c >= 75 else "yellow" if c >= 50 else "red")
            console.print(line)
        console.print(Text("      █ good  █ fair  █ poor  · no data", style="dim"))
        s = h.watch_summary()
        if s.get("worst_hours"):
            console.print(f"Worst hours: {', '.join(s['worst_hours'])}")


@app.command("app")
def open_app(
    port: int = typer.Option(8765, "--port"),
    browser: bool = typer.Option(False, "--browser", help="Open in your web browser instead of a window"),
) -> None:
    """Open the NetDoctor desktop app."""
    from ..app.server import launch

    launch(port=port, prefer_browser=browser)


@app.command("lab")
def lab() -> None:
    """Score the diagnosis engine against the fault-injection scenarios."""
    from ..lab_runner import main as lab_main

    raise typer.Exit(lab_main())


def run() -> None:
    """Entry point: `netdoctor [site] [options]` means `netdoctor check ...`."""
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"netdoctor {__version__}")
        return
    if not argv or argv[0] not in SUBCOMMANDS:
        sys.argv.insert(1, "check")
    app()


if __name__ == "__main__":
    run()
