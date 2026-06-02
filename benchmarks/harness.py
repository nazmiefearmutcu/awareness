"""Shared timing + result plumbing for the benchmark suite.

Timing methodology (kept deliberately simple and honest):

* every measured callable is **warmed up** once (JIT-free Python still
  benefits: imports, regex compilation, first-touch allocation),
* then run for ``repeats`` rounds; we report the **median** round to damp
  scheduler noise (min would flatter us, mean would be skewed by GC),
* throughput is ``work / median_seconds`` where work is documents or bytes.

Results serialize to a single ``results.json`` consumed by ``plot.py`` and
by the README table generator.
"""

from __future__ import annotations

import json
import platform
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RESULTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "benchmarks" / "results.json"


@dataclass(slots=True)
class Entry:
    """One competitor's score within a suite."""

    name: str
    value: float
    unit: str
    is_awareness: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Suite:
    key: str
    title: str
    metric: str            # human label, e.g. "Throughput (MB/s)"
    higher_is_better: bool
    entries: list[Entry] = field(default_factory=list)
    subtitle: str = ""

    def add(self, entry: Entry) -> None:
        self.entries.append(entry)


@dataclass(slots=True)
class Sweep:
    """A line-chart suite: several series measured across a shared x-axis."""

    key: str
    title: str
    x_label: str
    y_label: str
    x_values: list[float]
    series: list[dict] = field(default_factory=list)  # {name, values, is_awareness, note}
    higher_is_better: bool = True
    subtitle: str = ""

    def add_series(self, name: str, values: list[float], *, is_awareness: bool = False, note: str = "") -> None:
        self.series.append({"name": name, "values": values, "is_awareness": is_awareness, "note": note})


def time_callable(fn: Callable[[], Any], *, repeats: int = 5, warmup: int = 1) -> float:
    """Return the median wall-clock seconds over ``repeats`` runs."""
    for _ in range(max(0, warmup)):
        fn()
    samples: list[float] = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def throughput(work_units: float, seconds: float) -> float:
    return work_units / seconds if seconds > 0 else float("inf")


def machine_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "machine": platform.machine(),
    }
    # Best-effort logical CPU count.
    try:
        import os

        info["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    return info


def write_results(
    suites: list[Suite],
    *,
    sweeps: list[Sweep] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    try:
        from awareness import __version__ as aw_version
    except Exception:
        aw_version = "unknown"
    payload = {
        "meta": {
            "awareness_version": aw_version,
            "machine": machine_info(),
            **(extra_meta or {}),
        },
        "suites": [
            {
                "key": s.key,
                "title": s.title,
                "metric": s.metric,
                "higher_is_better": s.higher_is_better,
                "subtitle": s.subtitle,
                "entries": [asdict(e) for e in s.entries],
            }
            for s in suites
        ],
        "sweeps": [asdict(s) for s in (sweeps or [])],
    }
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(payload, indent=2))
    return RESULTS_PATH


def load_results() -> dict[str, Any]:
    return json.loads(RESULTS_PATH.read_text())


def fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"
