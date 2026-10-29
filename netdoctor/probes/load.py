"""Controls the load-generator process and measures throughput."""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import queue
import time
from typing import Callable

from ..config import Config
from ..models.run import Measurement
from .loadworker import worker_main


class LoadGenerator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        ctx = mp.get_context("spawn")
        self.cmd_q = ctx.Queue()
        self.err_q = ctx.Queue()
        self.counter = ctx.Value("q", 0)
        self.stop = ctx.Event()
        self.proc = ctx.Process(
            target=worker_main, args=(self.cmd_q, self.counter, self.stop, self.err_q), daemon=True
        )

    def start(self) -> None:
        """Start the process early (it idles until asked) so spawn cost isn't on the clock."""
        self.proc.start()

    async def run(
        self,
        direction: str,
        min_s: float = 4.0,
        max_s: float = 6.0,
        on_tick: Callable[[float], None] | None = None,
    ) -> Measurement:
        url = self.cfg.download_url if direction == "download" else self.cfg.upload_url
        with self.counter.get_lock():
            self.counter.value = 0
        self.cmd_q.put((direction, url, self.cfg.load_streams))
        t0 = time.perf_counter()
        series: list[list[float]] = []
        last_b, last_t = 0, t0
        warm_b, warm_t = None, None
        while True:
            await asyncio.sleep(0.25)
            now = time.perf_counter()
            b = self.counter.value
            mbps = (b - last_b) * 8 / (now - last_t) / 1e6
            last_b, last_t = b, now
            el = now - t0
            series.append([round(el, 2), round(mbps, 1)])
            if on_tick:
                on_tick(mbps)
            if warm_b is None and el >= 1.0:
                warm_b, warm_t = b, now
            if el >= max_s:
                break
            # Early stop: throughput has settled, enough lag samples are in.
            if el >= min_s and len(series) >= 8:
                a = sum(x[1] for x in series[-4:]) / 4
                p = sum(x[1] for x in series[-8:-4]) / 4
                if p > 0 and abs(a - p) / p < 0.08:
                    break
        self.stop.set()
        end_t, end_b = time.perf_counter(), self.counter.value
        try:
            errors = await asyncio.to_thread(self.err_q.get, True, 3)
        except queue.Empty:
            errors = ["worker_timeout"]
        if warm_b is not None and end_t - warm_t > 0.5:
            mbps = (end_b - warm_b) * 8 / (end_t - warm_t) / 1e6
        else:
            mbps = end_b * 8 / max(end_t - t0, 0.001) / 1e6
        return Measurement(
            probe="load",
            target=url.split("?")[0],
            phase=direction,
            meta={
                "mbps": round(mbps, 1),
                "bytes": end_b,
                "seconds": round(end_t - t0, 2),
                "series": series,
                "errors": sorted(set(errors)),
                "failed": end_b < 100_000,
            },
        )

    def close(self) -> None:
        try:
            self.stop.set()
            self.cmd_q.put(("quit",))
            self.proc.join(timeout=2)
        except Exception:
            pass
        if self.proc.is_alive():
            self.proc.terminate()
