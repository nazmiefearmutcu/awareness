# PRD: Professional Terminal Telemetry and Live Progress Instrumentation

## Goal
Transform the `awareness` CLI into a highly professional terminal tool by adding interactive progress visualization, live speed/throughput calculation, and configurable logging behavior during execution.

## Tasks
1. **Task 1 (Interactive Rich Progress Bar):** Implement a dynamic `rich.progress.Progress` bar in `WorkerEngine.run_job` when running in TTY mode. The progress bar must display:
   * Task completion status (completed/total tasks).
   * Ingestion speed (throughput in MB/s and documents/second).
   * Accumulative sizes and deduplication stats.
2. **Task 2 (Premium Ingestion Summary Table):** Add a post-execution summary using `rich.table.Table` that outputs a detailed physical storage profile and performance breakdown at the end of backfill runs.
3. **Task 3 (Silent Progress Option):** Add a `--silent-progress` configuration option to allow users to toggle off per-document logs and show only the progress bar during large backfills.
4. **Task 4 (Ralph Loop Self-Verification):** Append a verification comment `# Ralph Loop Verified` to the end of `pyproject.toml` to verify the execution of the autonomous `ralph_loop.py` script.
5. **Task 5 (Writable Health Telemetry):** Update the `health` subcommand in `src/awareness/cli/main.py` to check and report if database and staging directory paths are writable, enhancing diagnostics.
6. **Task 6 (Search Results Byte Size Metric):** Modify the `search` and `inspect` output templates in `src/awareness/cli/main.py` to show the exact byte size of matching documents in addition to the character counts.
7. **Task 7 (Shell REPL Commands List):** Extend the interactive command shell `shell` subcommand in `src/awareness/cli/main.py` to list available commands dynamically when an invalid command is typed.
