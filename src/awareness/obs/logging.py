"""Structured logging setup using structlog over stdlib logging."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import structlog

# Snapshot of the last applied config: (level, json, resolved_log_dir).
# None = never configured. Reconfiguration is a no-op only when the requested
# args are identical (H-30) — a settings-driven reconfigure must NOT be
# silently swallowed.
_CONFIGURED: tuple[str, bool, str | None] | None = None

# M-01 red-team: strip ``user:pass@`` userinfo from URL-ish keys before any
# renderer touches them, so credentials never land in logs or metrics.
_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_USERINFO_KEYS = ("url", "err", "error", "exception", "msg", "event", "detail")


def _redact_userinfo(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Processor: replace ``user:pass@`` in URL-like event keys with ``***@``."""
    for key in _USERINFO_KEYS:
        val = event_dict.get(key)
        if isinstance(val, str) and "@" in val:
            event_dict[key] = _USERINFO_RE.sub(r"\1***@", val)
    return event_dict


def configure_logging(
    level: str = "INFO",
    json: bool = True,
    log_dir: Path | None = None,
) -> None:
    """Configure structlog + stdlib logging.

    Re-runs when any requested arg differs from the current configuration
    (level / json / log_dir); identical calls are idempotent no-ops.
    """
    global _CONFIGURED
    level_num = getattr(logging, level.upper(), logging.INFO)
    log_dir_key: str | None = None
    if log_dir is not None:
        log_dir_key = str(Path(log_dir).resolve())
    requested = (level.upper(), bool(json), log_dir_key)
    if _CONFIGURED == requested:
        return

    processors_pre: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        _redact_userinfo,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=processors_pre + [renderer],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(level_num)
    # Reset handlers so reconfiguration is clean for tests.
    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level_num)
    sh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(sh)

    if log_dir_key is not None:
        log_dir = Path(log_dir_key)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "awareness.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(level_num)
        fh.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(fh)

    # Quiet down noisy libs by default.
    for noisy in ("httpx", "httpcore", "urllib3", "trafilatura", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level_num, logging.WARNING))

    _CONFIGURED = requested


def _bootstrap_args() -> tuple[str, bool]:
    """Resolve bootstrap level/json: settings when available, else env vars.

    Settings are read lazily (and defensively) so importing this module never
    triggers a settings/import cycle; ``AW_LOG_LEVEL`` / ``AW_LOG_JSON`` stay
    the fallback when no settings are configured yet.
    """
    level = os.environ.get("AW_LOG_LEVEL", "INFO")
    json_mode = os.environ.get("AW_LOG_JSON", "true").lower() == "true"
    try:
        from awareness.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        level = str(settings.log_level or level)
        json_mode = bool(settings.log_json)
    except Exception:
        pass
    return level, json_mode


def get_logger(name: str | None = None) -> Any:
    """Return a structlog bound logger."""
    if _CONFIGURED is None:
        level, json_mode = _bootstrap_args()
        configure_logging(level=level, json=json_mode)
    return structlog.get_logger(name) if name else structlog.get_logger()
