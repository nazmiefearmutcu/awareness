"""Awareness benchmark suite.

Same-machine, head-to-head micro-benchmarks comparing Awareness's own
algorithms (64-bit Charikar simhash near-dup, xxh3 content fingerprinting,
the normalize→dedup→write ingestion loop) against the de-facto peer
libraries in each space (datasketch MinHashLSH, BLAKE3, SHA-256, …), plus
the SOTA libraries Awareness rides on (trafilatura extraction, DuckDB FTS).

Everything is deterministic and self-contained: the corpus is generated
from a fixed seed, so anyone cloning the repo reproduces the same numbers
(modulo hardware). No private capture data is required.

Run:  python -m benchmarks.run_all
Plot: python -m benchmarks.plot
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
