# Detailed Analysis Report: CLI Highlighting and Shell Enhancements

This report documents findings from the read-only investigation of the `awareness` CLI highlighting and shell REPL implementations, analyzing test failures, and proposing a strategy and verification test cases.

---

## 1. Why `tests/unit/test_cli_highlight.py` Fails

In the unmodified **clean HEAD branch**, running the test suite causes failures due to the following reasons:
1. **Missing `highlight_tokens`**: The function `highlight_tokens` was not defined or exported in `src/awareness/cli/main.py`. Consequently, line 8 in `tests/unit/test_cli_highlight.py` failed with an `ImportError`:
   ```python
   from awareness.cli.main import app, highlight_tokens
   ```
2. **Missing `browse` Highlighting**: The browse command had no query token highlighting implemented. In HEAD, `browse` did not accept a `--query` option or perform any highlight rendering for document text or titles.
3. **Invalid Word Boundary Regex for Prefix Matching**: The original search highlight logic inside the search command used a double word-boundary regex (`\b...\b`):
   ```python
   pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
   ```
   This strictly required the query to match entire words, failing the prefix-boundary requirement (R3) where searching for a prefix (e.g., `financ`) must highlight words starting with it (e.g., `financial`).

The working directory currently contains uncommitted modifications to `src/awareness/cli/main.py` that implement `highlight_tokens`, apply prefix-boundary matching (`\b(terms)\w*`), and integrate it into `search` and `browse`. These changes successfully resolve the issues and cause `tests/unit/test_cli_highlight.py` to pass.

---

## 2. Analysis of Query Token Highlighting (`search` and `browse`)

### `highlight_tokens` & `highlight_query` Implementation
The current uncommitted implementation in the workspace uses:
```python
def highlight_query(text: str, query: str) -> str:
    """Escapes rich tags in text and highlights query tokens case-insensitively using prefix boundaries."""
    escaped_text = escape(text or "")
    if not query:
        return escaped_text
    terms = [t for t in re.findall(r"[A-Za-z0-9']+", query.lower()) if len(t) >= 2]
    if not terms:
        return escaped_text
    terms.sort(key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\w*", re.IGNORECASE)
    return pattern.sub(lambda m: f"[bold yellow]{m.group(0)}[/bold yellow]", escaped_text)

def highlight_tokens(text: str, query: str) -> str:
    return highlight_query(text, query)
```

- **Tag Escaping**: Escaping via `rich.markup.escape` is performed first (line 2385) to prevent Rich from misinterpreting any pre-existing brackets in the raw capture text (e.g., `[awesome]` becomes `\[awesome]`).
- **Term Extraction & Length Filtering**: Alphanumeric tokens are extracted with `re.findall(r"[A-Za-z0-9']+")` and filtered to retain only those with length $\ge 2$ (line 2388).
- **Nested Matching Prevention**: Sorting the terms by length descending (`terms.sort(key=len, reverse=True)`) ensures that longer terms are matched and replaced first, preventing shorter substrings from nesting highlighting tags inside them.
- **Prefix Boundary Matching**: The pattern `\b(term)\w*` uses a word-boundary prefix `\b` but a trailing `\w*` instead of `\b`. This allows it to match and highlight the entire word if it starts with the query token (e.g., query `financ` highlights the whole word `financial`).

### CLI Integration: `search`
- **Non-Interactive Mode**: Integrated at lines 2605-2610. The command iterates over search results and highlights matching tokens in both the titles and snippets using `highlight_tokens(title, query)` and `highlight_tokens(snippet, query)`.
- **Interactive Mode**:
  - Highlights titles in the main results table: lines 2657-2663.
  - Highlights titles and text bodies inside the document read view: lines 2697 and 2706.

### CLI Integration: `browse`
- **Query Filter**: Browse accepts a `--query` option (line 2406) which parses query terms and filters DuckDB rows using a SQL `ILIKE` clause (lines 2439-2447).
- **Highlights**:
  - Highlights titles in the browse table: line 2488.
  - Highlights titles and body text inside the document read view: lines 2519 and 2528.

---

## 3. Analysis of Interactive Shell Autocomplete and History

### Autocomplete Implementation
The shell autocomplete function is `completer` nested inside `_setup_shell_readline` (lines 3868-3910).
- **Readline Setup**: Registers `completer` as the completion function and binds the Tab key (`tab: complete` or `bind ^I rl_complete` depending on `libedit` or GNU readline presence on macOS).
- **Delimiter Overriding**: By default, `readline.set_completer_delims(" \t\n")` is called to avoid splitting on characters like `-` or `/` which are valid parts of subcommands (e.g. `auth-gdrive`).
- **Command Parsing**:
  - Extracts the text before the cursor (`prefix = buffer[:begidx]`).
  - Tokenizes it using `shlex.split` (falling back to `.split()` on syntax error).
  - Handles leading slashes (e.g., `/search` becomes `search`) and `help` / `?` prefixes.
- **Tree Traversal**: Starting at the click root (`click_cmd`), it iterates over the words to traverse subcommand groups recursively:
  ```python
  current_group = click_cmd
  for word in words:
      if current_group and hasattr(current_group, "commands") and word in current_group.commands:
          current_group = current_group.commands[word]
      else:
          current_group = None
          break
  ```
- **Option Pool**: If the active command is a group (has `commands`), the autocomplete pool consists of its subcommand names (and `_REPL_META` for the root group). Otherwise, it is empty.

### History Implementation
- **History File**: Defined as `~/.awareness_history` (lines 3981-3984) with a fallback to `data_dir / "state" / "shell_history"` if home expansion fails.
- **Loading**: `readline.read_history_file(str(history_file))` is called if the file exists on startup (lines 3920-3921).
- **Saving**: `readline.write_history_file(str(history_file))` is invoked at the end of every command loop iteration (lines 4069-4075) to prevent history loss in case of a crash or exit.

---

## 4. Strategy to Fix Failures and Satisfy R3 and R4

The current uncommitted implementation in the workspace is highly functional, but we can make it more robust. Here is the suggested strategy:

### A. Fix Top-Level Slash-Prefixed Autocomplete
Currently, typing `/c` and pressing Tab does not autocomplete because the word-under-cursor `text` starts with `/` (e.g. `text="/c"`), whereas the command pool contains `"config"`, `"cloud"`, etc. None start with `/`.
- **Solution**: Modify `completer` to strip the leading slash from the search prefix, match against the command pool, and prepend it back to the returned options:
  ```python
  has_slash = text.startswith("/")
  search_text = text[1:] if has_slash else text
  
  options = [
      ("/" + c if has_slash else c)
      for c in sorted(pool)
      if c.startswith(search_text)
  ]
  ```

### B. Polish HTML/Rich Tag Escaping inside Bracket Highlights
Rich markup allows escaping `[` to `\[`. To guarantee that text containing multiple brackets does not interfere with the tags generated by `highlight_tokens`, ensure that the raw text is always fully escaped via `rich.markup.escape` before search terms are substituted. The current implementation does this correctly, but the tests should verify this behavior with complex bracket nests.

---

## 5. Suggested Test Cases for History and Autocomplete

To verify interactive shell history and autocomplete (which are hard to test manually in a TUI), the following automated tests using pytest and standard library mocks are recommended:

```python
import sys
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest
from awareness.cli.main import _setup_shell_readline

@pytest.fixture
def mock_readline(monkeypatch):
    mock_rl = MagicMock()
    # Mock doc to determine libedit presence
    mock_rl.__doc__ = "GNU Readline support"
    monkeypatch.setitem(sys.modules, "readline", mock_rl)
    return mock_rl

def test_setup_shell_readline_loads_history(mock_readline, tmp_path):
    history_file = tmp_path / "test_history"
    history_file.write_text("status\nsearch python\n", encoding="utf-8")
    
    click_cmd = MagicMock()
    click_cmd.commands = {}
    
    assert _setup_shell_readline(click_cmd, history_file) is True
    
    mock_readline.read_history_file.assert_called_once_with(str(history_file))
    mock_readline.set_history_length.assert_called_once_with(2000)

def test_autocomplete_toplevel(mock_readline):
    click_cmd = MagicMock()
    # Mock commands structure for Typer / Click app
    cmd1 = MagicMock()
    cmd2 = MagicMock()
    click_cmd.commands = {"start": cmd1, "status": cmd2, "cloud": MagicMock()}
    
    _setup_shell_readline(click_cmd, None)
    
    completer_fn = mock_readline.set_completer.call_args[0][0]
    
    # Mock input line state: cursor at 'st'
    mock_readline.get_line_buffer.return_value = "st"
    mock_readline.get_begidx.return_value = 0
    
    # State 0 and 1 should return matching candidates in sorted order
    assert completer_fn("st", 0) == "start"
    assert completer_fn("st", 1) == "status"
    assert completer_fn("st", 2) is None

def test_autocomplete_subcommand(mock_readline):
    click_cmd = MagicMock()
    cloud_group = MagicMock()
    cloud_group.commands = {"status": MagicMock(), "auth-gdrive": MagicMock()}
    click_cmd.commands = {"cloud": cloud_group}
    
    _setup_shell_readline(click_cmd, None)
    completer_fn = mock_readline.set_completer.call_args[0][0]
    
    # Mock input line state: cursor at 'cloud st'
    mock_readline.get_line_buffer.return_value = "cloud st"
    mock_readline.get_begidx.return_value = 6
    
    assert completer_fn("st", 0) == "status"
    assert completer_fn("st", 1) is None

def test_autocomplete_with_slash_prefix(mock_readline):
    click_cmd = MagicMock()
    click_cmd.commands = {"search": MagicMock(), "status": MagicMock()}
    
    _setup_shell_readline(click_cmd, None)
    completer_fn = mock_readline.set_completer.call_args[0][0]
    
    # Mock input line: cursor at '/se'
    mock_readline.get_line_buffer.return_value = "/se"
    mock_readline.get_begidx.return_value = 0
    
    # Verify that the leading slash is preserved and matched
    assert completer_fn("/se", 0) == "/search"
```
