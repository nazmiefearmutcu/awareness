# Operations

Automation recipes for the awareness engine: scheduled briefings, weekly
email digests, the periodic alert runner, quality snapshots, and retention.
Every command here is verified against the CLI in
`src/awareness/cli/main.py` — see `tests/unit/test_docs_operations.py` for
the contract test that keeps the docs and the CLI in sync.

## Project root resolution (read this first)

`awareness` resolves the project root via `get_settings()` in
`src/awareness/config/settings.py`:

1. `AW_PROJECT_ROOT` env var, if set (this is the *preferred* way for cron —
   cron does not inherit your shell env or CWD).
2. Otherwise it is inferred from the installed package location
   (`src/awareness/config/settings.py` → 3 parents up). For a venv install
   that resolves to the venv's `site-packages` parent — which is **not** the
   project checkout — so `data_dir` would point into the venv.

`data_dir` defaults to `<project_root>/data`, which anchors every path the
scheduled commands touch: the DuckDB index, the SQLite state DB, and the
`saved briefings` directory. **Always export `AW_PROJECT_ROOT` in cron and
launchd jobs** pointing at the checkout:

```bash
export AW_PROJECT_ROOT=/path/to/awareness
```

## Daily briefing (cron)

`awareness briefing --save --json` builds the morning briefing (movers,
top terms, new domains, sentiment, alert firings, GDELT gaps) and:

- writes `{data_dir}/briefings/YYYY-MM-DD.json` (`--save`; an optional
  positional `[NAME]` appends `-NAME` for weekly variants),
- keeps stdout a pure JSON object — the save confirmation goes to stderr —
  so stdout can be piped/redirected safely.

Crontab line (07:00 daily, UTC; adjust for your TZ):

```cron
0 7 * * * cd /path/to/awareness && AW_PROJECT_ROOT=/path/to/awareness /path/to/awareness/.venv/bin/awareness briefing --save --json >> /var/log/awareness-briefing.log 2>&1
```

Notes:

- Use the venv's `awareness` entrypoint (`.venv/bin/awareness`) or
  `.venv/bin/python -m awareness.cli.main`; `PYTHONPATH` is not needed.
- `briefing` fetches GDELT coverage gaps by default. If the cron host has no
  network access to GDELT, add `--no-gdelt` (the JSON then carries
  `"gdelt_gaps": {"skipped": true}`).
- Check the log for the confirmation line (`Briefing saved to …`); the file
  lands under `data/briefings/`. An empty corpus prints
  `no corpus yet` and exits 0 — no file is written, the run is not a failure.

## Weekly digest email (cron)

`awareness report --email` renders the combined report (digest + corpus
quality + alert activity + GDELT context) as markdown and delivers it over
SMTP. Crontab line (08:00 every Monday):

```cron
0 8 * * 1 cd /path/to/awareness && AW_PROJECT_ROOT=/path/to/awareness /path/to/awareness/.venv/bin/awareness report --email you@example.com >> /var/log/awareness-report.log 2>&1
```

SMTP is configured via env vars (read in `_email_digest` in
`src/awareness/cli/main.py`; flags `--smtp-host/--smtp-port/--smtp-user/
--smtp-password/--from` override them):

| Env var          | Meaning                                            | Default  |
| ---              | ---                                                | ---      |
| `SMTP_HOST`      | SMTP server host (required)                        | —        |
| `SMTP_PORT`      | Port; `465` uses implicit SSL, anything else uses STARTTLS | `587` |
| `SMTP_USER`      | Login user (optional; enables `server.login`)      | —        |
| `SMTP_PASSWORD`  | Login password                                     | —        |
| `EMAIL_FROM`     | From address (falls back to `SMTP_USER`, then the recipient) | — |

Example wrapper for cron (credentials belong in the environment, not the
crontab line):

```bash
#!/bin/bash
export AW_PROJECT_ROOT=/path/to/awareness
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USER=awareness@example.com
export SMTP_PASSWORD='…'
export EMAIL_FROM='Awareness <awareness@example.com>'
exec /path/to/awareness/.venv/bin/awareness report --email you@example.com
```

`report --out /path/to/report.md` is the file-only variant; the markdown
goes to stdout when neither `--email` nor `--out` is set.

## Alerts runner (periodic evaluation + webhooks)

The API process hosts an optional periodic alert loop. Enable it with
`AW_ALERTS_AUTOSTART=1` before starting `awareness-api` (`api/server.py`
gates the runner on this exact value):

```bash
AW_ALERTS_AUTOSTART=1 awareness-api
```

- The loop (from `awareness/alerts/runner.py`) evaluates all active rules
  every 300s by default, clamped to a 30s floor; the first pass runs
  immediately at startup.
- Every firing whose rule has a webhook URL is delivered via HTTP POST
  (the delivery payload format is the `webhook_format` rule field; the
  legacy single `webhook_url` is the fallback when the webhook list is
  empty).
- A tick that raises is logged and the loop continues.
- Without the env var, evaluate on demand:

  ```bash
  awareness alerts check       # all active rules, one pass
  awareness alerts run-once    # same, via the runner entry point
  ```

- The runner lives and dies with the API process — keep the API running
  (`awareness service install` installs it as a macOS Launch Agent).

## Quality recording (daily cron hook)

`awareness quality --record` persists the current corpus-quality snapshot
(sizes, duplicate / near-dup ratios, languages, domains) as one JSON
object, appended to `<data_dir>/quality_history.jsonl` — an append-only
JSONL store chosen so a crash mid-write can never corrupt history (a torn
final line is skipped on read, every earlier line stays intact). The
snapshot comes from `CorpusXEngine.quality_snapshot()`; an empty corpus
still records the zeroed snapshot (total=0) so the trend has no gaps —
note that a cron running before first ingestion records zero-days. Read
the recorded series back with `awareness quality --recorded N` (last N
days, table or `--json`); the per-day history served by
`/qualityx/history` is computed directly from the corpus, so a missing or
empty store never blocks reads.

Crontab line (06:30 daily, UTC; adjust for your TZ):

```cron
30 6 * * * cd /path/to/awareness && AW_PROJECT_ROOT=/path/to/awareness /path/to/awareness/.venv/bin/awareness quality --record >> /var/log/awareness-quality.log 2>&1
```

Notes:

- `quality --record` prints a one-line confirmation (`Recorded quality
  snapshot: <path> (total=…, dup=…%)`); add `--json` to emit the record
  as JSON on stdout instead.
- The **daily briefing hook stays `briefing --save`** (see "Daily
  briefing" above): every morning it persists the full briefing JSON
  (movers, terms, sentiment, alert summary) under `data/briefings/`.
  `quality --record` is the *quality-series* complement — the recorded
  snapshots back a persisted quality trend, while the briefing is the
  operator-facing morning read.
- `awareness report --json` still embeds a live `quality` snapshot in its
  payload; on-demand snapshots remain `awareness quality --json`.

## Weekly alert summary (cron)

`awareness alerts weekly` prints a 7-day (UTC) alert summary — total
firings, exact per-rule counts (SQL, so they survive the 500-row list
clamp), each rule's last firing, the top rule, and a Mon..Sun distribution
as a block sparkline; `--json` emits the raw payload (`window_days`,
`since`, `total_firings`, `rules`, `top_rule`, `rules_fired` /
`rules_total`) for scripting. An empty week prints `No alert firings in
the last 7 days.` and exits 0. Crontab line (09:00 every Monday):

```cron
0 9 * * 1 cd /path/to/awareness && AW_PROJECT_ROOT=/path/to/awareness /path/to/awareness/.venv/bin/awareness alerts weekly --json >> /var/log/awareness-alerts-weekly.log 2>&1
```

## macOS launchd example (daily briefing)

`awareness service install` generates a Launch Agent plist for the API
server with this vocabulary: `Label`, `WorkingDirectory`,
`EnvironmentVariables`, `ProgramArguments`, `RunAtLoad`, `KeepAlive`,
`StandardOutPath`, `StandardErrorPath` (see `service_install` in
`cli/main.py`; `service schedule-compaction` adds `StartInterval`).
A scheduled one-shot job needs a calendar schedule instead of
`RunAtLoad`/`KeepAlive`, so the briefing plist below mirrors the same keys
and swaps in the launchd-native `StartCalendarInterval`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.awareness.briefing</string>

  <key>WorkingDirectory</key>
  <string>/path/to/awareness</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>/path/to/awareness/src</string>
    <key>AW_PROJECT_ROOT</key>
    <string>/path/to/awareness</string>
  </dict>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/awareness/.venv/bin/python</string>
    <string>-m</string>
    <string>awareness.cli.main</string>
    <string>briefing</string>
    <string>--save</string>
    <string>--json</string>
    <string>--no-gdelt</string>
  </array>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>7</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>/var/log/awareness-briefing.launch.out</string>
  <key>StandardErrorPath</key>
  <string>/var/log/awareness-briefing.launch.err</string>
</dict>
</plist>
```

Install it (after editing the two `/path/to/awareness` occurrences):

```bash
cp com.awareness.briefing.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.awareness.briefing.plist
# test the command the plist runs, from the same env:
AW_PROJECT_ROOT=/path/to/awareness /path/to/awareness/.venv/bin/python \
  -m awareness.cli.main briefing --save --json --no-gdelt
# and for the API-server variant: awareness service install
```

## Retention

Saved briefings accumulate one file per day (~a few KB each). Prune with
`find` from the project root:

```bash
find /path/to/awareness/data/briefings -type f -name '*.json' -mtime +30 -delete
```

`data/alerts.db` (SQLite) grows unbounded: the `firings` table only ever
appends — there is no built-in purge, and the `idx_firings_fired_at` index
makes manual deletion cheap. `fired_at` is stored as ISO-8601 text, so a
plain SQLite date comparison works:

```bash
sqlite3 /path/to/awareness/data/alerts.db \
  "DELETE FROM firings WHERE fired_at < datetime('now', '-90 day');" \
  "VACUUM;"
```

Run both from cron (e.g. monthly) or fold them into the daily briefing job.
