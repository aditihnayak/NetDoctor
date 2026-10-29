"""Measurement file schema.

A Run holds everything one checkup measured. It is plain data: it can be saved,
loaded and replayed through the engine without touching the network.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class Measurement:
    probe: str
    target: str | None = None
    phase: str | None = None
    samples_ms: list[float | None] | None = None
    loss: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and v != {}}

    @classmethod
    def from_dict(cls, d: dict) -> "Measurement":
        return cls(
            probe=d["probe"],
            target=d.get("target"),
            phase=d.get("phase"),
            samples_ms=d.get("samples_ms"),
            loss=d.get("loss"),
            meta=d.get("meta") or {},
        )


@dataclass
class Run:
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    version: int = SCHEMA_VERSION
    tool_version: str = ""
    duration_s: float = 0.0
    options: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    measurements: list[Measurement] = field(default_factory=list)
    verdict: dict[str, Any] | None = None

    # ---- queries -------------------------------------------------------
    def find(self, probe: str, target: str | None = None, phase: str | None = None) -> list[Measurement]:
        return [
            m
            for m in self.measurements
            if m.probe == probe
            and (target is None or m.target == target)
            and (phase is None or m.phase == phase)
        ]

    def first(self, probe: str, target: str | None = None, phase: str | None = None) -> Measurement | None:
        found = self.find(probe, target, phase)
        return found[0] if found else None

    def add(self, m: Measurement | None) -> None:
        if m is not None:
            self.measurements.append(m)

    # ---- (de)serialisation ---------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "tool_version": self.tool_version,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "options": self.options,
            "context": self.context,
            "measurements": [m.to_dict() for m in self.measurements],
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        return cls(
            version=d.get("version", SCHEMA_VERSION),
            tool_version=d.get("tool_version", ""),
            started_at=d.get("started_at", ""),
            duration_s=d.get("duration_s", 0.0),
            options=d.get("options") or {},
            context=d.get("context") or {},
            measurements=[Measurement.from_dict(m) for m in d.get("measurements", [])],
            verdict=d.get("verdict"),
        )

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def save(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        p.write_text(self.to_json())
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Run":
        return cls.from_dict(json.loads(Path(path).expanduser().read_text()))
