## 2026-06-06T21:54:40Z

You are teamwork_preview_worker. Your working directory is /Users/nazmi/awareness_dev/.agents/worker_m2.

### Goal
Implement Milestone 2: TUI Live Capture Panel (R1) and TUI Job Management Controls (R2) in the development repository /Users/nazmi/awareness_dev.

### Requirements & Design
1. R1: Live Capture Panel
   - In `src/awareness/cli/main.py`, modify `_make_tui_layout` to split `layout["right"]` into `right_top` (Jobs), `right_middle` (Recent Captures), and `right_bottom` (Disk Storage Breakdown).
   - Instantiate `idx = DuckDbIndex(...)` once outside the loop in the `tui` command (in `src/awareness/cli/main.py`), and pass it as an argument to `_make_tui_layout`.
   - In `_make_tui_layout`, fetch the 10 most recent captures from DuckDB using a query on the `captures` view, ordered by `fetch_ts DESC limit 10`.
   - Display a `Table` in the `right_middle` panel with columns: Time (HH:MM:SS format), Title, and Domain. Make sure it updates dynamically on every TUI refresh.
2. R2: TUI Job Management Controls
   - Maintain the active selection state (e.g., `selected_job_idx`) in the TUI loop in the `tui` command.
   - Add bindings for Up/Down arrow keys (already mapped to "up"/"down" in `_get_key_nonblocking()`) to select a job from the "Recent Jobs" list. Highlight the selected job row in the TUI.
   - Support the following keyboard controls on the selected job:
     - `S` (Stop/Cancel): If the selected job is running, set its status in the DB to `cancelled`. Update the status message in the footer with feedback.
     - `D` (Delete/Clear): If the selected job is not running, delete it from the DB. Add a new method `delete_job(self, job_id: str) -> None` in `StateDB` (in `src/awareness/storage/state.py`) that deletes the job row and any associated task rows. Update the status message in the footer with feedback.
     - `N` (New Job): Stop the Live view (`live.stop()`), prompt the user in the terminal for:
       - Job Type (backfill or tail)
       - If backfill: Start date, End date, Sources (comma-separated), Match keywords (comma-separated, optional)
       - If tail: Duration (seconds), Sources (comma-separated), Match keywords (comma-separated, optional)
       Then submit the job. If backfill, call `planner.submit_backfill(req)` and spawn the worker in the background using `subprocess.Popen` running `awareness backfill run <job_id> --silent-progress`. If tail, spawn `awareness tail start --no-interactive ...` in the background. Finally, restart the Live view (`live.start()`) and set feedback in the footer.
   - In `WorkerEngine.run_job` and `WorkerEngine.run_tail` (in `src/awareness/workers/engine.py`), check `self._state.get_job(job_id).status` periodically. If the status is `cancelled`, terminate execution cooperatively. If `paused`, sleep.

### Verification
- Run the test suite: `.venv/bin/pytest`
- Add unit/integration tests for:
  - Database job deletion (`delete_job`)
  - Autocomplete, TUI, etc., if applicable.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
