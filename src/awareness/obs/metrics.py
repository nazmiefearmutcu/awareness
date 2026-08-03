"""Light-weight, thread-safe in-process metrics registry.

A real deployment swaps this for Prometheus. The interface is the same:
``inc()``, ``add()``, ``observe()``, ``snapshot()``.
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (0.0 if empty)."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = min(n - 1, max(0, int(round((pct / 100.0) * (n - 1)))))
    return sorted_vals[idx]


@dataclass
class _Histogram:
    count: int = 0
    sum: float = 0.0
    min: float = float("inf")
    max: float = 0.0
    samples: list[float] = field(default_factory=list)
    max_samples: int = 256

    def observe(self, v: float) -> None:
        self.count += 1
        self.sum += v
        self.min = min(self.min, v)
        self.max = max(self.max, v)
        if len(self.samples) < self.max_samples:
            self.samples.append(v)
        else:
            # Vitter Algorithm R: the count-th item replaces a uniformly chosen
            # reservoir slot with probability max_samples/count, keeping the
            # sample uniform over the whole stream.
            j = random.randint(0, self.count - 1)  # noqa: S311
            if j < self.max_samples:
                self.samples[j] = v

    def as_dict(self) -> dict[str, Any]:
        avg = self.sum / self.count if self.count else 0.0
        ordered = sorted(self.samples)
        return {
            "count": self.count,
            "sum": round(self.sum, 4),
            "min": round(self.min if self.count else 0.0, 4),
            "max": round(self.max, 4),
            "avg": round(avg, 4),
            "p50": round(_percentile(ordered, 50), 4),
            "p95": round(_percentile(ordered, 95), 4),
            "p99": round(_percentile(ordered, 99), 4),
        }


class MetricsRegistry:
    """Thread-safe counters and histograms keyed by name and label tuple."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hist: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = defaultdict(_Histogram)
        self._started_at = time.time()

    @staticmethod
    def _labels_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        if not labels:
            return ()
        return tuple(sorted(labels.items()))

    def inc(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._counters[(name, self._labels_key(labels))] += value

    def add(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self.inc(name, value, labels)

    def counter_sum(self, name: str) -> float:
        """Sum all label series for a counter name (0.0 if none)."""
        with self._lock:
            return float(sum(v for (n, _), v in self._counters.items() if n == name))

    def counter_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Return a single labelled counter series (0.0 if absent)."""
        key = (name, self._labels_key(labels))
        with self._lock:
            return float(self._counters.get(key, 0.0))

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[(name, self._labels_key(labels))] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._hist[(name, self._labels_key(labels))].observe(value)

    def snapshot(self, *, prefix: str | None = None) -> dict[str, Any]:
        """Return counters/gauges/histograms; optional *prefix* filters by name.

        *prefix* matches the start of the metric name (case-sensitive). Empty
        or ``None`` returns the full snapshot. Uptime is always included.
        """
        with self._lock:
            counters = [
                {"name": n, "labels": dict(lbl), "value": round(v, 4)}
                for (n, lbl), v in sorted(self._counters.items())
            ]
            # Derived robots cache hit ratio from layer counters (memory+db vs total).
            # Updated gauges keep /metrics dashboards simple without client math.
            self._refresh_robots_hit_ratio_unlocked()
            gauges = [
                {"name": n, "labels": dict(lbl), "value": v} for (n, lbl), v in sorted(self._gauges.items())
            ]
            histograms = [
                {"name": n, "labels": dict(lbl), **h.as_dict()} for (n, lbl), h in sorted(self._hist.items())
            ]
            snap = {
                "uptime_seconds": round(time.time() - self._started_at, 2),
                "counters": counters,
                "gauges": gauges,
                "histograms": histograms,
            }
            return self.filter_snapshot(snap, prefix=prefix)

    @staticmethod
    def filter_snapshot(snap: dict[str, Any], *, prefix: str | None = None) -> dict[str, Any]:
        """Filter a snapshot dict by metric-name prefix (pure helper)."""
        if not prefix:
            return snap
        p = str(prefix)
        out = dict(snap)
        for key in ("counters", "gauges", "histograms"):
            rows = out.get(key) or []
            out[key] = [r for r in rows if str(r.get("name") or "").startswith(p)]
        if p:
            out["prefix"] = p
        return out

    @staticmethod
    def _prom_metric_name(name: str) -> str:
        """Sanitize a metric name for Prometheus (``[a-zA-Z_:][a-zA-Z0-9_:]*``)."""
        out: list[str] = []
        for i, ch in enumerate(name):
            if ch.isalnum() or ch in "_:":
                out.append(ch)
            elif ch in ".-/ ":
                out.append("_")
            else:
                out.append("_")
        s = "".join(out).strip("_") or "metric"
        if s[0].isdigit():
            s = f"m_{s}"
        return s

    @staticmethod
    def _prom_label_value(value: str) -> str:
        """Escape a label value for Prometheus text exposition."""
        return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def _prom_labels(self, labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        parts = [f'{self._prom_metric_name(k)}="{self._prom_label_value(v)}"' for k, v in labels]
        return "{" + ",".join(parts) + "}"

    def render_prometheus(self, *, prefix: str | None = None) -> str:
        """Render metrics in Prometheus text exposition format 0.0.4.

        Counters → ``_total`` suffix when missing; gauges as-is; histograms
        emit ``_count``, ``_sum``, and approximate ``_p50`` / ``_p95`` / ``_p99``
        from the reservoir (not true quantile streams). Process uptime is
        exported as ``awareness_uptime_seconds``.

        *prefix* limits series to metric names starting with that string
        (same semantics as :meth:`snapshot`). Uptime is always exported.
        """
        pfx = str(prefix) if prefix else ""

        def _match(name: str) -> bool:
            return (not pfx) or name.startswith(pfx)

        with self._lock:
            self._refresh_robots_hit_ratio_unlocked()
            lines: list[str] = [
                "# HELP awareness_uptime_seconds Process uptime in seconds.",
                "# TYPE awareness_uptime_seconds gauge",
                f"awareness_uptime_seconds {round(time.time() - self._started_at, 2)}",
            ]
            # Group counter series by base name for one TYPE line each.
            counter_names = sorted({n for (n, _) in self._counters if _match(n)})
            for name in counter_names:
                prom = self._prom_metric_name(name)
                if not prom.endswith("_total"):
                    prom = f"{prom}_total"
                lines.append(f"# TYPE {prom} counter")
                for (n, lbl), v in sorted(self._counters.items()):
                    if n != name:
                        continue
                    lines.append(f"{prom}{self._prom_labels(lbl)} {float(v)}")
            gauge_names = sorted({n for (n, _) in self._gauges if _match(n)})
            for name in gauge_names:
                prom = self._prom_metric_name(name)
                lines.append(f"# TYPE {prom} gauge")
                for (n, lbl), v in sorted(self._gauges.items()):
                    if n != name:
                        continue
                    lines.append(f"{prom}{self._prom_labels(lbl)} {float(v)}")
            hist_names = sorted({n for (n, _) in self._hist if _match(n)})
            for name in hist_names:
                prom = self._prom_metric_name(name)
                lines.append(f"# TYPE {prom} summary")
                for (n, lbl), h in sorted(self._hist.items()):
                    if n != name:
                        continue
                    lab = self._prom_labels(lbl)
                    d = h.as_dict()
                    lines.append(f"{prom}_count{lab} {d['count']}")
                    lines.append(f"{prom}_sum{lab} {d['sum']}")
                    # Approximate quantiles from reservoir sample.
                    for q, key in ((0.5, "p50"), (0.95, "p95"), (0.99, "p99")):
                        if lab:
                            # Insert quantile into existing label set.
                            inner = lab[1:-1]
                            qlab = "{" + inner + f',quantile="{q}"' + "}"
                        else:
                            qlab = f'{{quantile="{q}"}}'
                        lines.append(f"{prom}{qlab} {d[key]}")
            lines.append("")  # trailing newline per exposition format
            return "\n".join(lines)

    def _refresh_robots_hit_ratio_unlocked(self) -> None:
        """Set ``robots.cache.hit_ratio`` gauges from layer counter series.

        Hit = memory + db (no network fetch). Ratio is 0.0 when no resolutions
        have been recorded yet. Caller must hold ``self._lock``.
        """
        mem = float(self._counters.get(("robots.cache", (("layer", "memory"),)), 0.0))
        db = float(self._counters.get(("robots.cache", (("layer", "db"),)), 0.0))
        net = float(self._counters.get(("robots.cache", (("layer", "network"),)), 0.0))
        total = mem + db + net
        if total <= 0:
            hit_ratio = 0.0
            memory_ratio = 0.0
            db_ratio = 0.0
            network_ratio = 0.0
        else:
            hit_ratio = (mem + db) / total
            memory_ratio = mem / total
            db_ratio = db / total
            network_ratio = net / total
        self._gauges[("robots.cache.hit_ratio", ())] = round(hit_ratio, 6)
        self._gauges[("robots.cache.memory_ratio", ())] = round(memory_ratio, 6)
        self._gauges[("robots.cache.db_ratio", ())] = round(db_ratio, 6)
        self._gauges[("robots.cache.network_ratio", ())] = round(network_ratio, 6)
        self._gauges[("robots.cache.resolutions", ())] = float(total)


_REGISTRY: MetricsRegistry | None = None
_REG_LOCK = threading.Lock()


def get_metrics() -> MetricsRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REG_LOCK:
            if _REGISTRY is None:
                _REGISTRY = MetricsRegistry()
    return _REGISTRY
