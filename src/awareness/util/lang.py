"""BCP-47 language tag helpers for SQL filters and identity."""

from __future__ import annotations

from typing import Any


def normalize_language_tag(language: str | None) -> str | None:
    """Lowercase and normalize separators (``en_US`` → ``en-us``). Empty → None."""
    if language is None:
        return None
    raw = str(language).strip().lower().replace("_", "-")
    parts = [p for p in raw.split("-") if p]
    if not parts:
        return None
    return "-".join(parts)


def primary_language_tag(language: str | None) -> str | None:
    """Return the BCP-47 primary subtag (``en-US`` / ``en_GB`` → ``en``).

    Used by aggregate counts so regional variants roll up under one language
    bucket for operators. Empty / missing → ``None``.
    """
    tag = normalize_language_tag(language)
    if not tag:
        return None
    return tag.split("-", 1)[0]


def primary_language_sql(column: str = "language") -> str:
    """DuckDB expression: normalize *column* then take the BCP-47 primary subtag.

    Keep in sync with ``primary_language_tag`` (underscore → hyphen, lower, split).
    *column* must be a trusted identifier (code-owned, never request input).
    """
    return (
        f"split_part(lower(replace(CAST({column} AS VARCHAR), '_', '-')), '-', 1)"
    )


# Default over the bare ``language`` column (counts / facet WHERE path).
PRIMARY_LANGUAGE_SQL = primary_language_sql("language")


def language_sql_filter(
    language: str | None,
    *,
    column: str = "language",
    param: str = "lang",
) -> tuple[str | None, dict[str, Any]]:
    """Return ``(sql_clause, bind_params)`` for a language filter, or ``(None, {})``.

    Primary tags without a region/script (e.g. ``en``) match both the bare tag
    and any more-specific subtags (``en-US``, ``en-GB``). Full tags match
    exactly after normalization (case-insensitive; ``_`` treated as ``-``).

    Stored values may use either ``en-US`` or ``en_US``; both are normalized
    via ``replace(..., '_', '-')`` before comparison.
    """
    tag = normalize_language_tag(language)
    if not tag:
        return None, {}
    # Normalize stored tags the same way (underscore → hyphen, lower).
    col = f"lower(replace(CAST({column} AS VARCHAR), '_', '-'))"
    if "-" not in tag:
        # Primary-only: match bare tag and any subtag (en, en-us, en-gb).
        # Use a distinct param name (not a prefix of *param*) so bind drivers
        # cannot confuse ``$lang`` with ``$langpfx`` during substitution.
        pfx = f"{param}pfx"
        return (
            f"({col} = ${param} OR {col} LIKE ${pfx})",
            {param: tag, pfx: f"{tag}-%"},
        )
    return f"{col} = ${param}", {param: tag}


def append_language_filter(
    where: list[str],
    params: dict[str, Any],
    language: str | None,
    *,
    column: str = "language",
    param: str = "lang",
) -> None:
    """Append a language filter clause + bind params when *language* is set."""
    clause, extra = language_sql_filter(language, column=column, param=param)
    if clause:
        where.append(clause)
        params.update(extra)
