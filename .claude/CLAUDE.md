# cc-sessions-man

A uv project (Python 3.14) with two entry points: the `cc-sessions` CLI (`cli.py`) and the
`cc-sessions-ui` Textual app (`ui.py`). They list Claude Code sessions and classify whether
each is done or waiting on the user. See `README.md` for behaviour and `features.md` for the
requirements.

## Process for a feature or fix

1. Agree the change, and settle open questions first (for example, keys the terminal or
   desktop may take).
2. Write the code in the style of the surrounding code: its naming, comment density and idiom.
3. Add tests for the new behaviour.
4. Run the static checks and the full test suite:
   `uv run black .`, `uv run isort .`, `uv run flake8 src tests benchmarks`, `uv run pytest -q`.
5. For UI changes, check the result by eye (Textual's `app.save_screenshot()` in a
   `run_test` session gives an SVG).
6. Update the docs to match the code:
   - `README.md`: user-facing behaviour, including the keys table.
   - `features.md`: the requirement, its edge cases, and decisions taken (including
     approaches tried and rejected, and why).
7. Check the change for anything machine-specific or private: real paths, user names, session
   IDs, or company and project names. Use generic examples only.
8. Commit when the feature is built, checked and documented: one change per commit, a short
   imperative subject, and a short body saying what and why. Once the project is published,
   work moves to branches and pull requests.

## Tools and style

- Line length 88 (black, isort with the black profile, flake8 with E203 ignored).
- Never change a string's value to fit the line length: split it into adjacent literals, or
  use a backslash continuation inside triple-quoted prompts.
- A PostToolUse hook (`.claude/hooks/check.sh`) formats each edited `.py` file, runs flake8 on
  it, and runs the tests. Its failures come back as feedback to fix.

## Tests

- `tests/conftest.py`: autouse fixture that keeps tests away from the real caches, and the
  `trashed` fixture that records Trash moves instead of making them.
- `tests/test_delete.py` holds shared helpers: `make_home` builds a fake Claude home with
  sessions, and `run_ui` runs the app with `run_test` and awaits a `steps(app, pilot)`
  coroutine. Import them from there rather than copying them.
- Test behaviour through the app (key presses, rendered tables) rather than internals.

## Textual notes

- Key events name modifiers in alphabetical order (`alt+ctrl+t`, `ctrl+shift+r`), and
  bindings must use the same spelling.
- Terminals without the kitty keyboard protocol send Ctrl+Shift+letter and Ctrl+Alt+letter
  as plain Ctrl+letter, and the desktop or terminal often takes those chords anyway. Prefer
  plain Ctrl+letter or single keys.
- Bindings with `show=False` stay out of the footer but are still listed in the keys panel
  (`k`).
- `DataTable` cells and headings should be `Text`, never `str`: a `str` is read as Rich markup.
- Avoid overriding private Textual methods: prefer accepting a small limitation.
