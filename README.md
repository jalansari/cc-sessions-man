# cc-sessions-man

Lists every Claude Code session on this machine and says whether it is finished or waiting on you.

## What it reports

Each session lands in one group. Each group has a rank, and lower ranks sort first.
The gaps in the numbering leave room for new statuses, and Done stays last at 99.

| Rank | Group | Meaning | How it's decided |
|---|---|---|---|
| 0 | Running | A Claude process is still open (busy or idle) | Live process in `~/.claude/sessions/` |
| 1 | Needs your action | You must do something outside the session (edit a file, merge a PR, run sudo) | Haiku reads the final message |
| 2 | Needs your decision | Claude asked a question or wants approval | Haiku |
| 3 | Interrupted | Ended on your prompt with no reply, or partway through a tool call | Transcript structure |
| 4 | Offered extras | Finished, with optional follow-ups you can ignore | Haiku |
| 5 | Ended (not classified) | Not labelled yet (`--no-llm`, or the model skipped it) | — |
| 6 | Classification failed | The `claude -p` call for its batch failed (not logged in, rate limited, timed out). The error shows in the details panel and `--json` output. Never cached, so it's retried on refresh (`r`) or the next run | — |
| 99 | Done | Nothing left to do | Haiku |

Ranks are defined in `STATUS_RANK` in `src/cc_sessions_man/cli.py`.
The `--json` output includes each session's `status_rank`, `context_tokens` and `model`.

Only sessions that ended on a normal reply go to the model.
For each one, the model gets your last prompt and Claude's final message.
It returns a label and a short note on what is still outstanding.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.14 or later. uv downloads it if it isn't installed.
- The `claude` CLI on your `PATH`, logged in. Classification runs `claude -p --model haiku`.

## Run

```sh
cd /path/to/cc-sessions-man
uv run cc-sessions                 # all sessions, grouped
uv run cc-sessions --pending       # only decision / action / interrupted
uv run cc-sessions --days 7        # only the last week
uv run cc-sessions -n 20           # only the 20 most recent
uv run cc-sessions --json          # machine-readable, includes a resume command
```

To run it from anywhere, install it as a tool:

```sh
uv tool install --editable /path/to/cc-sessions-man
cc-sessions --pending
```

To resume a session, use the directory and full session ID it prints:

```sh
cd ~/code/my-app && claude --resume 3f2a9c1e-7b4d-4e8a-9c6f-1d2e3f4a5b6c
```

## Terminal UI

```sh
uv run cc-sessions-ui              # or: cc-sessions-ui, after `uv tool install`
uv run cc-sessions-ui --no-llm     # cached labels only, no model calls
uv run cc-sessions-ui --refresh-interval 60   # auto-refresh every minute; 0 turns it off
```

The UI is built with [Textual](https://textual.textualize.io/).
It shows a table of sessions with status, start time, last activity, title, folder, context size and model.

- **Context** is how full the session's context was at Claude's last reply: the input tokens it was given, including cached ones.
  It's shown as, for example, `134k` or `1.0M`, and is blank for sessions with no recorded usage.
  Its colour shows the band: blue below 250k, green from 250k, yellow from 500k, and red from 800k.
  Click the heading to sort largest first.
- **Model** is the model that wrote Claude's latest reply in the main conversation, shortened to, for example, `opus-5-5` or `haiku-4-5`.
  Subagents' models aren't shown. After a `/model` switch, it changes once Claude next replies. The details panel shows the full model ID.
  It's blank for sessions with no reply yet.
- Folders longer than 40 characters keep their end, for example `…/my-app/.claude/skills/changelog-writer`. The details panel shows the full path.

- The list appears as soon as the transcripts are scanned, using cached labels.
  Sessions with no cached label show as "Classifying…".
  They're sent to `claude -p` in batches of 5, 16 batches at a time, and each row updates as soon as its batch returns.
  The progress (for example `Classifying 15/35`) shows in the header.
  Change these with `--batch-size N` and `--jobs N`.
- **Auto refresh:** the list refreshes itself every 30 seconds, picking up new sessions, status changes and sessions that have finished.
  - New or changed sessions are classified as part of the refresh.
  - It runs quietly, without the "Scanning…" status, and keeps your selection, marks, filter, sort and scroll position.
    When nothing has changed, the table isn't redrawn at all.
  - A tick is skipped while a scan or classification is still running, and while a dialog, the command palette or another screen is open.
  - Deletes and renames you make while a refresh is scanning are kept; the refresh can't bring a deleted session back.
  - **After a classification error** (for example, logged out or rate limited), auto refresh pauses classification.
    It keeps rescanning but makes no model calls, and the title bar says "Classification paused after an error. Press r to retry."
    The error is shown once, not on every tick. Press `r` to retry; when a run completes without errors, auto refresh classifies again.
  - Set the interval with `--refresh-interval SECONDS`: at least 1, or `0` to turn auto refresh off.
- By default it sorts by status rank, with the newest last activity first within each status.
- `s` cycles through four sort orders (see the keys below).
- Click a column header to sort by that column alone. Click it again to reverse the order.
- The headings show the current sort: `^1` marks the primary sort column, `^2` the secondary and `^3` the tertiary.
  Status sorts by rank (needs you first), last activity newest first, and folder A to Z.
- The panel at the bottom shows the selected session's outstanding work, full ID, folder and full model ID.

| Key | Action |
|---|---|
| ↑ / ↓ | Move between sessions |
| Enter | Resume the session: the UI closes and runs `claude --resume` in the session's folder |
| `p` | Show only sessions that need you (decision, action, interrupted) |
| `a` | Show all sessions: resets the group drop-down to All and clears the search |
| `/` | Filter by title, folder or session ID. Enter or Esc returns to the table |
| `Space` | Mark or unmark the session for deletion, and move down a row |
| `u` | Unmark all. Does nothing when nothing is marked |
| `Shift+Space` | Unmark all; with nothing marked, it marks the current session, like `Space`. Only in terminals that report Shift with Space; many send a plain Space |
| `n` | Rename the selected session. Enter saves, Esc cancels |
| `d` | Delete the marked sessions, or the selected one if none are marked. Asks first |
| `s` | Cycle the sort order, one step per press, back to 1 after 4:<br>1. Status, then last activity<br>2. Last activity, then status<br>3. Folder, then status, then last activity<br>4. Last activity, then folder, then status<br>From a column-header sort, `s` starts again at 1 |
| `r` | Rescan and classify any new, changed or failed sessions. If a scan or classification is already running, it runs again straight after. A run without errors lifts a classification pause |
| `k` | Show or hide the keys panel, which lists every key and what it does |
| `q` | Quit |

Use the drop-down at the top left to show a single group.
A session in the Running group is open in another terminal, so it can't be resumed or deleted from here.

### Renaming sessions

Renaming saves the title the same way Claude Code's `/rename` does, so `claude --resume` shows the new name too:

- a `custom-title` line is appended to the transcript, and
- `<session-id>/custom-title.json` is written next to it.

The session's label is kept, so a rename doesn't trigger classification again.
A running session can't be renamed from here; use `/rename` inside it.

### Deleting sessions

Delete moves a session's files to the desktop Trash, so you can restore them from there.
For each session it moves:

- the transcript, `~/.claude/projects/<project>/<session-id>.jsonl`
- its folder of subagent transcripts and tool output, `~/.claude/projects/<project>/<session-id>/`
- its file backups for `/rewind`, `~/.claude/file-history/<session-id>/`
- `~/.claude/session-env/<session-id>/`

Your prompt history in `~/.claude/history.jsonl` is shared by all sessions, so it is left alone.
The confirmation dialog opens with Cancel selected, so pressing Enter by mistake deletes nothing.

## CLI options

| Option | Effect |
|---|---|
| `-n, --limit N` | Only the N most recent sessions |
| `--days N` | Only sessions active in the last N days |
| `-s, --show GROUP` | Only this group; repeatable. Groups: `running`, `decision`, `action`, `interrupted`, `optional`, `done`, `unclassified` |
| `--pending` | Same as `-s decision -s action -s interrupted` |
| `--no-llm` | Don't call the model; use cached labels only. Uncached sessions show as "Ended (not classified)" |
| `--refresh` | Classify the listed sessions again, ignoring their cached labels. With `-n` or `--days`, other sessions keep their labels |
| `--model M` | Model alias or ID (default `haiku`) |
| `--batch-size N` | Sessions per `claude` call (default 5) |
| `-j, --jobs N` | Parallel `claude` calls (default 16). They share your account's rate limits with other Claude sessions |
| `--claude-home DIR` | Claude config directory (default `~/.claude`, or `$CLAUDE_CONFIG_DIR`) |

## Using another Claude config directory

The tool reads the same directory Claude Code does:

- `$CLAUDE_CONFIG_DIR` if it's set and not empty. A leading `~` is expanded.
- `~/.claude` otherwise.

`--claude-home DIR` overrides both for one run.

When the directory isn't `~/.claude`, the tool passes it on to `claude`:

- Resuming from the UI runs `claude` with `CLAUDE_CONFIG_DIR` set to that directory, so `--resume` finds the session.
- The resume commands the CLI prints start with `CLAUDE_CONFIG_DIR=...`.

```sh
CLAUDE_CONFIG_DIR=~/.claude-work uv run cc-sessions-ui
uv run cc-sessions --claude-home ~/.claude-work --pending
```

## Cost and caching

Two caches live in `~/.cache/cc-sessions-man/` (or `$XDG_CACHE_HOME/cc-sessions-man/`):

- `scan-<hash>.json` holds a summary of each transcript, one file per Claude config directory: title, times, state, context size, model, and the last prompt and reply.
  A transcript is only read again when its size or modification time changes.
  This keeps startup fast; reading all transcripts in full takes seconds.
- `labels.json` holds the model's label for each session, keyed by session ID and the transcript's modification time.
  A session is only classified again after its transcript changes.

Both are safe to delete. The next run rebuilds them.
To watch classification run from scratch, delete `labels.json` and start the UI.

The first classification of all sessions costs a few cents. Later runs only send new or changed sessions.

## How the `claude` calls are isolated

Each classification call runs with:

- `--no-session-persistence`, so the calls don't appear as sessions in the list.
- `--tools ""` and `--strict-mcp-config`, so the model can only read the text it is given.
- `--setting-sources ""` and an empty temporary working directory, so no hooks, settings or project `CLAUDE.md` load.

Session text is sent to the Anthropic API.
That is the same place the sessions themselves ran.

## Limits

- Labels are a model's judgement of the final message.
  An occasional session will land in the wrong group.
- It can't tell whether you already dealt with a question elsewhere, for example by merging a PR by hand.
- It reads Claude Code's internal transcript format, which isn't a stable public API.
  A future Claude Code release could break parsing.

## Development

```sh
uv run pytest
```

### Benchmarking classification

`benchmarks/classify_batch_size.py` measures how batch size and the number of parallel calls affect classification speed, cost and accuracy:

```sh
uv run python benchmarks/classify_batch_size.py                    # 60 sessions; batch 1, 5, 10, 20, 40; 4 and 8 parallel
uv run python benchmarks/classify_batch_size.py --sample 40 --batch-sizes 5 20 --jobs 8
```

- It takes a fixed random sample of finished sessions. Use `--seed` to change the sample.
- It classifies the sample once per configuration, using the same `claude -p` call as the app.
- Accuracy is compared against a reference run with batch size 1. The reference is run twice, so the model's own run-to-run noise gives a baseline to compare against.
- "Agree (needs you)" checks only whether a session needs you (decision or action) or not. That is the distinction that matters most in the UI.
- It never writes to the app's caches.
  It prints a Markdown table and saves the numbers to `benchmarks/results/<timestamp>.json`, so you can compare runs over time.
- A full run takes a few minutes and costs well under a dollar.
