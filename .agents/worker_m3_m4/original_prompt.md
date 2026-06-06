## 2026-06-06T00:00:00Z
You are Worker M3 M4.
Your working directory is `/Users/nazmi/awareness_dev/.agents/worker_m3_m4`.
Your mission is to implement terminal improvements for the Awareness engine in `/Users/nazmi/awareness_dev` as specified in the following requirements:

## M3: Search & Browse Keyword Highlighting (R3)
1. Enhance the `search` command in `src/awareness/cli/main.py`:
   - Non-interactive output: Highlight matching query tokens in the title and snippet text. Modify the base color of the printed title (e.g., from `[bold yellow]` to standard bold white or similar) so the bold yellow matching tokens stand out clearly.
   - Interactive table view: Highlight matching query tokens in the title and snippet columns.
   - Document read view: Highlight matching query tokens in the title as well (the text body is already highlighted).
2. Enhance the `browse` command in `src/awareness/cli/main.py`:
   - Add a `query` option to the browse command: `query: str = typer.Option("", "--query", "-q", help="Search query/terms to highlight")`.
   - Highlight matching query tokens in the interactive browse table's title.
   - Highlight matching query tokens in the document read view's title and text body, using the same highlighting logic.
3. Build/use a robust `highlight_query(text: str, query: str) -> str` helper function. Use `rich.markup.escape` on the text first, extract query tokens using case-insensitive regex matching (length >= 2 words), and wrap matches in `[bold yellow]...[/bold yellow]`.

## M4: Interactive Shell Autocomplete & History (R4)
1. Enhance `awareness shell` in `src/awareness/cli/main.py`:
   - Persistent history: Load and save command history from `~/.awareness_history` (resolved via `Path("~/.awareness_history").expanduser()`). Handle fallback gracefully.
   - Tab completion: Ensure autocomplete suggestions work cleanly for all first-level commands and their respective subcommands (e.g., `backfill`, `tail`, `config`, `dedup`, etc.) using readline features. Ensure that if a subcommand is typed, readline suggests only valid subcommands of that command group.

## Tests & Verification
1. Create a new test file `tests/unit/test_search_highlight_and_shell.py` that verifies:
   - Search highlighting in non-interactive print and interactive table views.
   - Browse highlighting of query tokens when `--query` option is passed, in both list view and read view.
   - Shell history loading/saving from/to `~/.awareness_history`.
   - Shell autocomplete suggestions for first-level and second-level commands.
2. Run the entire test suite using `.venv/bin/pytest` and verify that all 180+ tests pass successfully.

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Please execute the changes, run all tests, document the commands and output, and write a detailed `handoff.md` in your directory.

## 2026-06-05T23:53:37Z
**Context**: Checking on status of Milestone 3 & 4 implementation.
**Content**: Please report your progress on implementing the search & browse highlighting, shell autocomplete, history, and the new unit tests.
**Action**: Reply with your current status, any blockers, or the path to your handoff report if complete.

## 2026-06-05T23:56:10Z
**Context**: Checking progress of Worker M3_M4.
**Content**: Could you please provide an update on your progress implementing the search/browse highlighting and interactive shell features? Are you encountering any issues?
**Action**: Please reply with status or progress.
