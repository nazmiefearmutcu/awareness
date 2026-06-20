# Project: Awareness Terminal Improvements

## Architecture
- **TUI (Terminal User Interface)**: Implemented using `rich` / `textual` or similar UI libraries. Located under `src/awareness/cli/terminal.py` or similar TUI module.
- **Search & Browse Commands**: Part of the CLI logic under `src/awareness/cli/`. Prints documents from DuckDB query results.
- **Interactive Shell**: The CLI shell command handler.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Baseline & Exploration | Run baseline tests, locate TUI, search, and shell files, define detailed design recommendations. | None | DONE |
| 2 | TUI Enhancements (R1, R2) | Implement TUI Live Capture Panel showing 10 most recent captures and Job Management (S, D, N controls) with arrow key selection. | M1 | DONE |
| 3 | Highlight Search & Browse (R3) | Implement bold yellow formatting for matching search query tokens in CLI search and browse outputs. | M1 | DONE |
| 4 | Shell Autocomplete & History (R4) | Implement subcommand tab-completion and history file loading/saving in `awareness shell`. | M1 | DONE |
| 5 | Validation & Handoff | Verify all tests pass, verify layout/integrity, write handoff, copy files back to original repo. | M2, M3, M4 | DONE |

## Interface Contracts
- **TUI Live Capture**: Fetch from `captures` view in DuckDB via the existing database layer.
- **TUI Job Management**: Trigger jobs and stop jobs using the existing worker/tail interfaces or APIs.
- **Highlighter**: Add rich styling tags to text formatting logic.
- **History File**: Use `~/.awareness_history`.
- **Autocomplete**: Standard `readline` autocompletion for shell command loop.

## Code Layout
- `src/awareness/cli/`: Main CLI entry points and command definitions.
- `src/awareness/cli/terminal.py`: Terminal/TUI code.
- `src/awareness/cli/shell.py`: Interactive shell code.
- `src/awareness/storage/`: Database connection / query logic.
- `tests/`: Project tests.
