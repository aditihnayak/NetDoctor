"""Desktop app backend: a tiny local HTTP server (stdlib only) + JSON API + live events.

The UI is plain HTML/JS in ./static (no build step). `netdoctor app` opens it in
a native window if pywebview is installed, otherwise in your browser.
Only listens on 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import socket
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .. import __version__
from ..config import Config
from ..engine import extract
from ..orchestrator.checkup import run_checkup
from ..orchestrator.watch import Watcher
from ..render.isp_report import isp_report_html, isp_report_text
from ..store.history import History, load_settings, save_settings

STATIC = Path(__file__).parent / "static"


class EventHub:
    """Keeps every event of the current checkup so late listeners can catch up."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.running = False
        self.gen = 0

    def reset(self) -> None:
        with self.cond:
            self.events = []
            self.gen += 1
            self.running = True
            self.cond.notify_all()

    def push(self, e: dict) -> None:
        with self.cond:
            self.events.append(e)
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.running = False
            self.cond.notify_all()


class AppState:
    def __init__(self) -> None:
        self.history = History()
        self.hub = EventHub()
        self.lock = threading.Lock()
        s = load_settings()
        self.watcher = Watcher(self.history, s.get("watch_interval_min") or 5)
        if s.get("watch_enabled"):
            self.watcher.start()

    def start_check(self, site: str | None, load: bool) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self.hub.reset()

        def work() -> None:
            try:
                run = asyncio.run(run_checkup(site or None, load, self._emit))
                run_id = self.history.add_run(run)
                s = load_settings()
                cfg = Config()
                if s.get("share_results") and cfg.server_url:
                    from ..render.share import share

                    share(run, cfg.server_url)
                self.hub.push({"type": "saved", "id": run_id})
            except Exception as e:
                traceback.print_exc()
                self.hub.push({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                self.hub.finish()
                self.lock.release()

        threading.Thread(target=work, daemon=True, name="netdoctor-check").start()
        return True

    def _emit(self, e: dict) -> None:
        if e.get("type") == "done":
            return  # we send 'saved' with the run id instead
        self.hub.push(e)

    def run_view(self, run_id: int) -> dict | None:
        run = self.history.get_run(run_id)
        if not run:
            return None
        F = extract(run)
        tl = run.first("timeline")
        return {
            "id": run_id,
            "started_at": run.started_at,
            "duration_s": run.duration_s,
            "options": run.options,
            "context": run.context,
            "verdict": run.verdict,
            "timeline": tl.meta if tl else None,
            "features": F,
        }


STATE: AppState | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"NetDoctor/{__version__}"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # ---- helpers -------------------------------------------------------
    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, ctype: str = "text/plain; charset=utf-8", status: int = 200, download: str | None = None) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def _same_origin(self) -> bool:
        """Block other websites from driving this API (CSRF / DNS rebinding)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return False
        return origin is None or urlparse(origin).hostname in ("127.0.0.1", "localhost")

    # ---- routes --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        st = STATE
        u = urlparse(self.path)
        p = u.path
        parts = [x for x in p.split("/") if x]
        try:
            if p in ("/", "/index.html"):
                return self._static("index.html")
            if parts[:1] == ["static"] and len(parts) == 2:
                return self._static(parts[1])
            if p == "/api/state":
                last = st.history.last_run()
                return self._json({
                    "version": __version__,
                    "settings": load_settings(),
                    "running": st.hub.running,
                    "last": {"id": last[0], "started_at": last[1].started_at, "title": last[1].verdict.get("title"),
                             "culprit": last[1].verdict.get("culprit")} if last and last[1].verdict else None,
                    "watch": {"running": st.watcher.running, **st.history.watch_summary(7)},
                })
            if p == "/api/events":
                return self._events()
            if p == "/api/history":
                return self._json({
                    "runs": st.history.runs(100),
                    "heatmap": st.history.heatmap(),
                    "watch": st.history.watch_samples(7),
                    "summary": st.history.watch_summary(14),
                })
            if parts[:2] == ["api", "run"] and len(parts) >= 3:
                run_id = int(parts[2])
                if len(parts) == 3:
                    v = st.run_view(run_id)
                    return self._json(v) if v else self._json({"error": "not found"}, 404)
                run = st.history.get_run(run_id)
                if not run:
                    return self._json({"error": "not found"}, 404)
                s = load_settings()
                kw = {"plan_down": s.get("plan_down_mbps"), "plan_up": s.get("plan_up_mbps"),
                      "watch_summary": st.history.watch_summary(14)}
                if parts[3] == "isp-report":
                    return self._json({"text": isp_report_text(run, **kw)})
                if parts[3] == "isp-report.html":
                    return self._text(isp_report_html(run, **kw), "text/html; charset=utf-8")
                if parts[3] == "json":
                    return self._text(run.to_json(), "application/json", download=f"netdoctor-run-{run_id}.json")
            return self._json({"error": "not found"}, 404)
        except (ValueError, IndexError):
            return self._json({"error": "bad request"}, 400)

    def do_POST(self) -> None:  # noqa: N802
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        st = STATE
        p = urlparse(self.path).path
        data = self._body()
        if p == "/api/check":
            settings = load_settings()
            load = bool(data.get("load", settings.get("load_test", True)))
            site = (data.get("site") or "").strip()[:200] or None
            ok = st.start_check(site, load)
            return self._json({"ok": ok}, 200 if ok else 409)
        if p == "/api/settings":
            s = save_settings(data)
            st.watcher.interval_min = float(s.get("watch_interval_min") or 5)
            if s.get("watch_enabled") and not st.watcher.running:
                st.watcher.start()
            elif not s.get("watch_enabled") and st.watcher.running:
                st.watcher.stop()
            return self._json(s)
        return self._json({"error": "not found"}, 404)

    def _static(self, name: str) -> None:
        f = (STATIC / name).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._json({"error": "not found"}, 404)
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(f.name)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events(self) -> None:
        """Server-sent events for the running checkup."""
        hub = STATE.hub
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        i = 0
        gen = hub.gen
        try:
            while True:
                with hub.cond:
                    while i >= len(hub.events) and hub.running and hub.gen == gen:
                        hub.cond.wait(timeout=10)
                        if i >= len(hub.events) and hub.running:
                            break  # send a keep-alive
                    if hub.gen != gen:
                        return
                    batch = hub.events[i:]
                    i = len(hub.events)
                    finished = not hub.running
                if not batch and not finished:
                    self.wfile.write(b": keep-alive\n\n")
                for e in batch:
                    self.wfile.write(f"data: {json.dumps(e, default=str)}\n\n".encode())
                self.wfile.flush()
                if finished and i >= len(hub.events):
                    self.wfile.write(b'data: {"type": "end"}\n\n')
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    return preferred


def serve(port: int = 8765) -> tuple[ThreadingHTTPServer, str]:
    global STATE
    STATE = AppState()
    port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="netdoctor-http").start()
    return httpd, f"http://127.0.0.1:{port}/"


def launch(port: int = 8765, prefer_browser: bool = False) -> None:
    httpd, url = serve(port)
    if not prefer_browser:
        try:
            import webview  # pywebview, optional

            webview.create_window("NetDoctor", url, width=860, height=940, min_size=(380, 600))
            webview.start()
            httpd.shutdown()
            return
        except ImportError:
            pass
        except Exception as e:  # no GUI backend available: fall back to the browser
            print(f"Couldn't open a native window ({e}); using your browser instead.")
    print(f"NetDoctor is running at {url}  (press Ctrl+C to quit)")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    launch(prefer_browser=True)

