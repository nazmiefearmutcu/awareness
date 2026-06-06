# Milestone 2 Code Review & Adversarial Challenge Report

## Review Summary

**Verdict**: APPROVE

---

## Findings

### [Minor] Input Parsing Vulnerability in TUI New Job Creation

- **What**: `to_utc(start_str)` and `coerce_relative_end(end_str)` are called directly without exception handling during interactive TUI inputs.
- **Where**: `src/awareness/cli/main.py:2300-2303`
- **Why**: If a user enters an invalid date format (e.g., typos or arbitrary text), the parsing functions raise `ValueError` or other exceptions, which will crash the interactive TUI.
- **Suggestion**: Wrap input parsing and validation in a try/except block, show a friendly warning message, and re-prompt the user or default to a safe fallback date/time.

---

## Verified Claims

- **Arrow keys and S, D, N controls in dashboard handle selections/actions** → verified via code inspection and running `tests/unit/test_tui_controls_and_cancellation.py` → **pass**
- **Live captures panel shows 10 recent captures with Time, Title, Domain** → verified via code inspection of `_make_tui_layout` → **pass**
- **Unit and integration tests pass successfully** → verified via running `.venv/bin/pytest` → **pass**
- **Delete job removes job row and tasks from database** → verified via unit test `test_delete_job` → **pass**

---

## Coverage Gaps

- **Compaction error handling** — risk level: low — recommendation: accept risk. (Compaction error returns standard rich color formatting status messages instead of throwing, which is correct).

---

## Unverified Items

- **GDELT firehose actual data emission on live tail** — reason not verified: Requires a live network connection and a 15-minute wait for GDELT slots, which is out of scope.

---
---

## Challenge Summary

**Overall risk assessment**: LOW

---

## Challenges

### [Medium] Infinite Loop in Tail Daemon on Job Deletion

- **Assumption challenged**: The job row is assumed to always exist in the database for a running tail.
- **Attack scenario**: If a user deletes a paused or cancelled tail job from the database using key `d` or other database CLI tools while a background `run_tail` loop is still running, the state DB `get_job(job_id)` returns `None`. The `run_tail` loop skips status cancellation check (`if js:`) and queries tasks, which returns an empty list, then sleeps and loops forever.
- **Blast radius**: Background tail daemon runs infinitely, consuming CPU/DB resources on useless polling.
- **Mitigation**: Add a check inside `run_tail` to break the loop if `js` (job state) is `None`, signaling the job no longer exists.

---

## Stress Test Results

- **Invalid/Malformatted Keyboard input sequence** → Escape sequence parser safely ignores unknown sequences → **pass**
- **Concurrent DB deletion during Job Selection** → `selected_job_idx` is clamped dynamically to `len(jobs) - 1` in layout rendering → **pass**

---

## Unchallenged Areas

- **Cloud/Google Drive credentials and catalog authentication** — reason not challenged: Network constraints (CODE_ONLY) prevent checking live external cloud resources.
