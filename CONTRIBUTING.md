# Contributing to Awareness

Thanks for your interest! Awareness is a single-Python-process ingestion
engine; the architecture is intentionally small and contribution areas are
clearly bounded.

## Easiest contributions

- **Source adapters** — add a new adapter under `src/awareness/sources/`
  that produces `DocCapture` records. Examples: Wikipedia dump, arXiv,
  patent corpora, regional government RSS, academic preprint mirrors.
- **Dedup tuning** — open an issue with a real-world case where the
  exact+simhash dedup is missing or over-merging. Include capture IDs.
- **Iceberg / DuckDB read-path examples** — notebooks or SQL recipes for
  common analytics on the captured corpus.

## Code contributions

1. Fork the repo and create a branch from `master`.
2. `pip install -e .[dev]` to install dev dependencies.
3. Run `pytest tests/` before opening the PR (all tests must stay green).
4. Add a test for any behavior change in `tests/`. Adapter PRs need a
   fixture-based test that proves the new source emits canonical
   `DocCapture` records.
5. Politeness: any new HTTP-fetching adapter must respect `robots.txt`,
   use exponential backoff, and honour `If-Modified-Since` / `ETag`
   where the protocol allows.
6. Open the PR with a one-line summary and a sample run output if the
   change is observable from the CLI.

## What this project intentionally does NOT do

- Scrape login-gated content
- Bypass paywalls
- Store images, video, audio, or other binary media
- Operate outside of `robots.txt`

PRs that attempt any of these will be closed without merge.

## Code of conduct

Be respectful, be specific, be brief. Disagreements are fine; insults are not.
