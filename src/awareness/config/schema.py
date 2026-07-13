"""User-facing configuration schema — the single source of truth for the
``config`` and ``configure`` CLI surfaces.

The runtime model lives in :mod:`awareness.config.settings` (a pydantic
``BaseSettings``). That model knows *types and defaults*, but it carries no
human metadata: which section a knob belongs to, a one-line description, the
valid range, whether it is a TAIL **write destination**, or which environment
variable overrides it. This module adds exactly that, in a small declarative
registry, so every config-facing command renders the *same* information and
validates values the *same* way.

Everything here is pure: no I/O, no network, no database. That keeps the
registry trivially unit-testable and lets the CLI, the wizard, and the
validator all share one definition. A test pins ``CONFIG_SCHEMA`` against
``Settings.model_fields`` so the two never drift.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ── field kinds ──────────────────────────────────────────────────────────────
KIND_BOOL = "bool"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_STR = "str"
KIND_PATH = "path"
KIND_CHOICE = "choice"

_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}


@dataclass(frozen=True)
class ConfigField:
    """One user-tunable configuration knob.

    ``key`` must match a field on :class:`awareness.config.settings.Settings`.
    """

    key: str
    section: str
    kind: str
    default: Any
    description: str
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    is_destination: bool = False  # a TAIL write-destination toggle
    example: str | None = None

    @property
    def env_var(self) -> str:
        """The ``AW_``-prefixed environment variable that overrides this key."""
        return "AW_" + self.key.upper()

    @property
    def type_label(self) -> str:
        if self.kind == KIND_CHOICE and self.choices:
            return "choice(" + "|".join(self.choices) + ")"
        return self.kind

    def coerce(self, raw: Any) -> tuple[Any, str | None]:
        """Coerce + validate a raw value (typically a CLI string).

        Returns ``(value, None)`` on success or ``(None, error_message)`` on
        failure. ``raw`` may already be a typed value (bool/int/float) — the
        right thing happens either way.
        """
        return coerce_and_validate(self, raw)


# ── the registry — ordered by section, then by importance within section ─────
# NOTE: keep `default` mirrored with Settings; the test
# `test_config_schema.py::test_defaults_match_settings` enforces it.
CONFIG_SCHEMA: tuple[ConfigField, ...] = (
    # ── Storage routing — WHERE captures are written (the destinations) ──────
    ConfigField(
        "enable_jsonl_staging", "Storage routing", KIND_BOOL, True,
        "Write captures to local JSONL staging files (+ the SQLite/DuckDB index).",
        is_destination=True,
    ),
    ConfigField(
        "enable_iceberg", "Storage routing", KIND_BOOL, True,
        "Write captures to the Iceberg warehouse (local path or S3/cloud URI).",
        is_destination=True,
    ),
    ConfigField(
        "enable_gdrive", "Storage routing", KIND_BOOL, False,
        "Upload finalized JSONL chunks to Google Drive (needs `cloud auth-gdrive`).",
        is_destination=True,
    ),
    # ── Destination targets — the paths/URIs the destinations write to ───────
    ConfigField(
        "data_dir", "Destination targets", KIND_PATH, None,
        "Root directory for ALL local data (JSONL, index, state, logs, cache).",
        example="/Users/me/awareness-data",
    ),
    ConfigField(
        "iceberg_warehouse", "Destination targets", KIND_STR, None,
        "Iceberg warehouse location: a local folder or a cloud URI (s3://bucket/path).",
        example="s3://awareness/warehouse",
    ),
    ConfigField(
        "gdrive_folder_name", "Destination targets", KIND_STR, "Awareness Captures",
        "Name of the Google Drive folder uploaded chunks are placed in.",
        example="Awareness Captures",
    ),
    # ── Tail (live capture) ──────────────────────────────────────────────────
    ConfigField(
        "tail_poll_seconds", "Tail (live capture)", KIND_FLOAT, 60.0,
        "How often the tail re-arms its seed feeds/sitemaps, in seconds.",
        minimum=1.0, maximum=86_400.0,
    ),
    ConfigField(
        "tail_gdelt", "Tail (live capture)", KIND_BOOL, False,
        "Also follow the GDELT global-news firehose while tailing.",
    ),
    ConfigField(
        "tail_gdelt_max_urls", "Tail (live capture)", KIND_INT, 500,
        "Cap on URLs pulled from each 15-minute GDELT slot.",
        minimum=1, maximum=100_000,
    ),
    ConfigField(
        "tail_show_captures", "Tail (live capture)", KIND_BOOL, True,
        "Print each captured document to the terminal as it lands while tailing.",
    ),
    ConfigField(
        "terminal_mute_duplicates", "Tail (live capture)", KIND_BOOL, False,
        "Mute/skip printing duplicate captures (EXACT_DUP, NEAR_DUP, REVISION) and tight near-dup skip-store lines to the terminal.",
    ),
    ConfigField(
        "tail_seed_file", "Tail (live capture)", KIND_PATH, None,
        "Path to the YAML file of feeds/sitemaps the tail watches.",
        example="configs/tail_seeds.yaml",
    ),
    # ── Storage tuning ───────────────────────────────────────────────────────
    ConfigField(
        "jsonl_compress", "Storage tuning", KIND_BOOL, False,
        "Gzip JSONL staging files on disk (.jsonl.gz instead of .jsonl).",
    ),
    ConfigField(
        "storage_flush_records", "Storage tuning", KIND_INT, 500,
        "Flush the write buffer after this many records.",
        minimum=1, maximum=10_000_000,
    ),
    ConfigField(
        "storage_flush_seconds", "Storage tuning", KIND_FLOAT, 15.0,
        "Flush the write buffer at least this often, in seconds.",
        minimum=0.5, maximum=86_400.0,
    ),
    ConfigField(
        "bounded_queue_size", "Storage tuning", KIND_INT, 1024,
        "Maximum in-flight items in the bounded work queue.",
        minimum=1, maximum=10_000_000,
    ),
    ConfigField(
        "reaper_enabled", "Storage tuning", KIND_BOOL, True,
        "Enable the background state database reaper to clean up old tasks.",
    ),
    ConfigField(
        "reaper_interval_seconds", "Storage tuning", KIND_INT, 86400,
        "How often the state database reaper runs, in seconds.",
        minimum=1, maximum=10_000_000,
    ),
    ConfigField(
        "reaper_retention_days", "Storage tuning", KIND_INT, 7,
        "Number of days to retain completed/old tasks in the state database.",
        minimum=0, maximum=3650,
    ),
    # ── Corpus filters ───────────────────────────────────────────────────────
    ConfigField(
        "text_min_chars", "Corpus filters", KIND_INT, 200,
        "Drop documents whose extracted text is shorter than this.",
        minimum=0, maximum=100_000_000,
    ),
    ConfigField(
        "text_max_chars", "Corpus filters", KIND_INT, 1_500_000,
        "Drop documents whose extracted text is longer than this.",
        minimum=1, maximum=1_000_000_000,
    ),
    # ── Politeness / fetch ───────────────────────────────────────────────────
    ConfigField(
        "per_domain_concurrency", "Politeness / fetch", KIND_INT, 2,
        "Maximum concurrent requests per domain.",
        minimum=1, maximum=1000,
    ),
    ConfigField(
        "per_domain_delay_sec", "Politeness / fetch", KIND_FLOAT, 1.0,
        "Minimum delay between requests to the same domain, in seconds.",
        minimum=0.0, maximum=3600.0,
    ),
    ConfigField(
        "global_fetch_concurrency", "Politeness / fetch", KIND_INT, 32,
        "Maximum concurrent fetches across all domains.",
        minimum=1, maximum=100_000,
    ),
    ConfigField(
        "request_timeout_sec", "Politeness / fetch", KIND_FLOAT, 30.0,
        "Per-request HTTP timeout, in seconds.",
        minimum=1.0, maximum=3600.0,
    ),
    ConfigField(
        "max_retries", "Politeness / fetch", KIND_INT, 4,
        "Retry a failed fetch up to this many times.",
        minimum=0, maximum=100,
    ),
    ConfigField(
        "robots_cache_ttl_sec", "Politeness / fetch", KIND_INT, 3600,
        "How long a robots.txt result is cached, in seconds.",
        minimum=0, maximum=2_592_000,
    ),
    ConfigField(
        "backoff_base_sec", "Politeness / fetch", KIND_FLOAT, 1.5,
        "Base of the exponential backoff between retries, in seconds.",
        minimum=0.0, maximum=3600.0,
    ),
    # ── Runtime / scheduler ──────────────────────────────────────────────────
    ConfigField(
        "worker_concurrency", "Runtime / scheduler", KIND_INT, 8,
        "Number of worker tasks draining the queue.",
        minimum=1, maximum=4096,
    ),
    ConfigField(
        "extract_concurrency", "Runtime / scheduler", KIND_INT, 4,
        "Number of concurrent text-extraction workers.",
        minimum=1, maximum=4096,
    ),
    ConfigField(
        "redis_url", "Runtime / scheduler", KIND_STR, None,
        "Redis URL for distributed locking and horizontal scaling coordination (e.g. redis:// or redlock://).",
        example="redis://localhost:6379/0",
    ),
    # ── Identity ─────────────────────────────────────────────────────────────
    ConfigField(
        "user_agent", "Identity", KIND_STR,
        "AwarenessBot/0.1 (+https://github.com/nazmiefearmutcu/awareness; public-text-research)",
        "User-Agent header sent with every fetch.",
    ),
    ConfigField(
        "contact_email", "Identity", KIND_STR, "research@example.invalid",
        "Contact email advertised to site operators.",
    ),
    ConfigField(
        "ingest_version", "Identity", KIND_STR, "0.1.0",
        "Version string stamped onto every capture record.",
    ),
    # ── Search ───────────────────────────────────────────────────────────────
    ConfigField(
        "search_default_mode", "Search", KIND_CHOICE, "auto",
        "Matching strategy: auto (FTS then stem-prefix fallback), fts (BM25 only), "
        "prefix (stem-root substring), or substring (raw ILIKE).",
        choices=("auto", "fts", "prefix", "substring"),
    ),
    ConfigField(
        "search_default_fields", "Search", KIND_STR, "title,text",
        "Comma-list of columns prefix/substring matching looks at "
        "(any of: title, text, domain, url).",
        example="title,text",
    ),
    ConfigField(
        "search_default_limit", "Search", KIND_INT, 10,
        "Default number of results shown per page.",
        minimum=1, maximum=1000,
    ),
    ConfigField(
        "search_max_results", "Search", KIND_INT, 200,
        "Hard ceiling on rows returned in a single search (overload guard).",
        minimum=1, maximum=100000,
    ),
    ConfigField(
        "search_idf_threshold", "Search", KIND_FLOAT, 1.0,
        "Minimum Inverse Document Frequency (IDF) threshold for query terms in BM25F ranking.",
        minimum=0.0, maximum=100.0,
    ),
    # ── Observability / terminal ─────────────────────────────────────────────
    ConfigField(
        "log_level", "Observability / terminal", KIND_CHOICE, "INFO",
        "Logging verbosity.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    ConfigField(
        "log_json", "Observability / terminal", KIND_BOOL, True,
        "Emit structured JSON logs (off = human-readable console logs).",
    ),
)

# Section display order (any section not listed sorts last, alphabetically).
SECTION_ORDER: tuple[str, ...] = (
    "Storage routing",
    "Destination targets",
    "Tail (live capture)",
    "Storage tuning",
    "Corpus filters",
    "Politeness / fetch",
    "Runtime / scheduler",
    "Search",
    "Identity",
    "Observability / terminal",
)

_BY_KEY: dict[str, ConfigField] = {f.key: f for f in CONFIG_SCHEMA}


# ── lookups ──────────────────────────────────────────────────────────────────
def get_field(key: str) -> ConfigField | None:
    """Return the field for ``key`` (accepts dashes or underscores)."""
    return _BY_KEY.get(normalize_key(key))


def normalize_key(key: str) -> str:
    """CLI-friendly: ``data-dir`` and ``data_dir`` are the same key."""
    return key.strip().replace("-", "_")


def all_keys() -> list[str]:
    return [f.key for f in CONFIG_SCHEMA]


def destination_fields() -> list[ConfigField]:
    return [f for f in CONFIG_SCHEMA if f.is_destination]


def fields_by_section() -> dict[str, list[ConfigField]]:
    """Group the schema by section, honouring :data:`SECTION_ORDER`."""
    grouped: dict[str, list[ConfigField]] = {}
    for f in CONFIG_SCHEMA:
        grouped.setdefault(f.section, []).append(f)
    ordered: dict[str, list[ConfigField]] = {}
    for section in SECTION_ORDER:
        if section in grouped:
            ordered[section] = grouped[section]
    for section in sorted(grouped):  # any stragglers not in SECTION_ORDER
        if section not in ordered:
            ordered[section] = grouped[section]
    return ordered


def suggest_keys(key: str, n: int = 3) -> list[str]:
    """Closest known keys to a (probably mistyped) ``key``."""
    return difflib.get_close_matches(normalize_key(key), all_keys(), n=n, cutoff=0.5)


# ── validation / coercion ────────────────────────────────────────────────────
def coerce_and_validate(fld: ConfigField, raw: Any) -> tuple[Any, str | None]:
    """Coerce ``raw`` to ``fld``'s type and range-check it.

    Returns ``(value, None)`` on success, ``(None, message)`` on failure.
    """
    if fld.kind == KIND_BOOL:
        return _coerce_bool(raw)
    if fld.kind == KIND_INT:
        return _coerce_number(fld, raw, integer=True)
    if fld.kind == KIND_FLOAT:
        return _coerce_number(fld, raw, integer=False)
    if fld.kind == KIND_CHOICE:
        return _coerce_choice(fld, raw)
    # str / path
    text = str(raw).strip()
    if text == "":
        return None, f"{fld.key} must not be empty"
    return text, None


def _coerce_bool(raw: Any) -> tuple[Any, str | None]:
    if isinstance(raw, bool):
        return raw, None
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True, None
    if text in _FALSE:
        return False, None
    return None, f"expected a boolean (true/false), got {raw!r}"


def _coerce_number(fld: ConfigField, raw: Any, *, integer: bool) -> tuple[Any, str | None]:
    try:
        value: Any = int(str(raw).strip()) if integer else float(str(raw).strip())
    except (TypeError, ValueError):
        want = "an integer" if integer else "a number"
        return None, f"expected {want}, got {raw!r}"
    if fld.minimum is not None and value < fld.minimum:
        return None, f"{fld.key} must be >= {_fmt_num(fld.minimum, integer)} (got {value})"
    if fld.maximum is not None and value > fld.maximum:
        return None, f"{fld.key} must be <= {_fmt_num(fld.maximum, integer)} (got {value})"
    return value, None


def _coerce_choice(fld: ConfigField, raw: Any) -> tuple[Any, str | None]:
    text = str(raw).strip()
    choices = fld.choices or ()
    for choice in choices:
        if text.lower() == choice.lower():
            return choice, None  # normalise to the canonical spelling
    return None, f"{fld.key} must be one of: {', '.join(choices)} (got {raw!r})"


def _fmt_num(value: float, integer: bool) -> str:
    return str(int(value)) if integer else str(value)


# ── effective-source resolution ──────────────────────────────────────────────
SOURCE_ENV = "env"
SOURCE_YAML = "yaml"
SOURCE_DEFAULT = "default"


def value_source(key: str, yaml_data: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """Where the effective value of ``key`` comes from.

    Precedence mirrors :mod:`awareness.config.settings`: environment variables
    win, then the YAML override file, otherwise the built-in default.
    """
    key = normalize_key(key)
    env_var = ("AW_" + key).upper()
    if any(k.upper() == env_var for k in env):
        return SOURCE_ENV
    if key in yaml_data:
        return SOURCE_YAML
    return SOURCE_DEFAULT


# ── destination plan (pure) ──────────────────────────────────────────────────
@dataclass(frozen=True)
class Destination:
    """One row in the rendered "where TAIL writes" plan."""

    key: str
    label: str
    enabled: bool
    detail: str
    warning: str | None = None


@dataclass(frozen=True)
class DestinationPlan:
    destinations: list[Destination] = field(default_factory=list)
    terminal_only: bool = False

    @property
    def warnings(self) -> list[str]:
        return [d.warning for d in self.destinations if d.enabled and d.warning]


def _is_cloud_uri(value: str | None) -> bool:
    return bool(value) and str(value).startswith(("s3://", "s3a://", "gs://", "gcs://"))


def describe_destinations(
    *,
    local: bool,
    s3: bool,
    gdrive: bool,
    data_dir: str | None,
    warehouse: str | None,
    gdrive_folder: str,
    gdrive_authorized: bool,
) -> DestinationPlan:
    """Build the human-readable routing plan from resolved destination state.

    Pure on purpose: callers pass already-resolved values (the CLI reads them
    off ``Settings`` + the gdrive auth file) so this stays unit-testable with no
    I/O. The wizard summary, ``configure --show`` and ``config doctor`` all
    render from this one description.
    """
    rows: list[Destination] = []

    local_detail = f"{data_dir}/jsonl  (+ SQLite/DuckDB index)" if data_dir else "local JSONL + index"
    rows.append(Destination("local", "Local JSONL + index", local, local_detail))

    if warehouse:
        s3_detail = str(warehouse) + ("  (cloud)" if _is_cloud_uri(warehouse) else "  (local path)")
    else:
        s3_detail = "Iceberg warehouse (unset → defaults under data dir)"
    s3_warn = None
    if s3 and not _is_cloud_uri(warehouse):
        s3_warn = "warehouse is a local path, not a cloud URI — set one with `configure --warehouse s3://…`"
    rows.append(Destination("s3", "Cloud — S3 / Iceberg", s3, s3_detail, s3_warn))

    gd_warn = None
    if gdrive and not gdrive_authorized:
        gd_warn = "Google Drive is not authorized — run `awareness cloud auth-gdrive`"
    rows.append(
        Destination("gdrive", "Google Drive", gdrive, f"folder “{gdrive_folder}”", gd_warn)
    )

    return DestinationPlan(destinations=rows, terminal_only=not (local or s3 or gdrive))
