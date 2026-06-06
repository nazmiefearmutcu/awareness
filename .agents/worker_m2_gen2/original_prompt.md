## 2026-06-06T01:15:36Z
Goal
Resume and complete Milestone 2: TUI Live Capture Panel (R1) and TUI Job Management Controls (R2) in the development repository /Users/nazmi/awareness_dev.

Status and Context
The previous worker started the implementation but was interrupted. The following changes are already present in the workspace:
1. delete_job database logic is implemented in src/awareness/storage/state.py with passing unit tests in tests/unit/test_planner.py.
2. Cooperative cancellation checking is implemented in WorkerEngine.run_job and WorkerEngine.run_tail in src/awareness/workers/engine.py.
3. _make_tui_layout is modified to split the right panel, accept idx and selected_job_idx, fetch recent captures from DuckDB, and highlight the selected job in src/awareness/cli/main.py.

Requirements & Design
You need to complete the following:
1. Initialize idx = DuckDbIndex(...) once outside the loop in the tui command (around line 2060 in src/awareness/cli/main.py). Use:
   idx = DuckDbIndex(
       db_path=settings.duckdb_path(),
       jsonl_dir=settings.staging_jsonl_dir(),
       iceberg_warehouse=settings.iceberg_warehouse,
   )
2. Maintain selected_job_idx (default 0) in the TUI loop in the tui command.
3. In the TUI loop:
   - Call _make_tui_layout(state, settings, idx, selected_job_idx) when updating layout.
   - Read keyboard inputs when current_view == "dashboard":
     - Keys up / k: decrement selected_job_idx (minimum 0).
     - Keys down / j: increment selected_job_idx. Check jobs = state.list_jobs(limit=5) to bound selected_job_idx by len(jobs) - 1 so selection doesn't go out of bounds.
     - Key s (Stop/Cancel): If there is a selected job and its status is running (or not completed/cancelled), update its status in the DB to cancelled. Put status feedback in status_msg (e.g. [yellow]Cancelled job <job_id>[/yellow]).
     - Key d (Delete): If there is a selected job and its status is NOT running, delete it using state.delete_job(selected_job.job_id). Put status feedback in status_msg (e.g. [green]Deleted job <job_id>[/green]).
     - Key n (New Job): Stop the Live view (live.stop()), print prompts to the console to request:
       - Job Type (backfill or tail)
       - If backfill: Start date, End date, Sources (comma-separated), Match keywords (comma-separated, optional)
       - If tail: Duration (seconds), Sources (comma-separated), Match keywords (comma-separated, optional)
       Submit the job to StateDB / Planner, spawn the runner process in the background using subprocess.Popen (for backfill, run awareness backfill run <job_id> --silent-progress; for tail, run awareness tail start --no-interactive ...), restart the Live view (live.start()), and update status_msg with the new job ID.
4. Verify all tests pass, and write/run new unit/integration tests for the TUI controls and job deletion.
