# Plan - Awareness Terminal Improvements

## Goal
Implement terminal improvements for the Awareness engine in the copied repository `/Users/nazmi/awareness_dev` to ensure compatibility and pass all tests, and then copy the final files back to the target directory.

## Steps
1. **Milestone 1: Exploration & Baseline** (DONE)
   - Spawn Explorer agent to examine existing codebase, locate CLI, TUI, search, and shell files.
   - Run baseline tests to verify current codebase state.
   - Identify precise logic and locations needed for R1, R2, R3, R4.
2. **Milestone 2: TUI Enhancements (R1 & R2)** (DONE)
   - Add Live Capture Panel (10 most recent captures with HH:MM:SS Time, Title, Domain).
   - Add Job Management Controls (S/D/N, list navigation, arrow selection, start/stop/delete jobs).
   - Verify with new/modified tests.
3. **Milestone 3 & 4: Highlight Search & Browse (R3) & Shell Autocomplete & History (R4)** (IN_PROGRESS)
   - Highlight matching query tokens in search and browse terminal output (both interactive tables and non-interactive lists, as well as document read views) using rich formatting (bold yellow).
   - Support `~/.awareness_history` as the persistent history file for `awareness shell`.
   - Ensure complete tab completion for all subcommands via `readline`.
   - Verify with new/modified tests.
4. **Milestone 5: Validation & Handoff** (PLANNED)
   - Run final full test suite in copied repository.
   - Copy changed source files and tests to original repository.
   - Run test suite in original repository to confirm.
   - Write final handoff/completion report to Parent.

