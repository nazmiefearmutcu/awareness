## 2026-06-06T00:00:00Z
Goal
Implement Milestone 3: Highlight Search & Browse (R3) in the development repository /Users/nazmi/awareness_dev.

Requirements
1. Update `browse` command signature in `src/awareness/cli/main.py`:
   - Add a new option: `query: str = typer.Option("", "--query", "-q", help="Filter and highlight by query keyword")`
   - If `query` is provided, append a filter to the SQL `where` clause to match either the `title` or `text` fields using the query. You can do this by adding `(title ILIKE $q_term OR text ILIKE $q_term)` for the query or each token in the query.
2. Implement robust term highlighting in `src/awareness/cli/main.py`:
   - Define a helper function `highlight_tokens(text: str, query: str) -> str` that:
     - Escapes the text using `from rich.markup import escape`.
     - Finds tokens in the query using `re.findall(r"[A-Za-z0-9']+", query.lower())` (minimum 2 chars).
     - Replaces matching tokens in the escaped text with `[bold yellow]token[/bold yellow]`. To be robust and match the search behavior, you can match case-insensitively and use word/prefix boundaries. E.g., `re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\w*", re.IGNORECASE)` to highlight the whole word starting with the term, or simply match the exact tokens with word boundaries `\b(...)` similar to the existing read view.
   - Apply `highlight_tokens` in:
     - `search` command non-interactive list print: Highlight the matching tokens in the title and the snippet before printing (ensure the title is NOT entirely bold yellow so highlights stand out).
     - `search` command interactive list table: Highlight matching tokens in the title and the snippet in the `Title / Snippet` column.
     - `search` command interactive read view: Ensure full text body highlighting remains robust.
     - `browse` command list table: Highlight matching tokens in the `Title` column.
     - `browse` command interactive read view: Highlight matching tokens in the title and the body text.
3. Verify all tests pass: `.venv/bin/pytest`
4. Add new unit/integration tests to verify that search and browse output highlighting works correctly, and that the browse query filter functions as expected.
