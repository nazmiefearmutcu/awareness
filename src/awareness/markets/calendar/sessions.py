"""Exchange trading sessions (open/close/lunch break, early closes) from ``exchange_calendars``.

Computed offline from the exchange rule sets — no network. Used by showme's
trading-hours pane (TRDH), which previously had wrong lunch breaks / closes for
Tokyo and Hong Kong.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

SUPPORTED_HINT = (
    "XNYS XNAS XIST XLON XETR XPAR XTKS XHKG XSHG XTSE XASX XSWX XAMS XMIL XMAD XKRX XBOM"
)


def _to_utc(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        import pandas as pd

        if pd.isna(ts):
            return None
    except (ImportError, TypeError, ValueError):
        return None if ts is None else _plain(ts)
    return _plain(ts)


def _plain(ts: Any) -> datetime | None:
    dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def exchange_sessions(mic: str, start: date, end: date) -> dict[str, Any]:
    """Sessions of ``mic`` between ``start`` and ``end`` (inclusive, clipped to the calendar's bounds).

    Raises ``ValueError`` for an unknown MIC.
    """
    import exchange_calendars as xcals

    code = mic.strip().upper()
    try:
        cal = xcals.get_calendar(code)
    except Exception as exc:
        raise ValueError(f"unknown exchange {mic!r}; try one of: {SUPPORTED_HINT}") from exc
    first = max(start, cal.first_session.date())
    last = min(end, cal.last_session.date())
    if last < first:
        return {"exchange": code, "timezone": str(cal.tz), "sessions": []}
    sessions = cal.sessions_in_range(first.isoformat(), last.isoformat())
    tz = cal.tz
    rows: list[dict[str, Any]] = []
    close_counter: Counter[str] = Counter()
    for session in sessions:
        open_utc = _to_utc(cal.session_open(session))
        close_utc = _to_utc(cal.session_close(session))
        break_start = _to_utc(cal.session_break_start(session))
        break_end = _to_utc(cal.session_break_end(session))
        if open_utc is None or close_utc is None:
            continue
        close_local = close_utc.astimezone(tz).strftime("%H:%M")
        close_counter[close_local] += 1
        rows.append(
            {
                "date": session.date().isoformat(),
                "open_utc": open_utc.isoformat(),
                "close_utc": close_utc.isoformat(),
                "break_start_utc": break_start.isoformat() if break_start else None,
                "break_end_utc": break_end.isoformat() if break_end else None,
                "open_local": open_utc.astimezone(tz).strftime("%H:%M"),
                "close_local": close_local,
                "break_local": (
                    f"{break_start.astimezone(tz).strftime('%H:%M')}-{break_end.astimezone(tz).strftime('%H:%M')}"
                    if break_start and break_end
                    else None
                ),
            }
        )
    regular_close = close_counter.most_common(1)[0][0] if close_counter else None
    early_set = {d.date().isoformat() for d in cal.early_closes if first <= d.date() <= last}
    for row in rows:
        row["early_close"] = row["date"] in early_set or (
            regular_close is not None and row["close_local"] < regular_close
        )
    return {
        "exchange": code,
        "timezone": str(tz),
        "regular_close_local": regular_close,
        "sessions": rows,
    }
