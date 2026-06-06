## 2026-06-06T00:00:39Z
You are the Worker agent. Your task is to implement the terminal improvements for the Awareness engine in the copied repository `/Users/nazmi/awareness_dev` according to the specified requirements.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Requirements to implement:
1. R1. TUI Live Capture Panel:
   - Split the TUI layout's `right` column into `right_top` (Recent Jobs), `right_middle` (Recent Captures), and `right_bottom` (Disk Storage Breakdown).
   - In the `tui` command, instantiate `DuckDbIndex` once and pass it to `_make_tui_layout`.
   - In `_make_tui_layout`, execute the query to fetch the 10 most recent captures:
     `SELECT fetch_ts, title, domain, source_type FROM captures ORDER BY fetch_ts DESC LIMIT 10`
   - Render these in a Table in `right_middle` showing HH:MM:SS Time, Title, and Domain.

2. R2. TUI Job Management Controls:
   - Add `delete_job(self, job_id: str) -> None` in `src/awareness/storage/state.py` to delete job and task rows.
   - In `WorkerEngine.run_job` and `WorkerEngine.run_tail` loops, check `self._state.get_job(job_id).status` at each iteration and break/stop execution if status is `CANCELLED` or `FAILED`.
   - Track `selected_job_idx` in the `tui` loop. Handle `up` and `down` arrow key presses to change selection and highlight the selected job in the Jobs table.
   - Bind `S` to cancel the selected running job (prompt for confirmation, change status to CANCELLED).
   - Bind `D` to delete the selected non-running job (prompt for confirmation, call `delete_job`).
   - Bind `N` to start/submit a new job (prompt for start date, end date, sources, domain filters, match queries; submit via Planner; spawn the runner process in background).

3. R3. Search & Browse Keyword Highlighting:
   - Highlight query tokens in `search` output table cells (title/snippet) using rich tags (bold yellow).
   - Add a `--query` option to the `browse` command, and highlight query tokens in the browse table and read view.

4. R4. Interactive Shell Autocomplete & History:
   - Load and save command history from `~/.awareness_history`.
   - Implement Tab-completion in `_setup_shell_readline` using standard python `readline` to support subcommands, config keys, and schema choices.

5. Testing & Verification:
   - Create new unit/integration tests to verify these features (e.g. autocompleter returning expected options, job deletion clearing DB, highlight formatting matching tokens).
   - Run the full test suite using `.venv/bin/pytest` and ensure all tests pass.
   - Provide the test execution command and output in your handoff.

Write your changes and test outputs to `.agents/worker_m2/changes.md` and `handoff.md`.
