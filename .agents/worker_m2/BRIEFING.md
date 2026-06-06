# BRIEFING — 2026-06-06T00:54:40+03:00

## Mission
Implement Milestone 2: TUI Live Capture Panel (R1) and TUI Job Management Controls (R2) in the development repository /Users/nazmi/awareness_dev.

## 🔒 My Identity
- Archetype: teamwork_preview_worker
- Roles: implementer, qa, specialist
- Working directory: /Users/nazmi/awareness_dev/.agents/worker_m2
- Original parent: a32f5a76-9405-469d-a732-0fc94b096750
- Milestone: Milestone 2: TUI Live Capture & Job Management

## 🔒 Key Constraints
- CODE_ONLY network mode: No external HTTP calls, no external websites/services.
- Write only to own agent folder `/Users/nazmi/awareness_dev/.agents/worker_m2`.
- DO NOT CHEAT. No hardcoding or dummy implementations.

## Current Parent
- Conversation ID: a32f5a76-9405-469d-a732-0fc94b096750
- Updated: not yet

## Task Summary
- **What to build**: Modify `_make_tui_layout` to split right layout, query captures from DuckDB index, implement select job with Up/Down arrow keys, support S (Stop/Cancel), D (Delete/Clear), and N (New Job) commands in TUI, check status in workers, and implement `delete_job` in `StateDB`.
- **Success criteria**: Functional TUI panels, interactive job controls, non-interactive tail and backfill spawning, cooperative job cancellation checking in worker engines, and passing tests.
- **Interface contracts**: PROJECT.md (if exists)
- **Code layout**: src/awareness/

## Key Decisions Made
- None yet.

## Artifact Index
- `/Users/nazmi/awareness_dev/.agents/worker_m2/original_prompt.md` — Original prompt copy.

## Change Tracker
- **Files modified**: None yet.
- **Build status**: Unknown.
- **Pending issues**: None.

## Quality Status
- **Build/test result**: Unknown.
- **Lint status**: Unknown.
- **Tests added/modified**: None.

## Loaded Skills
- None loaded.
