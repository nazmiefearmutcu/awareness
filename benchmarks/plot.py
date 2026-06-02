"""Render the benchmark results into aesthetic charts for the README.

Reads ``docs/benchmarks/results.json`` (written by ``run_all``) and emits a
curated set of dark, turquoise-accented figures matching Awareness's terminal
theme. Awareness bars are highlighted; peers are muted slate.

    python -m benchmarks.plot

Outputs PNG (retina DPI) into ``docs/benchmarks/``.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

from .harness import fmt, load_results

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "benchmarks"

# ── palette (Awareness terminal theme) ───────────────────────────────────────
BG = "#0b0f14"
PANEL = "#10161d"
INK = "#e8f0f2"
MUTED = "#8aa0ad"
GRID = "#1c2730"
AWARE = "#2ee6d6"          # turquoise — Awareness
AWARE_DK = "#15b9ab"
PEERS = ["#5b7283", "#6c8aa0", "#46586a", "#7d97a8", "#3f5260", "#8aa6b6"]
GOOD = "#5bd6a0"
WARN = "#e8b84b"

plt.rcParams.update(
    {
        "figure.facecolor": BG,
        "savefig.facecolor": BG,
        "axes.facecolor": PANEL,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "text.color": INK,
        "xtick.color": MUTED,
        "ytick.color": INK,
        "grid.color": GRID,
        "font.size": 11,
        "axes.titlesize": 13,
        "figure.dpi": 150,
    }
)
# Prefer a clean sans; fall back gracefully.
for cand in ("DejaVu Sans", "Helvetica Neue", "Arial"):
    if any(cand == f.name for f in fm.fontManager.ttflist):
        plt.rcParams["font.family"] = cand
        break

MONO = "DejaVu Sans Mono"


def _suite(results: dict, key: str) -> dict | None:
    for s in results["suites"]:
        if s["key"] == key:
            return s
    return None


def _sweep(results: dict, key: str) -> dict | None:
    for s in results.get("sweeps", []):
        if s["key"] == key:
            return s
    return None


def _style_ax(ax) -> None:
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)


def hbars(ax, entries: list[dict], *, higher_is_better: bool, log: bool = False, unit: str = "") -> None:
    """Horizontal bars, sorted best→worst (best on top), Awareness highlighted."""
    rev = higher_is_better
    rows = sorted(entries, key=lambda e: e["value"], reverse=not rev)  # worst→best so best ends on top
    names = [e["name"] for e in rows]
    vals = [e["value"] for e in rows]
    y = range(len(rows))
    peer_i = 0
    colors = []
    for e in rows:
        if e["is_awareness"]:
            colors.append(AWARE)
        else:
            colors.append(PEERS[peer_i % len(PEERS)])
            peer_i += 1
    bars = ax.barh(list(y), vals, color=colors, height=0.66, edgecolor=BG, linewidth=0.8, zorder=3)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=10)
    if log:
        ax.set_xscale("log")
    ax.grid(axis="x", linewidth=0.6, alpha=0.5)
    _style_ax(ax)
    xmax = max(vals) if vals else 1
    for b, e in zip(bars, rows, strict=True):
        v = e["value"]
        label = fmt(v) + (f" {unit}" if unit else "")
        weight = "bold" if e["is_awareness"] else "normal"
        col = AWARE if e["is_awareness"] else INK
        if log:
            ax.text(v * 1.05, b.get_y() + b.get_height() / 2, label, va="center", ha="left",
                    fontsize=9.5, color=col, fontweight=weight)
        else:
            inside = v > xmax * 0.55
            ax.text(v - xmax * 0.012 if inside else v + xmax * 0.012,
                    b.get_y() + b.get_height() / 2, label, va="center",
                    ha="right" if inside else "left", fontsize=9.5,
                    color=(BG if inside and e["is_awareness"] else col), fontweight=weight)
    if not log:
        ax.set_xlim(0, xmax * 1.18)
    ax.tick_params(length=0)


def _title(ax, title: str, subtitle: str, direction: str) -> None:
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=16)
    ax.annotate(subtitle + f"   ·   {direction}", xy=(0, 1.012), xycoords="axes fraction",
                fontsize=8.6, color=MUTED, ha="left", va="bottom")


def _dir(higher: bool) -> str:
    return "▲ higher is better" if higher else "▼ lower is better"


def fig_hashing(results: dict) -> None:
    s = _suite(results, "hash_throughput")
    if not s:
        return
    fig, ax = plt.subplots(figsize=(9.2, 4.4))
    hbars(ax, s["entries"], higher_is_better=True, unit="MB/s")
    _title(ax, "Content-fingerprint hashing throughput", s["subtitle"], _dir(True))
    fig.tight_layout()
    _save(fig, "hashing.png")


def fig_dedup(results: dict) -> None:
    tp = _suite(results, "dedup_throughput")
    mem = _suite(results, "dedup_memory")
    sw = _sweep(results, "dedup_accuracy_sweep")
    fig = plt.figure(figsize=(15.5, 4.7))
    gs = fig.add_gridspec(1, 3, wspace=0.42, width_ratios=[1, 0.85, 1.15])

    ax1 = fig.add_subplot(gs[0])
    hbars(ax1, tp["entries"], higher_is_better=True, unit="docs/s")
    _title(ax1, "Near-dup throughput", "detection fingerprint vs peer", _dir(True))

    ax2 = fig.add_subplot(gs[1])
    hbars(ax2, mem["entries"], higher_is_better=False, unit="B", log=True)
    _title(ax2, "Signature footprint", "bytes per document (log)", _dir(False))

    ax3 = fig.add_subplot(gs[2])
    _line(ax3, sw)
    _title(ax3, "Accuracy vs edit intensity", "end-to-end F1 — Awareness at default, MinHashLSH at best", _dir(True))

    fig.suptitle("Near-duplicate detection  ·  128-bit weighted SimHash vs MinHash",
                 x=0.012, y=0.995, ha="left", fontsize=15, fontweight="bold", color=INK)
    fig.subplots_adjust(top=0.80, bottom=0.12, left=0.11, right=0.985, wspace=0.42)
    _save(fig, "dedup.png")


def _line(ax, sw: dict) -> None:
    x = sw["x_values"]
    styles = [(AWARE, "o", 2.6, 7), (WARN, "s", 1.8, 5), (MUTED, "^", 1.8, 5)]
    for ser, (col, mk, lw, ms) in zip(sw["series"], styles, strict=False):
        ax.plot(x, ser["values"], color=col, marker=mk, linewidth=lw, markersize=ms,
                label=ser["name"].replace(" (Awareness)", " ★").split(" (datasketch")[0],
                zorder=4 if ser["is_awareness"] else 3, markeredgecolor=BG, markeredgewidth=0.7)
    ax.set_xlabel(sw["x_label"] + " (% of words edited)", fontsize=9.5, color=MUTED)
    ax.set_ylabel(sw["y_label"], fontsize=9.5, color=MUTED)
    ax.set_ylim(0, 1.04)
    ax.grid(True, linewidth=0.6, alpha=0.5)
    _style_ax(ax)
    ax.tick_params(length=0)
    ax.legend(loc="lower left", fontsize=8.4, frameon=True, facecolor=PANEL, edgecolor=GRID, labelcolor=INK)


def fig_extraction(results: dict) -> None:
    meas = _suite(results, "extraction_quality")
    pub = _suite(results, "extraction_published")
    fig, axes = plt.subplots(1, 2, figsize=(15.0, 4.6))
    hbars(axes[0], meas["entries"], higher_is_better=True, unit="")
    _title(axes[0], "Extraction quality — measured", meas["subtitle"], _dir(True))
    hbars(axes[1], pub["entries"], higher_is_better=True, unit="")
    _title(axes[1], "Extraction quality — published", pub["subtitle"], _dir(True))
    fig.suptitle("HTML → main-text extraction  ·  Awareness rides the F1 leader (trafilatura)",
                 x=0.012, ha="left", fontsize=15, fontweight="bold", color=INK)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    _save(fig, "extraction.png")


def fig_speedups(results: dict) -> None:
    """Self-improvement before/after: the changes made to beat the baselines."""
    pairs = []
    ing = _suite(results, "ingestion_loop")
    fp = _suite(results, "ingestion_fingerprint")
    opt = _suite(results, "query_search_optimization")
    if fp:
        a = next(e for e in fp["entries"] if e["is_awareness"])["value"]
        b = next(e for e in fp["entries"] if not e["is_awareness"])["value"]
        pairs.append(("Fingerprint stage\n(simhash vectorized)", b, a, "docs/s", True))
    if ing:
        a = next(e for e in ing["entries"] if e["is_awareness"])["value"]
        b = next(e for e in ing["entries"] if not e["is_awareness"])["value"]
        pairs.append(("End-to-end ingestion\nloop", b, a, "docs/s", True))
    if opt:
        a = next(e for e in opt["entries"] if e["is_awareness"])["value"]
        b = next(e for e in opt["entries"] if not e["is_awareness"])["value"]
        pairs.append(("BM25 search latency\n(view cache)", b, a, "ms", False))

    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    import numpy as np

    n = len(pairs)
    x = np.arange(n)
    w = 0.36
    befores = [p[1] for p in pairs]
    afters = [p[2] for p in pairs]
    ax.bar(x - w / 2, befores, w, label="before", color=PEERS[0], edgecolor=BG, zorder=3)
    ax.bar(x + w / 2, afters, w, label="after (Awareness)", color=AWARE, edgecolor=BG, zorder=3)
    for i, (name, b, a, unit, higher) in enumerate(pairs):
        factor = (a / b) if higher else (b / a)
        ax.text(i, max(b, a) * 1.04, f"{factor:.1f}×", ha="center", fontsize=12,
                color=GOOD, fontweight="bold")
        ax.text(i - w / 2, b, " " + fmt(b), ha="center", va="bottom", fontsize=8, color=MUTED, rotation=0)
        ax.text(i + w / 2, a, " " + fmt(a), ha="center", va="bottom", fontsize=8, color=AWARE, rotation=0)
    ax.set_xticks(x)
    ax.set_xticklabels([p[0] for p in pairs], fontsize=9.5)
    ax.set_yscale("log")
    ax.set_ylabel("throughput (docs/s) or latency (ms), log", fontsize=9.5, color=MUTED)
    ax.grid(axis="y", linewidth=0.6, alpha=0.5)
    _style_ax(ax)
    ax.tick_params(length=0)
    ax.legend(loc="upper right", fontsize=9, frameon=True, facecolor=PANEL, edgecolor=GRID, labelcolor=INK)
    _title(ax, "Optimizations shipped for this benchmark", "before → after, on the same machine and corpus",
           "factor = speedup")
    fig.tight_layout()
    _save(fig, "speedups.png")


def fig_summary(results: dict) -> None:
    """Hero 2×2 of the cleanest head-to-head wins."""
    fig, axes = plt.subplots(2, 2, figsize=(14.5, 9.0))
    specs = [
        ("hash_throughput", "Hashing throughput (MB/s)", True, False, "MB/s"),
        ("dedup_throughput", "Near-dup throughput (docs/s)", True, False, "docs/s"),
        ("dedup_memory", "Near-dup memory (B/doc)", False, True, "B"),
        ("extraction_quality", "Extraction quality (F1)", True, False, ""),
    ]
    for ax, (key, title, higher, log, unit) in zip(axes.flat, specs, strict=True):
        s = _suite(results, key)
        entries = s["entries"]
        if key == "dedup_throughput":
            # Clean head-to-head for the hero: detection fingerprint vs the peer.
            entries = [e for e in entries if "128-bit" in e["name"] or "MinHash" in e["name"]]
        hbars(ax, entries, higher_is_better=higher, log=log, unit=unit)
        _title(ax, title, s["subtitle"][:62], _dir(higher))
    fig.suptitle("Awareness — head-to-head vs the de-facto peers",
                 x=0.012, ha="left", fontsize=17, fontweight="bold", color=INK)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    _save(fig, "summary.png")


def _save(fig, name: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {path.relative_to(OUT_DIR.parent.parent)}")


def main() -> int:
    results = load_results()
    print("rendering charts →", OUT_DIR)
    fig_summary(results)
    fig_dedup(results)
    fig_hashing(results)
    fig_extraction(results)
    fig_speedups(results)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
