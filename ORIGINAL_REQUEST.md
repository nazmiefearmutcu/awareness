# Original User Request

## Request — 2026-06-06T00:44:05Z

Implement terminal improvements for the Awareness engine:
1. Real-time capture stream panel in the TUI dashboard.
2. Job management from the TUI (start, stop, delete).
3. Search term highlighting in terminal search results.
4. Auto-complete and command history in `awareness shell`.

Working directory: /Users/nazmi/awareness_dev
Integrity mode: development

## Requirements

### R1. TUI Live Capture Panel
- Add a new panel to the TUI (`tui` command) named "Recent Captures" or "Live Stream" that lists the 10 most recently captured documents.
- Query these documents from the DuckDB index (`captures` view) and refresh them periodically.
- Display columns: Time (HH:MM:SS), Title, and Domain.

### R2. TUI Job Management Controls
- Implement interactive job management inside the TUI dashboard.
- The user can select a job from the "Recent Jobs" list (e.g., using Up/Down arrow keys or focus binding).
- Support keyboard actions: `[S]` to stop a selected running job, `[D]` to delete/clear a selected job, and `[N]` to start/trigger a new backfill or tail job.

### R3. Search & Browse Keyword Highlighting
- Enhance the `search` and `browse` commands to scan the returned document text and title for matching query tokens.
- Apply formatting (e.g. bold yellow via `rich` styling tags) to highlight the matching query tokens in the terminal output.

### R4. Interactive Shell Autocomplete & History
- Enhance `awareness shell` to load and save command history from a persistent file (e.g., `~/.awareness_history`).
- Enable tab-completion for all subcommands (e.g., `backfill`, `tail`, `configure`, `dedup`, `search`) using standard Python `readline` features.

## Acceptance Criteria

### TUI Layout & Ingestion Stream
- [ ] TUI layout shows the new capture stream panel containing the 10 most recent captures with columns: Time (HH:MM:SS), Title, and Domain.
- [ ] The captures view refreshes periodically when new captures land in DuckDB.

### TUI Job Controls
- [ ] User can interactively select jobs from the list.
- [ ] Pressing `[S]` pauses/stops the selected running job with status feedback.
- [ ] Pressing `[D]` deletes the selected job row from the state DB with status feedback.
- [ ] Pressing `[N]` prompts for dates/sources and triggers a new backfill or tail job running as a background worker.

### Search and Shell Enhancements
- [ ] `awareness search` prints highlighted query words in title and snippet text in bold yellow via Rich.
- [ ] `awareness browse` supports a `--query` / `-q` option and highlights matching words in bold yellow.
- [ ] `awareness shell` recalls previous commands via Up/Down keys across sessions using `~/.awareness_history`.
- [ ] `awareness shell` auto-completes subcommands (e.g. `config set`) on Tab key press.
