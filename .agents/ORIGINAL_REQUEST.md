# Original User Request

## Initial Request — 2026-06-05T21:02:35Z

Implement a set of terminal improvements for the Awareness engine:
1. Real-time capture stream panel in the TUI dashboard.
2. Job management from the TUI (start, stop, delete).
3. Search term highlighting in terminal search results.
4. Auto-complete and command history in `awareness shell`.

Working directory: /Users/nazmi/Desktop/Projeler/proje/awareness
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

### TUI Enhancements
- [ ] TUI layout shows the new capture stream panel containing the 10 most recent captures.
- [ ] User can interactively stop, start, and delete jobs from the TUI interface with visible status feedback messages.

### Search and Shell Enhancements
- [ ] `awareness search` prints highlighted query words in title and snippet text using Rich styling.
- [ ] `awareness shell` recalls previous commands via Up/Down keys across sessions.
- [ ] `awareness shell` auto-completes subcommands on Tab key press.
