"""Consumption features over the captures lake.

These modules turn raw captures into downstream-ready artifacts:

* :mod:`awareness.consume.llm_export` — LLM-ready dataset exports (JSONL /
  Parquet) with dedupe folding and bounded-memory streaming.
* :mod:`awareness.consume.digest` — weekly corpus digest (metrics, top
  domains/terms/headlines, growth) plus a markdown renderer.
* :mod:`awareness.consume.xbridge` — thin adapter exposing the X scraper
  session store to the HTTP layer.
* :mod:`awareness.consume.router` / :mod:`awareness.consume.xrouter` — FastAPI
  routers wiring the above into the API.
"""

from awareness.consume.digest import Digest, generate_digest, render_digest_markdown
from awareness.consume.llm_export import ExportResult, export_llm_dataset, sample_corpus

__all__ = [
    "Digest",
    "ExportResult",
    "export_llm_dataset",
    "generate_digest",
    "render_digest_markdown",
    "sample_corpus",
]
