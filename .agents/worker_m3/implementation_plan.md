# Implementation Plan - Highlight Search & Browse (R3)

This plan details the changes required to satisfy the requirements for Milestone 3.

## Step 1: Implement helper function `highlight_tokens`
We will add `highlight_tokens(text: str, query: str) -> str` helper function to `src/awareness/cli/main.py` just before `@app.command(name="browse")`.
- It will escape rich tags in `text` via `escape(text or "")`.
- It will find alphanumeric and single quote tokens from `query.lower()` using regex `re.findall(r"[A-Za-z0-9']+", query.lower())`, filtering to those of length >= 2.
- It will compile a case-insensitive regex pattern using prefix boundaries (`\b(token1|token2)\w*`).
- It will substitute matches in the escaped text with `[bold yellow]{matched}[/bold yellow]`.
- If `query` is empty or there are no tokens of length >= 2, it will return the escaped text as-is.

## Step 2: Update `browse` CLI command signature and query logic
- We will add the `--query`/`-q` option to the `browse` command: `query: str = typer.Option("", "--query", "-q", help="Filter and highlight by query keyword")`.
- Inside the command, if `query` is provided, we will tokenize it with `re.findall(r"[A-Za-z0-9']+", query.lower())`.
- For each token of length >= 2, we will append a SQL `where` clause filter: `(title ILIKE $q_term_N OR text ILIKE $q_term_N)` and bind the query parameter `q_term_N` to `f"%{token}%"`.
- If there are no tokens of length >= 2 (but `query` is non-empty), fallback to using the whole query string as a single term.

## Step 3: Implement search highlighting in output functions
- **Search non-interactive list print**:
  Use `highlight_tokens` on `title` and `snippet`.
  Change print formatting so `title` is not entirely wrapped in `[bold yellow]`. Let's wrap it in `[bold]` or keep it normal, while the matching parts are highlighted.
- **Search interactive list table**:
  Use `highlight_tokens` on `title` and `snippet` before adding to the table row in `Title / Snippet` column.
- **Search interactive read view**:
  Replace existing highlighting block with `highlight_tokens(text_body, query)`.
- **Browse list table**:
  Use `highlight_tokens` on `title` using the `query` option.
- **Browse interactive read view**:
  Use `highlight_tokens` on `doc['title']` and `doc['text']`.

## Step 4: Run build and verify tests
- Run `.venv/bin/pytest` to ensure all existing tests pass.

## Step 5: Write unit/integration tests
- Add test cases targeting search highlighting, browse query filtering, and browse highlighting.
- Place tests in `tests/unit/test_cli_terminal.py` or create a new test file `tests/unit/test_cli_highlight.py`.
- Run pytest again to verify.
