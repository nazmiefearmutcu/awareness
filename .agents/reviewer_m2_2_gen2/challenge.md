## Challenge Summary

**Overall risk assessment**: LOW

The Milestone 2 implementation exhibits solid defensive design features (e.g., clamping selected index boundaries, try-except blocks for database queries). However, there are a few edge cases where the system assumptions can be stressed.

---

## Challenges

### [Low] Challenge 1: Subprocess Resource Exhaustion
- **Assumption challenged**: Spawning new processes via `subprocess.Popen` is always successful.
- **Attack scenario**: High system load, process descriptor leaks, or restricted container permissions prevent the creation of new Python processes when the user creates a new job from the TUI.
- **Blast radius**: The TUI crashes due to an unhandled `OSError` exception.
- **Mitigation**: Wrap `subprocess.Popen` in a `try...except OSError` block, catch the exception, and display a user-friendly error in `status_msg`.

### [Low] Challenge 2: Non-TTY standard input redirection
- **Assumption challenged**: The TUI is always run interactively in a TTY.
- **Attack scenario**: A user runs the TUI inside a piped script or cron job without standard input attached.
- **Blast radius**: The interactive loop still runs, polling CPU, but `_get_key_nonblocking` returns `None` continuously.
- **Mitigation**: Add a check at startup: `if not sys.stdin.isatty(): raise typer.Exit("TUI requires an interactive TTY standard input.")`.

---

## Stress Test Results

- **DuckDB corruption/locking** → Query fails but panel displays empty captures instead of crashing → Expected behavior: empty captures list → Predicted behavior: empty captures list → **PASS**
- **Job deletion/creation race** → Attempt to delete a job that has just been marked running in the background → Expected behavior: TUI refuses to delete with error "Cannot delete running job" → Predicted behavior: block deletion → **PASS**

---

## Unchallenged Areas

- **Physical rendering and display refresh rate performance under massive terminal sizes** — reason not challenged: limited by non-interactive review environment.
