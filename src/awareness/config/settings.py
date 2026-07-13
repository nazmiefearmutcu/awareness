"""Application configuration.

All runtime knobs live here. Environment variables (prefix AW_) override
defaults; a YAML file at AW_CONFIG_FILE is optional and merges on top of
defaults but is overridden by env. We intentionally keep this small — the
adapters' source-specific knobs live in their own configs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    """Resolve project root: prefer AW_PROJECT_ROOT env, else infer from this file."""
    env_root = os.environ.get("AW_PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()
    # src/awareness/config/settings.py -> project root is 3 parents up from src.
    return Path(__file__).resolve().parents[3]


def _load_yaml_overrides() -> dict[str, Any]:
    path = os.environ.get("AW_CONFIG_FILE")
    if not path:
        # Default location: <root>/configs/awareness.yaml if present.
        candidate = _project_root() / "configs" / "awareness.yaml"
        if not candidate.exists():
            return {}
        path = str(candidate)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML config at {path} did not parse to a mapping")
    return data


class Settings(BaseSettings):
    """Runtime configuration for the awareness engine."""

    model_config = SettingsConfigDict(
        env_prefix="AW_",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # ── paths ────────────────────────────────────────────────────────────
    project_root: Path = Field(default_factory=_project_root)
    data_dir: Path | None = None
    iceberg_warehouse: Path | str | None = None
    iceberg_catalog_db: Path | None = None
    state_db_url: str | None = None
    log_dir: Path | None = None
    checkpoint_dir: Path | None = None
    cache_dir: Path | None = None
    warc_cache_dir: Path | None = None

    # ── identity ─────────────────────────────────────────────────────────
    ingest_version: str = "0.1.0"
    user_agent: str = (
        "AwarenessBot/0.1 (+https://github.com/nazmiefearmutcu/awareness; "
        "public-text-research)"
    )
    contact_email: str = "research@example.invalid"

    # ── politeness / fetch ────────────────────────────────────────────────
    request_timeout_sec: float = 30.0
    per_domain_concurrency: int = 2
    per_domain_delay_sec: float = 1.0
    robots_cache_ttl_sec: int = 3600
    global_fetch_concurrency: int = 32
    max_retries: int = 4
    backoff_base_sec: float = 1.5

    # ── runtime / scheduler ──────────────────────────────────────────────
    worker_concurrency: int = 8
    extract_concurrency: int = 4
    storage_flush_records: int = 500
    storage_flush_seconds: float = 15.0
    bounded_queue_size: int = 1024

    # ── state db reaper ──────────────────────────────────────────────────
    reaper_enabled: bool = True
    reaper_interval_seconds: int = 86400  # default 24 hours
    reaper_retention_days: int = 7        # keep completed/old tasks for 7 days
    redis_url: str | None = None

    # ── tail ─────────────────────────────────────────────────────────────
    tail_poll_seconds: float = 60.0
    tail_seed_file: Path | None = None  # YAML with feeds + sitemaps to watch
    tail_gdelt: bool = False  # also discover via GDELT (global news firehose)
    tail_gdelt_max_urls: int = 500  # cap URLs pulled per 15-min GDELT slot
    tail_show_captures: bool = True  # print each capture to the terminal as it lands
    terminal_mute_duplicates: bool = False  # skip printing EXACT_DUP / NEAR_DUP / REVISION and tight near-dup skip-store lines

    # ── corpus filters ───────────────────────────────────────────────────
    text_min_chars: int = 200
    # Lower floor for RSS/Atom/GDELT/sitemap-discovered pages (short news stubs).
    text_min_chars_news: int = 80
    text_max_chars: int = 1_500_000

    # ── Common Crawl WET ────────────────────────────────────────────────
    cc_wet_max_shards_per_crawl: int = 4  # cap WET shards planned per crawl discovery
    wet_quality_filter: bool = True  # drop Gopher/C4-low-quality WET records before storing

    # ── storage destinations (where captures are written) ────────────────
    # These three toggles are the TAIL/BODY write destinations. Set them with
    # ``awareness configure`` (a wizard) or ``awareness config set``.
    enable_iceberg: bool = True  # write to the Iceberg warehouse (local or cloud)
    enable_jsonl_staging: bool = True  # write local JSONL staging (+ index)
    enable_gdrive: bool = False  # upload finalized JSONL chunks to Google Drive
    gdrive_folder_name: str = "Awareness Captures"  # target Drive folder
    jsonl_compress: bool = False  # gzip staging files (.jsonl.gz)

    # ── search ───────────────────────────────────────────────────────────
    # Defaults for the ``search`` command. ``mode`` picks the matching
    # strategy (auto = FTS, then stem-prefix fallback); ``fields`` is the
    # comma-list of columns prefix/substring matching looks at; ``max_results``
    # is the hard overload ceiling on rows returned in one call.
    search_default_mode: str = "auto"
    search_default_fields: str = "title,text"
    search_default_limit: int = 10
    search_max_results: int = 200
    search_idf_threshold: float = 1.0

    # ── observability ────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True

    def model_post_init(self, __context: Any) -> None:
        """Resolve derived paths and create directories."""
        root = self.project_root
        if self.data_dir is None:
            self.data_dir = root / "data"
        if self.iceberg_warehouse is None:
            self.iceberg_warehouse = self.data_dir / "iceberg"
        if self.iceberg_catalog_db is None:
            self.iceberg_catalog_db = self.data_dir / "iceberg" / "catalog.sqlite"
        if self.state_db_url is None:
            self.state_db_url = f"sqlite+aiosqlite:///{self.data_dir / 'state' / 'awareness.sqlite'}"
        if self.log_dir is None:
            self.log_dir = self.data_dir / "logs"
        if self.checkpoint_dir is None:
            self.checkpoint_dir = self.data_dir / "checkpoints"
        if self.cache_dir is None:
            self.cache_dir = self.data_dir / "cache"
        if self.warc_cache_dir is None:
            self.warc_cache_dir = self.data_dir / "warc"
        if self.tail_seed_file is None:
            self.tail_seed_file = root / "configs" / "tail_seeds.yaml"

        for p in (
            self.data_dir,
            self.iceberg_warehouse,
            self.log_dir,
            self.checkpoint_dir,
            self.cache_dir,
            self.warc_cache_dir,
            self.iceberg_catalog_db.parent,
            self.data_dir / "state",
            self.data_dir / "jsonl",
            self.data_dir / "duckdb",
            self.data_dir / "dlq",
        ):
            if p is not None:
                p_str = str(p)
                if not p_str.startswith(("s3://", "s3a://", "gcs://", "gs://")):
                    Path(p).mkdir(parents=True, exist_ok=True)

    # ── helpers ──────────────────────────────────────────────────────────
    def staging_jsonl_dir(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "jsonl"

    def duckdb_path(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "duckdb" / "metadata.duckdb"

    def dlq_dir(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "dlq"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings; YAML overrides applied first, env wins."""
    overrides = _load_yaml_overrides()
    return Settings(**overrides)


def reset_settings() -> None:
    """Clear the cached singleton (used by tests)."""
    get_settings.cache_clear()
