# cc-sessions-man: features

What the tool does, written as requirements, grouped by area.
Each feature lists what it must do. "UI" means the `cc-sessions-ui` terminal app; "CLI" means the `cc-sessions` command.

- [cc-sessions-man: features](#cc-sessions-man-features)
- [Session discovery](#session-discovery)
  - [Finding sessions](#finding-sessions)
  - [Context size](#context-size)
  - [Model](#model)
  - [Session states from the transcript](#session-states-from-the-transcript)
  - [Running sessions](#running-sessions)
  - [Malformed input](#malformed-input)
- [Classification](#classification)
  - [Categories](#categories)
  - [AI batch classification](#ai-batch-classification)
  - [Streaming results (UI)](#streaming-results-ui)
  - [Failure handling](#failure-handling)
- [Caching](#caching)
  - [Label cache](#label-cache)
  - [Scan cache](#scan-cache)
  - [Refreshing](#refreshing)
- [List operations](#list-operations)
  - [Sorting](#sorting)
  - [Filtering by status](#filtering-by-status)
  - [Searching](#searching)
  - [AI search](#ai-search)
  - [Marking (selection)](#marking-selection)
  - [Stable cursor](#stable-cursor)
- [Managing sessions](#managing-sessions)
  - [Opening (resuming) a session](#opening-resuming-a-session)
  - [Renaming](#renaming)
  - [Moving](#moving)
  - [Deleting](#deleting)
- [Command-line interface (`cc-sessions`)](#command-line-interface-cc-sessions)
- [Configuration](#configuration)
  - [Claude config folder](#claude-config-folder)
  - [Requirements](#requirements)
- [UI experience](#ui-experience)
  - [Layout](#layout)
  - [Path display](#path-display)
  - [Keys help](#keys-help)
  - [Command palette and theming](#command-palette-and-theming)
  - [Notifications](#notifications)
- [Performance and benchmarking](#performance-and-benchmarking)
- [Reliability and safety](#reliability-and-safety)
- [Development and quality](#development-and-quality)


---

# Session discovery

## Finding sessions

- **Source:** every main-thread transcript at `<claude home>/projects/*/<session-id>.jsonl`.
- **Skipped:** subagent transcripts (those in subfolders).
- **Recorded** for each session:
  - session ID
  - title
  - working folder
  - start time: first message
  - last activity: last message
  - last prompt: the user's
  - last reply: Claude's
  - context size: the input tokens (fresh, cache-written and cache-read) of Claude's latest main-thread reply; subagent replies and output tokens don't count
  - model: the model ID of Claude's latest main-thread reply
- **Title:** the first of these that exists:
  1. the latest `/rename`
  2. Claude's AI-generated title
  3. the summary
  4. the first line of the last prompt
- **Ignored** when deciding how a session ended:
  - slash-command echoes
  - meta entries
  - sidechain entries
  - thinking-only chunks

## Context size

- **Value:** how full the session's context was at Claude's latest main-thread reply. It's the sum of that reply's:
  - `input_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
- **Excluded:** subagent (sidechain) replies and output tokens.
- **Non-integer usage values:** ignored.
- **No recorded usage:** 0, shown blank.
- **Display:** a right-aligned Context column after Folder, as `0.8k`, `134k` or `1.0M`, coloured by band:

  | Band | Colour |
  |---|---|
  | 0 to under 250k | Blue |
  | 250k to under 500k | Green |
  | 500k to under 800k | Yellow |
  | 800k and above | Red (bold) |

  The bands are set in `CONTEXT_BANDS` in `ui.py`.
- **Sorting:** clicking the Context heading sorts largest first; a second click reverses it.
- **Width:** 9 characters, set by the heading plus its mark (`Context v`), so a sort mark never resizes it.
- **Heading:** right-aligned like its values (`RIGHT_ALIGNED` in `ui.py`).
- **CLI:** `--json` output includes it as `context_tokens`.
- **Not shown:** total tokens and cost per session. Only newer Claude Code versions record them (`cost-state` entries).

## Model

- **Value:** the model ID (`message.model`) of Claude's latest main-thread reply.
  - After a `/model` switch, the latest reply wins.
  - A `/model` switch shows once Claude next replies.
- **Skipped:** subagent (sidechain) replies and Claude Code's `<synthetic>` placeholder replies.
- **Entries whose message isn't an object:** ignored, as for every other field.
- **No reply yet:** "" (shown blank).
- **Display:** a Model column at the end of the table, after Context.
  - Drops the `claude-` prefix (and any provider prefix before it), a date suffix and a Bedrock version suffix.
  - `claude-haiku-4-5-20251001`: shows as `haiku-4-5`
  - `us.anthropic.claude-sonnet-4-5-20250929-v1:0`: shows as `sonnet-4-5`
  - `claude-sonnet-4-5@20250929`: shows as `sonnet-4-5`
  - Other IDs: capped at 20 characters (`MODEL_MAX`).
- **Details panel:** shows the full model ID.
- **Sorting:** clicking the Model heading sorts A to Z, with blank models last; a second click reverses it.
- **CLI:** `--json` output includes the full ID as `model`.

## Session states from the transcript

Decide these states from the transcript's structure alone, without a model:

| State | Rule |
|---|---|
| Interrupted, no reply | The last real entry is the user's prompt |
| Interrupted, mid task | The last entry is a tool call or a tool result |
| Ended | The last entry is a text reply from Claude; the model classifies it |

## Running sessions

- **Running:** `<claude home>/sessions/<pid>.json` names the session and that process is alive.
- **Process owned by another user:** counts as alive (the liveness check returns "permission denied").
- **Reused PIDs:** the session isn't running if the start time recorded in the session file (`procStart`) differs from the live process's start time (field 22 of `/proc/<pid>/stat`).
  - Without `/proc`: fall back to the PID check alone.
- **Malformed session files:** ignored:
  - JSON that isn't an object
  - a non-integer PID
  - a missing session ID
- **Folder and status** from a live session file: only string values count, so a `null` folder can't replace the transcript's folder.

## Malformed input

- Skip transcript lines that aren't JSON objects, and entries whose `message` isn't an object.
- **Non-string fields** (title, folder, timestamp, text): treated as empty.
- **A transcript that still can't be parsed:**
  - is left out of the list;
  - never breaks the scan;
  - is retried once the file changes.
- **A transcript deleted mid-scan:** skipped.

---

# Classification

## Categories

Each session gets exactly one status.

- **Rank:** a number that sets the status order, with gaps left for new ones.
- **Done:** always last.

| Rank | Status | Meaning | Decided by |
|---|---|---|---|
| 0 | Running | A Claude process for the session is open | Live process check |
| 1 | Needs action | You must do something outside the session: edit a file, merge a PR, run sudo, log in | Model |
| 2 | Needs decision | Claude asked a question or is waiting for approval or a choice | Model |
| 3 | Interrupted | Ended on your prompt with no reply, or partway through a tool call | Transcript structure |
| 4 | Offered extras | Finished, with optional follow-ups that are safe to ignore | Model |
| 5 | Ended (not classified) | Not labelled yet (`--no-llm`, or the model skipped it) | - |
| 6 | Classification failed | The model call for its batch failed | Call result |
| 99 | Done | Nothing left to do | Model |

- **Ranks and labels:** defined in one place, `STATUS_RANK` and `GROUPS` in `cli.py`.
- **Startup check:** the app refuses to start if the two disagree.
- **Outstanding-work note:** the model writes one for each session in a needs-you status.
  - Length: at most 15 words.
  - Content: what's outstanding.
  - Shown in: the details panel and `--json`.

## AI batch classification

- **Scope:** only sessions in the Ended state with no valid cached label.
- **Model:** Haiku, through headless `claude -p`.
- **Batch size:** several sessions per call (default 5).
- **Jobs:** several calls in parallel (default 16).
- **Sent** for each session:
  - the user's last prompt: first 400 characters
  - Claude's final message: last 1,500 characters
- **Output:** structured JSON (`--json-schema`) with:
  - an id
  - a label from the fixed set
  - the outstanding-work note
- **Session text:** data to classify, never instructions.
- **Isolation** of each call:
  - not saved as a session (`--no-session-persistence`), so it never shows up in the list;
  - no tools and no MCP servers;
  - no settings or hooks loaded;
  - run from an empty temporary folder, so no project `CLAUDE.md` is loaded;
  - timed out after 300 seconds.
- **Overrides:** `--model`, `--batch-size` and `--jobs` replace the defaults.
  - Batch size and jobs: 1 or more.

## Streaming results (UI)

- **Scan finished:** show the full list at once.
- **Rows waiting for the model:** show "Classifying…".
- **Each batch returned:** update its rows, in the order batches complete.
- **Progress:** in the title bar, for example `Classifying 15/35 with haiku…`.

## Failure handling

- **Failed batch** (not logged in, rate limited, timed out): each of its sessions gets the "Classification failed" status, with the first line of the error as the detail.
- **UI notification:** one per run, explaining the failure and that `r` retries.
- **Failures:** never cached, so the next refresh or launch retries them.
- **Deleted mid-batch:** no label is cached for a session whose transcript was deleted while its batch was being classified.

---

# Caching

## Label cache

- **File:** `~/.cache/cc-sessions-man/labels.json` (or under `$XDG_CACHE_HOME`).
- **Key:** session ID plus the transcript's modification time, so a label stops matching as soon as its transcript changes.
- **Saved:** after every batch, so quitting partway keeps everything classified so far.
- **Merging:** saving merges with what's on disk, so labels saved meanwhile by another process (the CLI, a second UI) survive.
  - When both sides have a label for a session, keep the one for the newer transcript.
- **Deleted sessions:** their labels never come back.
- **Safety:**
  - use a file lock around each read-merge-write, so processes can't interleave;
  - write through a unique temporary file, then rename it into place;
  - take the in-memory lock only for the merge itself, never during file reads or writes, so the UI doesn't wait on disk.
- **Missing, unreadable or wrongly shaped file** (not a JSON object): treated as empty.

## Scan cache

- **File:** `~/.cache/cc-sessions-man/scan-<hash>.json`, holding a summary of each transcript.
  - One file per Claude config folder.
  - `<hash>`: of the folder's resolved path.
- **Re-read:** a transcript only when its size or modification time changes.
- **Speed:**
  - cached startup: about 0.2-0.3 s
  - full read of about 1.2 GB of transcripts: about 2.5 s
- **Running state:** never cached; checked fresh on every scan.
- **Kept text:**
  - last prompt: at most the first 2,000 characters
  - last reply: at most the last 4,000 characters
- **Writes:** through a unique temporary file.
- **Missing, stale-version or malformed file:** ignored.
- **Malformed entries:** dropped, so those transcripts are simply read again.
- **Format changes:** require bumping `SCAN_CACHE_VERSION` (now 3, since the model was added).

## Refreshing

- **UI, `r`:** rescan, merge in labels saved elsewhere, and classify new, changed or failed sessions.
  - **One load at a time**, because a running `claude` call can't be cancelled.
  - **During a load:** `r` queues another load to run straight after, rather than refusing.
  - **Classification:** `r` always attempts it.
  - **Clean run:** a run that completes without errors lifts a classification pause.
- **CLI, `--refresh`:** classify every listed finished session again.
  - Old labels stay until new ones arrive, so a failed refresh loses nothing.
  - Sessions outside `-n` / `--days` are never touched.
- **UI, automatic:** every 30 seconds by default, do the same as `r`, quietly (no "Scanning…" status).
  - **Skipped tick:** while a scan or classification is running, or while a dialog, the command palette or another screen is open.
  - **Kept:** the selection, marks, filter, sort, and horizontal and vertical scroll.
  - **No redraw:** when nothing changed, the table isn't redrawn.
  - **Re-applied:** deletes, renames and moves made while the refresh was scanning, so deleted sessions don't come back and new titles and folders don't revert.
  - **Marks:** kept while a session is briefly missing from a scan (e.g. mid-rewrite); dropped only when the transcript is gone.
- **Pause after errors** (automatic refresh): when a run hits a classification error, stop calling the model on automatic refreshes until a manual `r` run completes without errors.
  - Rescanning continues while paused.
  - Failed sessions keep their "Classification failed" status.
  - New sessions wait unclassified.
  - The title bar says classification is paused.
  - The error notification shows once per failing run, never on every tick.
- **`--refresh-interval SECONDS`:** sets the automatic interval.
  - Allowed: 0 (off) or at least 1.
  - Rejected: fractions below 1, negative values, infinity and NaN.
- **Both caches:** safe to delete.
  - Deleting `labels.json` forces a full re-classification, so you can watch a cold start.

---

# List operations

## Sorting

`s` cycles through four orders, going back to 1 after 4:

1. Status, then last activity
2. Last activity, then status
3. Folder, then status, then last activity
4. Last activity, then folder, then status

- **Direction:**
  - Status: by rank (needs you first)
  - Last activity: newest first
  - Folder: A to Z
- **Headings:** column headings mark the current sort with `^` (ascending) or `v` (descending).
  - Status by rank counts as ascending; newest-first dates and largest-first context count as descending.
  - With several sort columns, the marks are numbered by precedence (primary, secondary, tertiary), for example `Status ^1`, `Last activity v2`.
  - ASCII marks, because arrows such as `↑` `↓` have an ambiguous width in some terminals.
  - The marks and their numbers are bold blue, so they stand apart from the column names.
    - While the mouse is over the headings, Textual's hover colour turns them white. Keeping the blue would mean overriding a private Textual method, so this is accepted.
- **Notice:** each press names the order, for example "Sort 3 of 4: folder, then status, then last activity".
- **Header clicks:** clicking a heading sorts by that column alone, marked without a number (`Started v`).
  - A second click reverses it (`Started ^`).
  - Dates and context size sort largest or newest first; text columns sort A to Z.
  - Pressing `s` afterwards starts again at order 1.
- **Default:** order 1.

## Filtering by status

- **Drop-down:** All, "Needs you" (decision, action and interrupted), or any single status.
- **`p`:** shows only sessions that need you.
- **`a`:** shows everything: resets the drop-down to All and clears the search.

## Searching

- **`/`:** focuses the search box.
- **Typing:** filters by title, folder or session ID (case-insensitive).
- **Enter or Esc:** returns to the table.
- **While the search box has focus:** letters go into it, not to shortcuts.

## AI search

**Dialog** (`f`): asks a question about the transcripts' contents, such as "first session where `/advisor` was used" or "session that created github repository MY-EXAMPLE-REPO".

- **Filled with:** the last query.
- **Placeholder** when empty: "session that created github repository MY-EXAMPLE-REPO".
- **Enter:** searches.
- **Esc:** cancels.

**The call** (`search.py`): one `claude -p`.

- **Flags:** `--tools Read,Grep,Glob`, `--add-dir <claude home>/projects`, `--no-session-persistence`, `--strict-mcp-config` and `--setting-sources ""`.
- **Folder:** run from an empty temporary folder.
- **Shared helper:** classification and search build this command with the same helper (`headless.py`), so they stay equally isolated.
- **Reach:** the tools can't reach outside the projects folder. In `-p` mode `claude` refuses Read, Grep and Glob elsewhere (checked against canary files in `/tmp` and in home).
- **Settings:** no user settings load that could allow more.
- **Prompt:** the query, plus an index of every session: ID, start, last activity, folder and title.
- **System prompt:** describes the transcript format (including how slash commands appear), and tells Claude to:
  - grep first, never reading a whole file;
  - judge relevance from what matching lines say, not from how often words appear;
  - compare timestamps for first/last questions;
  - return at most 20 IDs, most relevant first, with a one-sentence reason each.
- **Output:** through `--json-schema` as `{matches: [{session_id, reason}]}`.
- **Model:** `--search-model` (default Sonnet).
- **Cost cap:** `--search-budget` (default $1), passed as `--max-budget-usd`.
  - claude checks it between turns, so a search can overshoot by one turn.
- **Typical:** 10-20 s and about $0.15 for about 330 sessions.

**Checking results:**

- **Dropped:**
  - IDs that aren't sessions in the index (reported as "made-up" in the notice)
  - repeats
  - malformed items
- **File names:** `<id>.jsonl` is accepted as its ID.

**Errors**, shown as a notification, leaving the list unchanged:

- budget reached (with the amount spent)
- not logged in, and other claude errors
- no structured result
- over 5 minutes (the process is killed)

**Running:** in a background thread, so the list stays usable.

- **Notification:** "Searching the transcripts for “query”…" stays up until the search ends (it outlasts the 5-minute limit), then gives way to the result or error.
- **Title bar:** shows the query too.
- **One search at a time:** `f` is blocked while a search runs, with a notice naming it.
- **Stopping:** `a` and quitting stop it.
  - claude runs in its own process group, so the whole group is terminated, including the ripgrep its Grep starts.
  - The notice is removed.
  - Results that still arrive are dropped.
- **Thread safety:** the worker hands over the process under a lock, so a search stopped just as claude starts still has it stopped.
- **Quitting:** stops it without touching any widget (they're already gone when the app unmounts).
- **Killed by something else** (e.g. out of memory): the search ends with an error naming the signal; it never looks stuck.
- **Before the first scan:** `f` is blocked until the first scan is shown, so the index holds every session.
- **Notice removal:** uses Textual internals (there's no public API for one notification); if they change, the notice just stays until its timeout.

**Results:**

- **Shown:** only the matching sessions, in Claude's order (a "relevance" sort with no column mark).
- **Re-sorting:** `s` or a header click.
- **Group drop-down and `/` filter:** reset to All and cleared, so no match is hidden; `/` can then narrow them.
- **Title bar:** `AI search “query”: N found, M shown (a = show all)`.
- **Details panel:** adds `AI search: <reason>` for the selected session.
- **Refreshes:** results are kept by session ID, so they survive.
- **`a`:** clears them and restores the default sort.

**CLI:** `cc-sessions --search QUERY`.

- **Output:** the matches (last activity, ID, folder, title, reason) and the cost.
- **`--json`:** a list with session ID, title, folder, last activity, reason and a resume command.
- **`-n` or `--days`:** only those sessions are in the index.
  - Claude is told only indexed sessions count.
  - Others it returns are ignored as outside the search.
- **Exit code:** 1 if the search fails, including when claude is killed.

## Marking (selection)

- **Space:** marks or unmarks the current session and moves down a row.
- **Marked row:** shows a red `●` before its status.
- **`u`:** unmark all. It never marks anything, so pressing it "to be safe" can't set up a delete.
- **Shift+Space:** unmark all; with nothing marked, it marks the current row like Space.
  - Works only in terminals that report Shift with Space; many send a plain space.
- **Title bar:** "N marked" on the right side.
- **Rescan:** clears marks for sessions that disappear.

## Stable cursor

- **List rebuilt** (a batch arrives, a row is marked, the filter changes): keep the selected session on the same screen line, even if it moves to another group.
  - Near the end of the list, scroll as far as the list allows.

---

# Managing sessions

## Opening (resuming) a session

- **Enter** on a row: closes the UI and runs `claude --resume <id>` in the session's folder.
- **Non-default config folder:** passed on through `CLAUDE_CONFIG_DIR`, as an absolute path.
- **Refused:**
  - a running session ("still open in another terminal");
  - a session whose folder no longer exists, with a notice naming the folder (never run `claude` from the wrong folder).
- **CLI:** `--json` output holds a ready-to-paste resume command for each session, including the `CLAUDE_CONFIG_DIR=` prefix when needed.

## Renaming

- **`n`:** opens a dialog with the current title filled in and selected.
  - Enter saves; Esc cancels.
- **Saving** works the same way as Claude Code's `/rename`:
  - append a `custom-title` entry to the transcript (adding a newline first if the file doesn't end with one);
  - write `<session-id>/custom-title.json` next to it.
- **Label:** carried over to the new modification time, so a rename doesn't trigger classification again.
- **Refused:**
  - an empty title;
  - a session that's running at the moment of renaming (checked then, not from the scan).
- **Unchanged title:** saving does nothing.
- **Transcript vanishes** right after the rename: show a warning instead of crashing.

## Moving

- **`m`:** opens a dialog with the session's folder filled in.
  - Enter moves; Esc cancels.
- **Target folder:** must exist; `~` is expanded and symlinks are resolved, as Claude Code's own working folder is.
- **Tab completion** (`paths.py`):
  - **Tab:** fills in the first folder (A to Z, ignoring case) whose name starts with the text after the last `/`, in the folder before it.
  - **Further Tabs:** cycle to the next match, wrapping round.
  - **Empty name** (just after `/`): matches every folder.
  - **Home folder:** an empty field, `~`, or text with no `/` completes from it, filling in `~/`.
  - **Enter:** applies the same rule, so a bare name always means a folder in home, never one relative to where the app was started.
  - **Offered:** only folders (symlinks to folders count); hidden folders only when the name typed starts with `.`.
  - **Completed folder:** gets no trailing `/`, so Tab keeps cycling; typing `/` moves into it.
  - **Any edit:** starts a new round, even one that ends on the same text.
  - **No match, or a folder part that doesn't exist:** Tab leaves the text unchanged.
  - **Focus:** Tab never moves it out of the field.
- **Red highlighting**, live as you type:
  - **Folder part** (up to the last `/`; for a bare name, the whole name): red if it isn't an existing, readable folder.
  - **Name after it:** otherwise red if no folder there starts with it.
  - **Always fine:** an empty name, `.` and `..`.
  - **`~name/` for a user that doesn't exist:** shown red, never a crash.
  - **Disk check:** once per change to the text, not on every redraw (the cursor blink redraws about twice a second).
- **Known limit:** Tab lists the folder on the UI thread, which is instant locally but could pause on a very large folder or a slow network mount.
- **Working folders:** rewrite every `cwd` in the transcript.
  - **Already inside the new folder:** stays as it is (moving `/repo` to `/repo/sub` keeps `/repo/sub/x`).
  - **Launch folder** (the one the session's project folder is named after; otherwise the first recorded): becomes the new folder.
  - **Subfolder of the launch folder:** becomes the same subfolder of the new folder if that exists, otherwise the new folder.
  - **Any other folder:** becomes the new folder.
  - **Rewritten lines:** only those whose `cwd` changes, as compact JSON like Claude Code's.
  - **Every other line:** stays byte for byte, including malformed ones.
- **Files:** move the transcript and its `<session-id>/` folder to `projects/<project name>/`, where Claude Code keeps the new folder's sessions.
  - **Project name:** follows Claude Code's rule: every character that isn't an ASCII letter or digit becomes `-` (per UTF-16 code unit).
  - **Names over 200 characters:** cut and given a base-36 hash of the path.
  - **Tests:** compare against values from Claude Code's own function.
- **Safety:**
  - Write the new transcript through a temporary file and flush it to disk (file and folder) before the original is removed.
  - Move the session folder with a single rename, never a copy, so it can't end up half-moved.
  - Remove the original last.
  - If any earlier step fails, undo the others, leaving the original untouched and no partial or temporary files behind.
  - Just before committing, check again that the session isn't running and that the transcript hasn't grown or changed since it was read; otherwise stop, so a session resumed elsewhere mid-move loses nothing.
  - A line holding an unpaired surrogate (text cut mid-emoji) is written with its non-ASCII characters escaped, which parses the same, instead of failing the move.
- **Refused:**
  - a folder that doesn't exist;
  - a session that's running at the moment of moving (checked then, not from the scan);
  - a move to where it already is (both its project folder and its recorded folders);
  - a target that already has a session with that ID.
- **Session started somewhere else:** typing the folder it last worked in still moves it.
- **Auto-refresh while the dialog is open:** if it replaced the rows, update the current row, not the one the dialog was opened from.
- **Label:** kept, as for renames.
- **Scan running during the move:** can list the session in both folders; show it once.
- **Effect** (checked against the real `claude` CLI):
  - `claude --continue` in the new folder picks the session up.
  - The new folder's `--resume` picker lists it.
  - `claude --resume <id>` finds a session from any folder anyway, and appends to it wherever it lives.
- **Not changed:**
  - paths inside messages and tool calls
  - subagent transcripts
  - `/rewind` file backups (which restore to the original paths)
- **Memory isn't migrated or cloned:**
  - Auto memory (`projects/<project>/memory/`) belongs to the folder and is shared by all its sessions, so it stays put.
  - The moved session uses the new folder's memory.
  - The move notice says so.

## Deleting

- **`d`:** deletes the marked sessions, or the current one if none are marked, after a confirmation dialog.
- **The dialog:**
  - lists up to 10 titles, plus "… and N more";
  - opens with Cancel selected, so a stray Enter is harmless;
  - `y` deletes; `n` or Esc cancels.
- **Trash:** move these files to the desktop Trash (via `send2trash`), so they can be restored:
  - the transcript;
  - its folder of subagent transcripts and tool output;
  - `file-history/<id>` (the `/rewind` backups);
  - `session-env/<id>`.
- **Shared prompt history** (`history.jsonl`): never touched.
- **Running sessions:** skipped, checked at the moment of deletion, not from the scan.
- **Partial failure:**
  - Try every file even if one fails.
  - If the transcript went to the Trash but other files didn't, treat the session as deleted and warn about what was left behind.
- **Clean-up:**
  - Drop the deleted sessions' labels from the cache.
  - Remove them from the marked and "Classifying…" sets.

---

# Command-line interface (`cc-sessions`)

- **Default output:** sessions grouped by status in rank order.
  - Each row: last activity, full session ID, folder, title and outstanding-work note.
  - At the end: a summary count and a resume hint.
- **`--json`:** machine-readable output with group, status rank, session ID, title, folder, last activity, detail, context tokens, model and a resume command.
- **`--search QUERY`:** AI search instead of the list (see [AI search](#ai-search)).
  - `--search-model` and `--search-budget` set its model and cost cap, in the CLI and the UI.
- **Filters:**
  - `-n N`: most recent N, at least 1
  - `--days N`: active in the last N days, more than 0
  - `-s GROUP`: only this group, repeatable
  - `--pending`: only sessions that need you
- **Classification:**
  - `--no-llm`: cached labels only
  - `--refresh`
  - `--model`
  - `--batch-size`
  - `--jobs`
- **Invalid values:** reported as a one-line usage error, not a traceback.

---

# Configuration

## Claude config folder

- **Folder:** `$CLAUDE_CONFIG_DIR` if it's set and not empty; otherwise `~/.claude`.
- **Paths:** `~` is expanded and relative paths are resolved to absolute.
- **`--claude-home DIR`:** overrides the folder for one run.
- **`--help`:** shows the folder in use.
- **Common options** (`--no-llm`, `--model`, `--batch-size`, `--jobs`, `--claude-home`): both commands share them from one definition.

## Requirements

- **Python:** 3.14 or later, managed with `uv`.
- **`claude` CLI:** on `PATH`, logged in.
- **Linux:** for the PID-reuse check and the file lock; other systems fall back to the plain PID check.

---

# UI experience

## Layout

- **Title bar:**
  - left-aligned: a summary ("N shown", plus counts for running, needs-you, interrupted and failed sessions) with dimmed progress text
  - right-aligned: "N marked"
- **Filter row:** the status drop-down and the search box.
- **Table columns:**
  - Status: in its status colour
  - Started
  - Last activity: local time
  - Title
  - Folder: home shown as `~`
  - Context
  - Model
- **Context cell:** context size, right-aligned, as `0.8k`, `134k` or `1.0M`.
  - Blank when there's no usage data.
  - Coloured by band: blue (0 to under 250k), green (250k to under 500k), yellow (500k to under 800k), red (800k and above).
  - Sorts largest first when its heading is clicked.
- **Column widths:** set when the table is drawn, as the widest of a column's cells across all sessions (not just the visible ones) and its heading with the longest sort mark it can get (`^`/`v`, plus the number if it's in one of the `s` orders).
  - Sorting, searching and filtering therefore never resize a column or move the headings.
  - Quick header clicks don't show squashed frames.
  - Widths change only when a refresh adds, removes or updates sessions.
  - Why: left to size themselves, Textual's columns start at heading width and only measure their cells once the table is idle.
- **Folder cell:** limited to 40 characters, keeping the end of the path behind a leading `…`.
- **Literal text:** titles, folders and models are shown exactly as written, never read as Rich markup, so `arr[0]` keeps its brackets and `[/]` can't crash the table.
- **Title cell:** limited to 50 characters, ending in `...`.
  - Cut before a word: at spaces first, then `-` `_` `/` for kebab-case titles.
  - A single word longer than the limit is cut mid-word.
- **Details panel:** status and outstanding-work note, full title, full session ID, folder and full model ID.
- **Footer:** the main keys.

## Path display

- Abbreviate the home folder to `~` only for the home folder itself or paths inside it, so `/home/annbackup` isn't shown as `~backup`.

## Keys help

- **`k`:** shows or hides Textual's keys panel, listing every key and what it does.
- **`s` entry:** lists the four sort orders, one per line.
- **README:** its key table documents every key.

## Command palette and theming

- **Ctrl+P:** opens the command palette, with Textual's built-in commands:
  - Theme
  - Keys
  - Screenshot: saves an SVG of the screen
  - Maximize/Minimize
  - Quit
- **Theme picker:** marks the current theme with `* ` and indents the others to match.
  - Uses an ASCII marker, so alignment holds in terminals that draw `●` two columns wide.
  - Wraps Textual's theme list, so Textual's own listing and search behaviour carry through.
  - A test checks that the built-in Theme command opens the marked list.
- **Theme keys:** hidden from the footer, but listed in the keys panel (`k`).
  - **Ctrl+T:** the next theme, in the theme picker's (alphabetical) order, wrapping round at the end.
  - **Ctrl+Y:** the previous theme, wrapping round at the start.
  - **Ctrl+R:** back to the default theme, `textual-dark`.
  - Each press names the new theme in a notice.
  - Ctrl+Shift+T and Ctrl+Alt+T were tried first and rejected: the desktop and terminal take them (a new tab, opening a terminal), and terminals without the kitty keyboard protocol send both as Ctrl+T.
    Ctrl+Shift+R was tried for the reset, to keep it apart from `r` (refresh), but those terminals send it as plain Ctrl+R.

## Notifications

- **Short notices:** sort changes, renames, moves and deletes.
- **Errors:** failed classification, deletes and resumes.
- **Warnings:** partial deletes and running-session refusals.

---

# Performance and benchmarking

- **Startup:**
  - from the scan cache: the list appears in about 0.2-0.3 s
  - cold start: the list appears after about 2.5 s, then fills in as batches return
- **Defaults:** chosen by benchmark.
  - Batch size 5: quickest first results, accuracy within the model's own run-to-run noise.
  - 16 parallel calls: about 1.7× faster than 8, with no failed calls.
  - Rate limits: these calls share the account's limits with other Claude sessions.
- **Benchmark script:** `benchmarks/classify_batch_size.py` measures, over a fixed random sample, how batch size and parallel calls affect:
  - total time, time to first result and sessions per second;
  - cost;
  - accuracy against a batch-size-1 reference, which is run twice to measure the model's own noise.
- **Benchmark output:** a Markdown table, plus JSON saved under `benchmarks/results/`.
- **`--no-reference`:** skips the accuracy runs.
- **App caches:** the benchmark never writes to them.

---

# Reliability and safety

- **Main thread only:** the UI changes its in-memory state (rows, marks, the "Classifying…" set) only there; the background worker hands results over.
- **One at a time:** one scan or classification, and one AI search; `f` waits for the running search.
- **Deletes:** go to the Trash, never permanent removal.
- **Running sessions:** never renamed, moved, deleted or resumed from the viewer.
- **Unusual input** (malformed transcripts, cache files or session files, vanished files): skipped or treated as empty; it never stops the list from loading.

---

# Development and quality

- **Tests:** `uv run pytest` runs the suite (132 tests).
  - Refresh tests: `tests/test_refresh.py`
  - Move tests: `tests/test_move.py`
- **Isolation:**
  - Tests never touch the real caches; an autouse fixture redirects them.
  - Tests that delete, rename or move use a fake config folder, and real sessions are never modified.
- **Fixes:** each bug fix lands as its own commit, with a test that fails before the fix.
- **Documentation:** the README documents setup, usage, keys, options, caching, `claude` call isolation and limitations.
