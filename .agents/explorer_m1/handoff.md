# Handoff Report: Terminal Improvements Explorer Investigation

## 1. Observation

### Codebase Entry Points & Components
We located the implementations of the TUI layout, search commands, browse commands, and interactive shell commands in the following locations:
*   **TUI Command & Layout**: 
    *   `src/awareness/cli/main.py:1984` defines the `@app.command(name="tui")` command.
    *   `src/awareness/cli/main.py:1727` defines `_make_tui_layout(state: StateDB, settings: Any) -> Any`, which constructs the dashboard using `rich.layout.Layout` panels.
    *   `src/awareness/cli/main.py:1901` defines `_make_tui_log_layout(...)` which compiles application/API logs.
*   **Browse Command**:
    *   `src/awareness/cli/main.py:2180` defines `@app.command(name="browse")` which lets users page through database captures.
*   **Search Command**:
    *   `src/awareness/cli/main.py:2297` defines `@app.command(name="search")` which queries documents via the full-text search (FTS) index.
*   **Interactive REPL Shell**:
    *   `src/awareness/cli/main.py:3696` defines `@app.command(name="shell")` which provides the interactive REPL.
    *   `src/awareness/cli/main.py:3622` defines `_setup_shell_readline(click_cmd: Any, history_file: Path | None) -> bool` to manage Arrow key history and TAB completions.

### Database schemas & worker engines:
*   **State DB**: `src/awareness/storage/state.py` defines the SQLite State database schemas (`JobRow`, `TaskRow`, `DedupRow`, etc.) and the state access methods.
*   **DuckDB FTS Index**: `src/awareness/storage/duckdb_index.py` handles the document full-text indexing and BM25 search queries.
*   **Worker Engine**: `src/awareness/workers/engine.py` executes the backfill and tail ingestion loops.

### Baseline Test Executions
We ran the project test suite using the command `.venv/bin/pytest`. The tests completed successfully with the following output:
```
tests/integration/test_topic_filter_pipeline.py: 4 warnings
tests/integration/test_worker_pipeline.py: 4 warnings
tests/smoke/test_tail_lifecycle.py: 15 warnings
tests/unit/test_urls.py: 2 warnings
  /Volumes/disk 2/Desktop_Migrate_2026-05-28/Projeler/proje/awareness/src/awareness/util/urls.py:94: DeprecationWarning: The 'registered_domain' property is deprecated and will be removed in the next major version. Use 'top_domain_under_public_suffix' instead, which has the same behavior but a more accurate name.
    primary = getattr(ext, "top_domain_under_public_suffix", None) or ext.registered_domain

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
178 passed, 25 warnings in 19.65s
```

---

## 2. Logic Chain

1. **R1 (Live Capture Panel)**: The TUI splits the screen layout using `rich.layout.Layout`. By adding a new pane to the right column split (`right_middle` in addition to `right_top` and `right_bottom`), we can render a new table panel. Executing a `SELECT` query against the DuckDB captures table sorted by `fetch_ts DESC LIMIT 10` will supply the 10 most recent captures. By instantiating `DuckDbIndex` once outside the loop instead of recreating it inside the `_make_tui_layout` tick, we prevent redundant file scans.
2. **R2 (Job Management Controls)**: Intercepting keypresses in the non-blocking TUI input loop is already done for `Q`, `C`, `T`, `A`, `R`, and `L`. By adding bindings for `S`, `D`, and `N`, we can pause the full-screen `rich.live.Live` view using `live.stop()`, request inputs cleanly from the standard tty stream, execute database status transitions or delete actions (with a newly added `delete_job` method on `StateDB` to clean up `JobRow` and `TaskRow`), or spawn new processes for background workers using `subprocess.Popen([sys.executable, "-c", ...], start_new_session=True)`, and then resume with `live.start()`.
3. **R3 (Highlighting)**: Rich formatting parses markup syntax like `[bold yellow]text[/bold yellow]`. The FTS index search returns terms matched by the query. In `search` command's table formatter, we can run a regex replace mapping target query terms to highlighted markups. To support `browse` highlighting, we can add a `--query` or `--highlight` parameter to the browse command, which is then used to highlight matches in both the browse lists and document read viewer.
4. **R4 (Shell Autocomplete & History)**: Python's `readline` module relies on a completion function. The existing completer only evaluates `len(tokens) <= 1`. By tracking `current_idx` (based on whether the buffer ends with a space and the token count), we can determine the exact depth of the input and dynamically feed options: subcommand names, configuration keys from `CONFIG_SCHEMA`, or choices/booleans from individual configuration schema fields.

---

## 3. Caveats

*   **DuckDB Lock Constraints**: When running backfill workers in the background, they might write to the staging directories or Iceberg warehouse. DuckDB supports concurrent reads if configured properly, but write locks must be respected. Staging JSONL views are generated dynamically and do not lock the main DuckDB catalog during standard queries.
*   **Readline Library Portability**: Under macOS, python's built-in `readline` is frequently linked against `libedit` (EditLine). We must preserve the existing `if "libedit" in ...` fallback check when registering completion and key bindings.
*   **Terminal Ratios**: Adding a third panel in the right column might require adjusting terminal size. Layout constraints should be robust enough to handle compact terminal windows.

---

## 4. Conclusion & Recommendations

We recommend implementing the terminal improvements as follows:

### R1: TUI Live Capture Panel
1. Modify `_make_tui_layout` to split `layout["right"]` into `right_top` (Jobs), `right_middle` (Captures), and `right_bottom` (Disk breakdown).
2. Instantiate `idx = DuckDbIndex(...)` once in the `tui` command and pass it as an argument to `_make_tui_layout`.
3. Inside `_make_tui_layout`, execute the query:
   ```sql
   SELECT domain, title, fetch_ts, source_type
   FROM captures
   ORDER BY fetch_ts DESC
   LIMIT 10
   ```
4. Build a `Table` with columns: Domain, Title, Captured At, Source, and render it in a panel inside `layout["right_middle"]`.

### R2: TUI Job Management Controls
1. Add `delete_job(self, job_id: str) -> None` in `src/awareness/storage/state.py` to delete job and task rows.
2. In `WorkerEngine.run_job` and `WorkerEngine.run_tail`, check `self._state.get_job(job_id).status` at the beginning of each poll loop and terminate running execution cooperatively if the status is `cancelled` or `paused`.
3. Inside the `tui` key interceptor loop:
    *   **`S`**: Stop a job. Stop the live view, prompt for Job ID, verify its existence, call `state.set_job_status(job_id, JobStatus.CANCELLED)`, and restart the live view.
    *   **`D`**: Delete a job. Stop live view, prompt for Job ID, ensure it isn't running, call `state.delete_job(job_id)`, and restart live view.
    *   **`N`**: Start/submit a new job. Stop live view, prompt for start date, end date, sources, and match query terms. Submit the backfill job to the planner, spawn it in the background using `subprocess.Popen([sys.executable, "-c", f"import sys; sys.argv = ['awareness', 'backfill', 'run', '{job_id}', '--silent-progress']; from awareness.cli.main import app; app()"], start_new_session=True)`, and restart live view.

### R3: Search & Browse Keyword Highlighting
1. In `search` command's result table construction, apply regex-based bold yellow markup replacement to matched query terms within both the title and snippet columns.
2. Add a `query: str = typer.Option("", "--query", "-q", help="Search keyword(s) to highlight")` parameter to the `browse` command.
3. In `browse` table list and document view reader, use a case-insensitive regex pattern matching the query terms to wrap hits in `[bold yellow]...[/bold yellow]` tags.

### R4: REPL Shell Autocomplete
Replace the completion function in `_setup_shell_readline` with a multi-token aware parser:
```python
def completer(text: str, state: int) -> str | None:
    try:
        buffer = readline.get_line_buffer()
        tokens = buffer.lstrip().split()
        num_tokens = len(tokens)
        
        if buffer.endswith(" "):
            current_idx = num_tokens
            prev_tokens = tokens
        else:
            current_idx = num_tokens - 1
            prev_tokens = tokens[:-1]
            
        options = []
        if current_idx == 0:
            options = [c for c in top if c.startswith(text)]
        elif current_idx == 1:
            subs = _shell_subcommands(click_cmd, prev_tokens[0])
            pool = subs if subs else top
            options = [c for c in pool if c.startswith(text)]
        elif current_idx == 2:
            if prev_tokens[0] == "config" and prev_tokens[1] in ("get", "set", "unset"):
                from awareness.config.schema import CONFIG_SCHEMA
                options = [f.key for f in CONFIG_SCHEMA if f.key.startswith(text)]
            elif prev_tokens[-1] in ("--source", "-s"):
                from awareness.schemas.doc import SourceKind
                options = [sk.value for sk in SourceKind if sk.value.startswith(text)]
        elif current_idx == 3:
            if prev_tokens[0] == "config" and prev_tokens[1] == "set":
                key = prev_tokens[2]
                from awareness.config.schema import CONFIG_SCHEMA
                f = next((field for field in CONFIG_SCHEMA if field.key == key), None)
                if f:
                    choices = ["true", "false"] if f.kind == "bool" else (list(f.choices) if f.choices else [])
                    options = [c for c in choices if c.startswith(text)]
                    
        return options[state] if state < len(options) else None
    except Exception:
        return None
```

---

## 5. Verification Method

1. **Unit and Integration Tests**:
   *   Execute `.venv/bin/pytest` to verify the baseline tests continue to pass after any changes.
   *   Add unit tests in `tests/unit/` to verify autocomplete returns correct choices for config keys and bool options, and verifying database `delete_job` cleanly removes DB constraints.
2. **Interactive Manual Audits**:
   *   `awareness tui`: Verify the live capture panel renders the 10 most recent captures dynamically, and pressing `S`, `D`, `N` prompts for actions and updates background jobs in real-time.
   *   `awareness search "climate"`: Verify the table view shows highlighted query terms in bold yellow.
   *   `awareness browse -q "internet"`: Verify the table list and full document reader highlight matching occurrences in yellow.
   *   `awareness shell`: Press TAB at `config set ` or `config set enable_iceberg ` to verify options are autocompleted dynamically.
