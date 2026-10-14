"""cc-sessions-ui: a terminal UI for browsing Claude Code sessions."""

import argparse
import functools
import os
import re
import shutil
import sys
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.cells import cell_len
from rich.highlighter import Highlighter
from rich.text import Text
from textual import constants, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits
from textual.containers import Grid, Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.notifications import Notification, Notify
from textual.screen import ModalScreen
from textual.theme import ThemeProvider
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static

from .classify import DEFAULT_BATCH_SIZE, DEFAULT_JOBS, Cache, classify, error_labels
from .cli import (
    CACHE_PATH,
    GROUPS,
    STATUS_RANK,
    add_common_args,
    add_search_args,
    detail_of,
    group_of,
    refresh_interval_arg,
    short_path,
)
from .delete import trash_session
from .headless import stop
from .move import move_session
from .paths import Completer, problem_span, to_path
from .rename import rename_session
from .scan import Session, claude_env, live_sessions, parse_transcript, scan
from .search import DEFAULT_SEARCH_BUDGET, DEFAULT_SEARCH_MODEL, SEARCH_TIMEOUT, search
from .terminal import open_in_terminal

PENDING = {"decision", "action", "interrupted"}
DEFAULT_REFRESH_INTERVAL = 30.0  # seconds between automatic refreshes; 0 turns them off
RESCAN_AFTER_OPEN = 5.0  # seconds after opening a new terminal

# The sort mark in a column heading, coloured so it stands apart from the name.
SORT_MARK_STYLE = "bold #61afef"

GROUP_STYLE = {
    "running": "bold #4ec96b",
    "decision": "bold #f0b429",
    "action": "bold #f25f5c",
    "interrupted": "#c678dd",
    "optional": "#56b6c2",
    "done": "dim",
    "unclassified": "dim italic",
    "error": "bold #ff8c42",
}

COLUMNS = [
    ("status", "Status"),
    ("started", "Started"),
    ("last", "Last activity"),
    ("title", "Title"),
    ("cwd", "Folder"),
    ("context", "Context"),
    ("model", "Model"),
]

# How each column sorts: key function and whether it runs descending by default.
SORT_KEYS = {
    "status": (lambda r: STATUS_RANK[r.group], False),  # needs you first
    "started": (lambda r: r.session.started, True),  # newest first
    "last": (lambda r: r.session.last_timestamp, True),  # newest first
    "title": (lambda r: r.session.title.lower(), False),
    "cwd": (lambda r: r.session.cwd, False),
    "context": (lambda r: r.session.context_tokens, True),  # largest first
    "model": (
        lambda r: (not r.session.model, short_model(r.session.model)),
        False,
    ),  # A to Z, blanks last
}

# The orders the s key cycles through: (primary, secondary, tertiary).
SORT_MODES = [
    ("status", "last"),
    ("last", "status"),
    ("cwd", "status", "last"),
    ("last", "cwd", "status"),
]
# Columns whose heading is right-aligned, to match their values.
RIGHT_ALIGNED = {"context"}


def mark_room(key: str) -> int:
    """Width a sort mark can add to a heading: " v", plus the number in a multi-column
    sort.
    """
    numbered = any(key in mode for mode in SORT_MODES)
    return 2 + (len(str(max(map(len, SORT_MODES)))) if numbered else 0)


COLUMN_NAMES = {
    "status": "status",
    "started": "start",
    "last": "last activity",
    "title": "title",
    "cwd": "folder",
    "context": "context size",
    "model": "model",
}
TITLE_MAX = 50
FOLDER_MAX = 40


def truncate_start(text: str, limit: int = FOLDER_MAX) -> str:
    """Keep the end of a path, which tells folders apart:
    "…/my-app/.claude/skills/x".
    """
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def truncate_words(text: str, limit: int = TITLE_MAX, marker: str = "...") -> str:
    """Shorten text to at most `limit` chars, cutting at a word boundary and adding
    `marker`.

    Spaces are preferred breaks; hyphens, underscores and slashes are used when there
    are no spaces (kebab-case titles). A single word longer than the limit is cut hard.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    room = limit - len(marker)
    head = text[: room + 1]  # +1 so a break right after `room` chars still counts
    for seps in (" ", "-_/"):
        cut = max(head.rfind(c) for c in seps)
        if cut > 0:
            return text[:cut].rstrip(" -_/.,;:") + marker
    return text[:room] + marker


def format_tokens(n: int) -> str:
    """Token count for the table: 1.2k, 134k, 1.0M; blank when unknown."""
    if n <= 0:
        return ""
    if n >= 999_500:
        return f"{n / 1_000_000:.1f}M"
    if n >= 9_950:
        return f"{n / 1000:.0f}k"
    return f"{n / 1000:.1f}k"


# Context size colour bands: (upper bound, exclusive; style). Each band starts where
# the last ends.
CONTEXT_BANDS = [
    (250_000, "#61afef"),  # blue: 0 to 250k
    (500_000, "#4ec96b"),  # green: 250k to 500k
    (800_000, "#f0b429"),  # yellow: 500k to 800k
    (None, "bold #f25f5c"),  # red: 800k and above
]


def context_style(n: int) -> str:
    return next(style for limit, style in CONTEXT_BANDS if limit is None or n < limit)


# Provider prefixes ("us.anthropic.claude-", "anthropic/claude-") and suffixes: a date
# ("-20251001", Vertex "@20250929") and a Bedrock version ("-v1:0").
MODEL_PREFIX = re.compile(r"^.*?claude-")
MODEL_SUFFIX = re.compile(r"(?:[-@]\d{8})?(?:-v\d+(?::\d+)?)?$")
MODEL_MAX = 20


@functools.cache  # called for every row on each render and sort
def short_model(model: str) -> str:
    """Model ID for the table: claude-opus-5-5 -> opus-5-5, claude-haiku-4-5-20251001
    -> haiku-4-5.

    Bedrock and Vertex IDs shorten the same way; anything else is capped at MODEL_MAX.
    """
    return truncate_words(
        MODEL_SUFFIX.sub("", MODEL_PREFIX.sub("", model, count=1), count=1), MODEL_MAX
    )


def local_time(ts: str) -> str:
    """ISO UTC timestamp -> 'YYYY-MM-DD HH:MM' in local time."""
    if not ts:
        return ""
    try:
        return (
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except ValueError:
        return ts[:16]


class Row:
    def __init__(self, s: Session, labels: dict[str, dict]):
        self.session = s
        self.update(labels)

    def update(self, labels: dict[str, dict]) -> None:
        info = labels.get(self.session.session_id)
        self.group = group_of(self.session.state, info and info["label"])
        self.detail = detail_of(self.session, info)


class MarkedThemeProvider(ThemeProvider):
    """Textual's theme list, with a marker on the theme in use.

    Wraps the hits from ThemeProvider rather than re-implementing it, so upstream
    changes to listing or filtering carry through. The marker is ASCII: characters
    like "●" are drawn two cells wide in some terminals, which would break alignment.
    """

    def _marked(self, hit: Hit | DiscoveryHit) -> Hit | DiscoveryHit:
        shown = hit.prompt
        marker = ("* ", "bold $accent") if hit.text == self.app.theme else "  "
        prompt = Content.assemble(marker, shown)
        if isinstance(hit, DiscoveryHit):
            return replace(hit, display=prompt)
        return replace(hit, match_display=prompt)

    async def discover(self) -> Hits:
        async for hit in super().discover():
            yield self._marked(hit)

    async def search(self, query: str) -> Hits:
        async for hit in super().search(query):
            yield self._marked(hit)


class TitleBar(Horizontal):
    """One-line header: summary and progress on the left, mark count on the right.

    Textual's Header centres a single title, so it can't split the line.
    """

    DEFAULT_CSS = """
    TitleBar {
        dock: top; height: 1; background: $panel;
        color: $foreground; padding: 0 1;
    }
    TitleBar #title-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    TitleBar #title-right {
        width: auto; padding-left: 2; color: #f25f5c; text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="title-left")
        yield Static(id="title-right")

    def show(self, left: Text, right: str) -> None:
        self.query_one("#title-left", Static).update(left)
        self.query_one("#title-right", Static).update(right)


class SessionTable(DataTable):
    # Re-label DataTable's own Enter binding so it shows in the footer.
    BINDINGS = [
        Binding("enter", "select_cursor", "Resume"),
        Binding("o", "app.open_terminal", "New terminal"),
        # Vim keys, alongside the arrows: listed in the keys panel, not the footer.
        Binding("k", "cursor_up", "Cursor up", show=False),
        Binding("j", "cursor_down", "Cursor down", show=False),
        Binding("h", "cursor_left", "Cursor left", show=False),
        Binding("l", "cursor_right", "Cursor right", show=False),
    ]


class RenameSession(ModalScreen[str | None]):
    """Ask for a new title. Enter saves, Esc cancels."""

    CSS = """
    RenameSession { align: center middle; }
    #rename-dialog {
        width: 80; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #rename-dialog Label { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-dialog"):
            yield Label(
                f"Rename session {self.session.session_id[:8]}  "
                "(Enter = save, Esc = cancel)"
            )
            yield Input(value=self.session.title, id="new-title")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        title = " ".join(event.value.split())
        if not title:
            self.notify("The title can't be empty.", severity="warning")
            return
        self.dismiss(title if title != self.session.title else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PathProblems(Highlighter):
    """Show the part of a typed path that doesn't exist in red: the folder, or the
    name.
    """

    def __init__(self):
        self.last: tuple[str, tuple[int, int] | None] | None = None

    def highlight(self, text: Text) -> None:
        # Textual highlights on every render, including each cursor blink: only check
        # the disk when the text has changed.
        if self.last is None or self.last[0] != text.plain:
            self.last = (text.plain, problem_span(text.plain))
        if span := self.last[1]:
            text.stylize("bold #f25f5c", *span)


class PathInput(Input):
    """A folder path input: Tab fills in the next matching folder, bad parts show in
    red.
    """

    BINDINGS = [
        Binding("tab", "complete", "Complete folder", show=False)
    ]  # instead of moving focus

    def __init__(self, *args, **kwargs):
        super().__init__(*args, highlighter=PathProblems(), **kwargs)
        self.completer = Completer()

    def action_complete(self) -> None:
        value = self.completer.complete(self.value)
        if value != self.value:
            self.value = value
            self.cursor_position = len(value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if (
            event.value != self.completer.filled
        ):  # typed, not filled in by Tab: a new round
            self.completer.reset()


class AskSearch(ModalScreen[str | None]):
    """Ask what to search the transcripts for. Enter searches, Esc cancels."""

    CSS = """
    AskSearch { align: center middle; }
    #search-dialog {
        width: 90; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #search-dialog Label { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, query: str = ""):
        super().__init__()
        self.query_text = query

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label(
                "AI search: Claude looks through every transcript  "
                "(Enter = search, Esc = cancel)"
            )
            yield Input(
                value=self.query_text,
                placeholder=(
                    "e.g. session that created github repository MY-EXAMPLE-REPO"
                ),
                id="ai-query",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = " ".join(event.value.split())
        if not query:
            self.notify("Type what to search for.", severity="warning")
            return
        self.dismiss(query)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MoveSession(ModalScreen[Path | None]):
    """Ask for the folder to move a session to. Enter moves, Esc cancels."""

    CSS = """
    MoveSession { align: center middle; }
    #move-dialog {
        width: 80; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #move-dialog Label { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Label(
                f"Move session {self.session.session_id[:8]} to folder  "
                "(Tab = complete, Enter = move, Esc = cancel)"
            )
            yield PathInput(value=short_path(self.session.cwd), id="new-folder")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        dest = to_path(
            event.value
        )  # a bare name is in home, as for Tab and the red text
        if not event.value.strip() or dest is None or not dest.is_dir():
            self.notify(
                f"{event.value.strip() or 'That'} isn't an existing folder.",
                severity="warning",
            )
            return
        self.dismiss(dest)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDelete(ModalScreen[bool]):
    """Ask before trashing sessions. Cancel has focus, so a stray Enter is harmless."""

    CSS = """
    ConfirmDelete { align: center middle; }
    #dialog {
        grid-size: 2; grid-gutter: 1 2; grid-rows: auto auto;
        width: 80; height: auto; max-height: 80%;
        padding: 1 2; border: thick $error; background: $surface;
    }
    #question { column-span: 2; height: auto; }
    Button { width: 100%; }
    """
    BINDINGS = [
        Binding("y", "answer(True)", "Delete"),
        Binding("n,escape", "answer(False)", "Cancel"),
    ]

    def __init__(self, sessions: list[Session]):
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        n = len(self.sessions)
        lines = [f"Move {n} session{'s' if n != 1 else ''} to the Trash?", ""]
        lines += [
            f"  • {truncate_words(s.title or s.session_id, 60)}"
            for s in self.sessions[:10]
        ]
        if n > 10:
            lines.append(f"  … and {n - 10} more")
        lines += [
            "",
            "You can restore them from the Trash. y = delete, n / Esc = cancel",
        ]
        with Grid(id="dialog"):
            yield Label("\n".join(lines), id="question")
            yield Button("Delete", variant="error", id="yes")
            yield Button("Cancel", variant="primary", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


def ignore_after_quit(method):
    """Drop a worker result or queued event that arrives after quitting removed the
    widgets.

    Only a missing widget on a torn-down screen is ignored; any other NoMatches still
    raises.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except NoMatches:
            if self.widgets_present():
                raise

    return wrapper


class SessionsApp(App[Session | None]):
    TITLE = "Claude Code sessions"
    CSS = """
    #filters { height: auto; padding: 0 1; }
    #group { width: 32; }
    #search { width: 1fr; }
    DataTable { height: 1fr; }
    #detail { height: auto; min-height: 4; padding: 0 1; border-top: solid $primary; }
    """
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding(
            "s",
            "cycle_sort",
            "Sort",
            tooltip="Cycle the sort order:\n1) status, last activity\n"
            "2) last activity, status\n"
            "3) folder, status, last activity\n4) last activity, folder, status",
        ),
        Binding("p", "pending", "Pending"),
        Binding("a", "all", "All"),
        Binding("/", "focus_search", "Search"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("u", "unmark_all", "Unmark all"),
        # Most terminals send plain Space for Shift+Space; this only fires where Shift
        # is reported.
        Binding("shift+space", "clear_marks_or_mark", "Unmark all", show=False),
        Binding("n", "rename", "Rename"),
        Binding("m", "move", "Move"),
        Binding("f", "ai_search", "AI search"),
        Binding("d", "delete", "Delete"),
        Binding("ctrl+k", "toggle_keys", "Keys", show=False),
        Binding("escape", "focus_table", "Table", show=False),
        # Theme keys: listed in the keys panel (Ctrl+K), not the footer.
        Binding("ctrl+t", "cycle_theme(1)", "Next theme", show=False),
        Binding("ctrl+y", "cycle_theme(-1)", "Previous theme", show=False),
        Binding("ctrl+r", "default_theme", "Default theme", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        claude_home: Path,
        model: str,
        use_llm: bool,
        batch_size: int = DEFAULT_BATCH_SIZE,
        jobs: int = DEFAULT_JOBS,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        search_model: str = DEFAULT_SEARCH_MODEL,
        search_budget: float = DEFAULT_SEARCH_BUDGET,
    ):
        super().__init__()
        self.search_model = search_model
        self.search_budget = search_budget
        # AI search results, shown instead of the full list until a cleared them:
        # session ID -> (rank, reason). Kept by ID so they survive refreshes rebuilding
        # the rows.
        self.found: dict[str, tuple[int, str]] | None = None
        self.found_query = ""
        self.searching = ""  # the query of a search in progress
        self.search_proc = None  # its claude process, to stop it
        self.search_token = (
            0  # bumped per search, so a stopped one's results are dropped
        )
        self.search_notice: Notification | None = (
            None  # "Searching…", shown until it ends
        )
        # The last theme key's notice, replaced by the next so quick presses don't
        # stack.
        self.theme_notice: Notification | None = None
        # The worker records the process while the UI may be stopping the search: one at
        # a time.
        self.search_lock = threading.Lock()
        self.loaded = (
            False  # the first scan has been shown: the session index is complete
        )
        self.refresh_interval = refresh_interval
        self.batch_size = batch_size
        self.jobs = jobs
        self.classifying: set[str] = set()  # session IDs queued for the model
        self.claude_home = claude_home
        self.model = model
        self.use_llm = use_llm
        self.cache = Cache(CACHE_PATH)
        self.rows: list[Row] = []
        self.marked: set[str] = set()  # session IDs picked for deletion
        self.sort_keys: tuple[str, ...] = SORT_MODES[0]  # primary first
        self.sort_flipped = False  # header clicked twice: primary runs the other way
        self.loading = False  # a scan/classify worker is running
        self.refresh_queued = False  # r pressed during a load: run another when it ends
        # After a run hits a classification error, automatic refreshes stop calling the
        # model until a manual r completes a run without errors.
        self.classify_paused = False
        self.failed: dict[str, dict] = (
            {}
        )  # in-memory "error" labels, shown until reclassified
        # Deletes and renames made while a load is scanning, applied to its (older)
        # results.
        self.load_deleted: set[str] = set()
        self.load_changed: dict[str, dict] = (
            {}
        )  # renames and moves, by session: Session fields
        self.shown_signature = (
            None  # what show() last rendered, to skip no-change refreshes
        )
        self.error_notified = False  # shown the "classification failed" notice this run

    def compose(self) -> ComposeResult:
        yield TitleBar()
        with Horizontal(id="filters"):
            options = [("All", "all"), ("Needs you", "pending")] + [
                (v, k) for k, v in GROUPS.items()
            ]
            yield Select(options, value="all", allow_blank=False, id="group")
            yield Input(
                placeholder="Filter by title, folder or session ID  (/)", id="search"
            )
        yield SessionTable(cursor_type="row", zebra_stripes=True, id="table")
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        self.add_columns(table, [])
        table.focus()
        self.start_load()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self.auto_refresh)

    # Data loading ---------------------------------------------------------

    def start_load(self, quiet: bool = False, auto: bool = False) -> None:
        # Only one load at a time: a thread worker can't be cancelled while it waits on
        # claude, so a second run would classify the same sessions twice.
        self.loading = True
        self.error_notified = False
        self.load_deleted, self.load_changed = set(), {}
        classify_now = self.use_llm and not (auto and self.classify_paused)
        self.load(quiet, classify_now, dict(self.failed))

    def auto_refresh(self) -> None:
        """Timer tick: rescan quietly, and classify anything new or changed.

        Skipped while a load is still running, and while a dialog, the command palette
        or another screen is open, so nothing changes under the user mid-action. While
        classification is paused after an error, it rescans but doesn't call the model.
        """
        if self.loading or len(self.screen_stack) > 1:
            return
        self.cache.reload()
        self.start_load(quiet=True, auto=True)

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def load_finished(self, outcome: bool | None) -> None:
        """outcome: True if classification hit errors, False if it ran cleanly, None if
        it didn't run.
        """
        self.loading = False
        if outcome is not None:
            self.classify_paused = outcome
        self.set_status(
            "Classification paused after an error. Press r to retry."
            if self.classify_paused
            else ""
        )
        if self.refresh_queued:
            self.refresh_queued = False
            self.cache.reload()
            self.start_load()

    @work(thread=True)
    def load(self, quiet: bool, classify_now: bool, failed: dict[str, dict]) -> None:
        outcome = None
        try:
            outcome = self._load(quiet, classify_now, failed)
        finally:
            self.call_from_thread(self.load_finished, outcome)

    def _load(
        self, quiet: bool, classify_now: bool, failed: dict[str, dict]
    ) -> bool | None:
        if not quiet:  # auto refreshes don't flash "Scanning…" every interval
            self.call_from_thread(self.set_status, "Scanning transcripts…")
        sessions = scan(self.claude_home)
        # One split, used both to mark rows "Classifying…" and to choose what to send.
        cached, todo = self.cache.partition(sessions)
        send = todo if classify_now else []
        # Sessions not being sent keep any earlier "Classification failed" label.
        labels = {
            **cached,
            **{
                s.session_id: failed[s.session_id]
                for s in todo
                if s.session_id in failed
            },
        }
        # Show the list straight away; unclassified rows fill in as batches come back.
        # The classifying set is handed to the main thread with it, which owns that
        # state.
        self.call_from_thread(self.show, sessions, labels, {s.session_id for s in send})
        if not send:
            return None
        self.call_from_thread(
            self.set_status, f"Classifying 0/{len(send)} with {self.model}…"
        )
        errors = []

        def on_batch(
            new: dict[str, dict], batch: list[Session], err: Exception | None
        ) -> None:
            if err:
                errors.append(err)
                new = {**new, **error_labels(batch, err, skip=new)}
            self.call_from_thread(
                self.apply_batch, new, [s.session_id for s in batch], len(send), err
            )

        classify(
            send, self.cache, self.model, self.batch_size, self.jobs, on_batch=on_batch
        )
        return bool(errors)

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def apply_batch(
        self,
        new: dict[str, dict],
        batch_ids: list[str],
        total: int,
        err: Exception | None = None,
    ) -> None:
        for r in self.rows:
            if r.session.session_id in new:
                r.update(new)
        for sid, label in new.items():
            if label["label"] == "error":
                self.failed[sid] = label
            else:
                self.failed.pop(sid, None)
        if err and not self.error_notified:
            self.error_notified = True  # once per run; the rows carry the detail
            msg = next(iter(new.values()))["pending"] if new else str(err)
            self.notify(
                f"{msg}\nAuto-refresh won't classify until it works again. "
                "Press r to retry.",
                title="Classification failed",
                severity="error",
                timeout=10,
            )
        self.classifying -= set(batch_ids)
        self.set_status(
            f"Classifying {total - len(self.classifying)}/{total} with {self.model}…"
        )
        self.render_table()

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def set_status(self, msg: str) -> None:
        self.sub_title = msg
        self.update_title_bar()

    def update_title_bar(self) -> None:
        left = Text(self.title)
        if self.sub_title:
            left.append(f"  —  {self.sub_title}", style="dim")
        if self.searching:
            left.append(f"  —  AI search “{self.searching}”…", style="dim")
        self.query_one(TitleBar).show(
            left, f"{len(self.marked)} marked" if self.marked else ""
        )

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def show(
        self,
        sessions: list[Session],
        labels: dict[str, dict],
        classifying: set[str] | None = None,
    ) -> None:
        # The scan may predate a delete, rename or move made while it ran: re-apply
        # those.
        # A scan during a move can see the transcript in both folders; keep one.
        seen = set()
        sessions = [
            s
            for s in sessions
            if s.session_id not in self.load_deleted
            and not (s.session_id in seen or seen.add(s.session_id))
        ]
        for s in sessions:
            for field, value in self.load_changed.get(s.session_id, {}).items():
                setattr(s, field, value)
        if classifying is not None:
            self.classifying = classifying - self.load_deleted
        # Keep a mark unless its session is really gone; a transcript skipped on one
        # scan (e.g. mid-rewrite) mustn't silently lose it.
        old_paths = {r.session.session_id: r.session.path for r in self.rows}
        found = {s.session_id for s in sessions}
        self.marked = {
            sid
            for sid in self.marked
            if sid in found or (sid in old_paths and old_paths[sid].exists())
        }
        self.rows = [Row(s, labels) for s in sessions]
        self.loaded = True
        signature = (
            tuple(
                (
                    r.session.session_id,
                    r.session.mtime,
                    r.session.state,
                    r.session.live_status,
                    r.session.title,
                    r.group,
                    r.detail,
                )
                for r in self.rows
            ),
            frozenset(self.classifying),
            frozenset(self.marked),
        )
        if signature == self.shown_signature:
            return  # nothing changed: don't rebuild (and jump) the table
        self.shown_signature = signature
        self.render_table()

    # Table ----------------------------------------------------------------

    def visible_rows(self) -> list[Row]:
        group = self.query_one("#group", Select).value
        needle = self.query_one("#search", Input).value.strip().lower()
        out = []
        for r in self.rows:
            if self.found is not None and r.session.session_id not in self.found:
                continue
            if group == "pending" and r.group not in PENDING:
                continue
            if group not in ("all", "pending") and r.group != group:
                continue
            s = r.session
            if needle and needle not in f"{s.title} {s.cwd} {s.session_id}".lower():
                continue
            out.append(r)
        if self.sort_keys == ("relevance",):  # AI search results: the model's order
            found = self.found or {}
            return sorted(
                out, key=lambda r: found.get(r.session.session_id, (len(found), ""))[0]
            )
        # Stable sorts, least significant first, so each key orders ties in the one
        # before.
        for i, name in reversed(list(enumerate(self.sort_keys))):
            out.sort(key=SORT_KEYS[name][0], reverse=self.descending(i))
        return out

    def descending(self, i: int) -> bool:
        """Whether the i-th sort key runs descending; a second header click flips the
        primary.
        """
        return SORT_KEYS[self.sort_keys[i]][1] ^ (i == 0 and self.sort_flipped)

    def add_columns(self, table: DataTable, cells: list[tuple]) -> None:
        """Column headings marked with the sort: ^ ascending, v descending.

        With several sort keys, the marks are numbered by precedence: Status ^1, Last
        activity v2.

        Each column gets an explicit width: the widest of its cells and its heading with
        the longest mark it can get. `cells` holds every session's cells, not only the
        visible ones, so sorting, searching and filtering never resize a column or move
        the headings; widths only change when sessions are added, removed or change.
        Left to size itself, a new column starts as wide as its heading and the table
        only measures the cells once it's idle, so quick header clicks showed squashed
        frames.
        """
        for n, (key, label) in enumerate(COLUMNS):
            width = max(
                [
                    cell_len(label) + mark_room(key),
                    *(cell_len(str(row[n])) for row in cells),
                ]
            )
            heading = Text(label, justify="right" if key in RIGHT_ALIGNED else "left")
            if key in self.sort_keys:
                i = self.sort_keys.index(key)
                mark = "v" if self.descending(i) else "^"
                number = i + 1 if len(self.sort_keys) > 1 else ""
                heading.append(f" {mark}{number}", style=SORT_MARK_STYLE)
            table.add_column(
                heading,
                key=key,
                width=width,
            )

    def row_cells(self, r: Row) -> tuple:
        """One table row's cells, in COLUMNS order.

        All Text, never str: the table reads a str cell as Rich markup, so a title like
        "arr[0] = x[i]" lost its brackets and one containing "[/]" raised MarkupError.
        """
        s = r.session
        status = self.status_text(r)
        if s.session_id in self.marked:
            status = Text.assemble(("● ", "bold #f25f5c"), status)
        return (
            status,
            Text(local_time(s.started)),
            Text(local_time(s.last_timestamp)),
            Text(truncate_words(s.title)),
            Text(truncate_start(short_path(s.cwd))),
            Text(
                format_tokens(s.context_tokens),
                style=context_style(s.context_tokens),
                justify="right",
            ),
            Text(short_model(s.model)),
        )

    def render_table(self) -> None:
        table = self.query_one(DataTable)
        selected = self.current_row()
        # Where the cursor sits on screen, so the selected row can stay put after the
        # rebuild.
        screen_offset = table.cursor_row - round(table.scroll_y)
        scroll_x = table.scroll_x
        rows = self.visible_rows()
        # Every session's cells, so column widths don't depend on the search or group
        # filter.
        cells = {r.session.session_id: self.row_cells(r) for r in self.rows}
        table.clear(
            columns=True
        )  # re-add columns so the headings show the current sort
        self.add_columns(table, list(cells.values()))
        for r in rows:
            table.add_row(*cells[r.session.session_id], key=r.session.session_id)
        if selected:
            index = next(
                (
                    i
                    for i, r in enumerate(rows)
                    if r.session.session_id == selected.session.session_id
                ),
                None,
            )
            if index is not None:
                # clear() reset the scroll; restore it so the row keeps its place on
                # screen instead of being scrolled just into view at the bottom edge.
                table.move_cursor(row=index, scroll=False)
                table.scroll_to(
                    y=max(0, index - screen_offset), animate=False, immediate=True
                )
        table.scroll_to(
            x=scroll_x, animate=False, immediate=True
        )  # and don't jump sideways
        counts = {g: sum(r.group == g for r in self.rows) for g in GROUPS}
        # self.title also sets the terminal window title.
        self.title = f"Claude Code sessions: {len(rows)} shown, " + ", ".join(
            f"{GROUPS[g].lower()} {n}"
            for g, n in counts.items()
            if n and g in PENDING | {"running", "error"}
        )
        if self.found is not None:
            self.title = (
                f"AI search “{self.found_query}”: {len(self.found)} found, "
                f"{len(rows)} shown  (a = show all)"
            )
        self.update_title_bar()
        self.update_detail()

    def status_text(self, r: Row) -> Text:
        if r.group == "unclassified" and r.session.session_id in self.classifying:
            return Text("Classifying…", style="dim italic")
        return Text(GROUPS[r.group], style=GROUP_STYLE[r.group])

    def current_row(self) -> Row | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((r for r in self.rows if r.session.session_id == key), None)

    def update_detail(self) -> None:
        r = self.current_row()
        detail = self.query_one("#detail", Static)
        if not r:
            detail.update("")
            return
        s = r.session
        text = Text()
        text.append_text(self.status_text(r))
        if r.detail:
            text.append(f": {r.detail}")
        text.append(f"\n{s.title}\n", style="bold")
        text.append(f"{s.session_id}  ·  {short_path(s.cwd)}", style="dim")
        if s.model:
            text.append(f"  ·  {s.model}", style="dim")
        if self.found and s.session_id in self.found:
            text.append(f"\nAI search: {self.found[s.session_id][1]}", style="italic")
        detail.update(text)

    # Events and actions -----------------------------------------------------

    @ignore_after_quit  # a highlight queued by a rebuild can arrive after quitting
    def on_data_table_row_highlighted(self, _event) -> None:
        self.update_detail()

    def widgets_present(self) -> bool:
        """Whether the main screen still has every widget the callbacks use (False while
        quitting).
        """
        main = self.screen_stack[0] if self.screen_stack else None
        return bool(main) and all(
            main.query(w) for w in (DataTable, TitleBar, "#detail", "#group", "#search")
        )

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col = event.column_key.value
        if self.sort_keys == (col,):
            self.sort_flipped = not self.sort_flipped
        else:
            self.sort_keys, self.sort_flipped = (col,), False
        self.render_table()

    def on_select_changed(self, _event) -> None:
        self.render_table()

    def on_input_changed(self, _event) -> None:
        self.render_table()

    def on_input_submitted(self, _event) -> None:
        self.action_focus_table()

    def on_data_table_row_selected(self, _event) -> None:
        self.action_resume()

    def resumable(self) -> Session | None:
        """The selected session, if it can be resumed; otherwise None, with a notice."""
        r = self.current_row()
        if r is None:
            return None
        # Checked now, not only from the scan: it may have been opened since.
        if r.group == "running" or r.session.session_id in live_sessions(
            self.claude_home
        ):
            self.notify(
                "That session is still open in another terminal.", severity="warning"
            )
            return None
        if not r.session.cwd or not os.path.isdir(r.session.cwd):
            # claude --resume looks the session up by folder; from anywhere else it
            # either can't find it or resumes in the wrong working tree.
            self.notify(
                f"Its folder {short_path(r.session.cwd) or '(unknown)'} no longer "
                "exists, so it can't be resumed.",
                title="Can't resume",
                severity="error",
            )
            return None
        return r.session

    def action_resume(self) -> None:
        if s := self.resumable():
            self.exit(s)

    def action_open_terminal(self) -> None:
        """o: resume the session in a new terminal window, keeping the UI open."""
        if not (s := self.resumable()):
            return
        try:
            open_in_terminal(s, self.claude_home)
        except (OSError, RuntimeError) as exc:
            self.notify(
                str(exc), title="Can't open a terminal", severity="error", markup=False
            )
            return
        self.notify(
            f"Resuming {truncate_words(s.title or s.session_id, 60)} in a new terminal."
        )
        # Rescan once claude has started, so the session moves to Running and can't
        # be opened twice.
        self.set_timer(RESCAN_AFTER_OPEN, self.action_refresh)

    def action_toggle_mark(self) -> None:
        r = self.current_row()
        if r is None:
            return
        self.marked ^= {r.session.session_id}
        table = self.query_one(DataTable)
        row = table.cursor_row
        self.render_table()
        table.move_cursor(
            row=min(row + 1, table.row_count - 1)
        )  # step down, like a file manager

    def action_unmark_all(self) -> None:
        """Unmark everything. Never marks: "u to be safe" must not set up a delete."""
        if self.marked:
            self.marked.clear()
            self.render_table()

    def action_clear_marks_or_mark(self) -> None:
        """Shift+Space: unmark everything; with nothing marked, mark the current row
        like Space.
        """
        if self.marked:
            self.action_unmark_all()
        else:
            self.action_toggle_mark()

    def action_rename(self) -> None:
        r = self.current_row()
        if r is None:
            return
        if r.session.session_id in live_sessions(self.claude_home):
            self.notify(
                "That session is still open; use /rename in it.", severity="warning"
            )
            return

        def done(title: str | None) -> None:
            if title:
                self.apply_rename(r, title)

        self.push_screen(RenameSession(r.session), done)

    def apply_rename(self, r: Row, title: str) -> None:
        s = r.session
        try:
            rename_session(s, title, self.claude_home)
        except Exception as exc:
            self.notify(str(exc), title="Rename failed", severity="error")
            return
        # Renaming touched the transcript. Carry the label over to the new mtime so the
        # session isn't sent for classification again just because of its title. (A
        # batch still in flight for this session is fine: only a title line was added,
        # so its label still fits.)
        label = self.cache.get(s)
        s.title = title
        try:
            s.mtime = s.path.stat().st_mtime
        except OSError:
            # Gone between the rename and now (e.g. Claude Code cleanup). The next scan
            # drops it.
            self.render_table()
            self.notify(
                "Renamed, but the transcript has since disappeared.", severity="warning"
            )
            return
        if label:
            self.cache.put(s, label["label"], label["pending"])
            self.cache.save()
        # In case a scan in progress has the old title.
        self.load_changed.setdefault(s.session_id, {}).update(
            title=title, mtime=s.mtime
        )
        self.render_table()
        self.notify(f"Renamed to “{title}”.")

    def action_move(self) -> None:
        r = self.current_row()
        if r is None:
            return
        if r.session.session_id in live_sessions(self.claude_home):
            self.notify(
                "That session is still open; exit it first.", severity="warning"
            )
            return

        def done(dest: Path | None) -> None:
            if dest:
                self.apply_move(r, dest)

        self.push_screen(MoveSession(r.session), done)

    def apply_move(self, r: Row, dest: Path) -> None:
        # An auto-refresh while the dialog was open replaces the rows: use the current
        # one.
        r = next(
            (x for x in self.rows if x.session.session_id == r.session.session_id), r
        )
        s = r.session
        label = self.cache.get(s)
        try:
            path = move_session(s, dest, self.claude_home)
        except Exception as exc:
            self.notify(str(exc), title="Move failed", severity="error")
            return
        try:
            moved = parse_transcript(path)
        except OSError:
            moved = None
        if (
            moved is None
        ):  # gone since (e.g. Claude Code cleanup): the next scan drops it
            self.render_table()
            self.notify(
                "Moved, but the transcript has since disappeared.", severity="warning"
            )
            return
        s.path, s.cwd, s.mtime = moved.path, moved.cwd, moved.mtime
        # Only working folders changed, so the label still fits: carry it to the new
        # mtime.
        if label:
            self.cache.put(s, label["label"], label["pending"])
            self.cache.save()
        self.load_changed.setdefault(s.session_id, {}).update(
            path=s.path, cwd=s.cwd, mtime=s.mtime
        )
        self.render_table()
        # Memory belongs to a folder, shared by its sessions, so it isn't moved: say
        # which one applies.
        self.notify(
            f"Moved to {short_path(s.cwd)}. Resuming it now runs there and uses that "
            "folder's memory."
        )

    def action_delete(self) -> None:
        if self.marked:
            targets = [
                r.session for r in self.rows if r.session.session_id in self.marked
            ]
        else:
            r = self.current_row()
            targets = [r.session] if r else []
        live = live_sessions(
            self.claude_home
        )  # re-check: one may have been resumed since the scan
        running = [s for s in targets if s.session_id in live]
        targets = [s for s in targets if s.session_id not in live]
        if running:
            self.notify(
                f"Skipping {len(running)} running session(s).", severity="warning"
            )
        if not targets:
            return

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.delete_sessions(targets)

        self.push_screen(ConfirmDelete(targets), done)

    def delete_sessions(self, targets: list[Session]) -> None:
        deleted, failed, leftovers = set(), [], []
        for s in targets:
            try:
                trash_session(s, self.claude_home)
            except Exception as exc:
                if s.path.exists():
                    failed.append(f"{s.session_id[:8]}: {exc}")
                    continue
                # The transcript is in the Trash, so the session is gone even though
                # some side files weren't moved. Drop the row; keeping it would point at
                # nothing.
                leftovers.append(f"{s.session_id[:8]}: {exc}")
            deleted.add(s.session_id)
            self.cache.drop(s.session_id)
        self.cache.save()
        self.marked -= deleted
        self.classifying -= deleted
        self.load_deleted |= deleted  # in case a scan in progress still has them
        self.rows = [r for r in self.rows if r.session.session_id not in deleted]
        self.render_table()
        if deleted:
            self.notify(f"Moved {len(deleted)} session(s) to the Trash.")
        for msg in failed:
            self.notify(msg, title="Delete failed", severity="error")
        for msg in leftovers:
            self.notify(
                msg,
                title="Deleted, but some files were left behind",
                severity="warning",
                timeout=10,
            )

    def action_toggle_keys(self) -> None:
        """Ctrl+K: show or hide Textual's keys panel (the same one as Ctrl+P > Keys)."""
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_cycle_theme(self, step: int) -> None:
        """Ctrl+T, Ctrl+Y: the next or previous theme, in the palette's order."""
        names = sorted(self.available_themes)
        i = names.index(self.theme) if self.theme in names else -1
        self.theme = names[(i + step) % len(names)]
        self.show_theme_notice()

    def action_default_theme(self) -> None:
        """Ctrl+R: back to the theme the app starts with: TEXTUAL_THEME, or Textual's
        default.
        """
        self.theme = constants.DEFAULT_THEME
        self.show_theme_notice()

    def show_theme_notice(self) -> None:
        if self.theme_notice is not None:
            self.call_later(self._remove_notice, self.theme_notice)
        self.theme_notice = Notification(f"Theme: {self.theme}")
        self.post_message(Notify(self.theme_notice))

    def action_cycle_sort(self) -> None:
        """s: step through SORT_MODES. From a column-header sort, start at the first."""
        if self.sort_keys in SORT_MODES and not self.sort_flipped:
            mode = (SORT_MODES.index(self.sort_keys) + 1) % len(SORT_MODES)
        else:
            mode = 0
        self.sort_keys, self.sort_flipped = SORT_MODES[mode], False
        order = ", then ".join(COLUMN_NAMES[k] for k in self.sort_keys)
        self.notify(f"Sort {mode + 1} of {len(SORT_MODES)}: {order}.")
        self.render_table()

    def action_refresh(self) -> None:
        """r: rescan and classify, retrying failed sessions and lifting a pause if it
        works.
        """
        if self.loading:
            # A quiet auto refresh may be running unseen: queue rather than refuse.
            self.refresh_queued = True
            self.notify("Refreshing again as soon as the current scan finishes.")
            return
        self.cache.reload()
        self.start_load()

    def action_pending(self) -> None:
        self.query_one("#group", Select).value = "pending"

    def action_all(self) -> None:
        """Show every session: reset the group filter, clear the search and any AI
        search.
        """
        self.cancel_search()
        if self.found is not None:
            self.found, self.found_query = None, ""
            if self.sort_keys == ("relevance",):
                self.sort_keys, self.sort_flipped = SORT_MODES[0], False
            self.render_table()
        self.query_one("#group", Select).value = "all"
        self.query_one("#search", Input).value = ""

    def action_ai_search(self) -> None:
        """f: ask Claude to search the transcripts. Only one search runs at a time."""
        if self.searching:
            self.notify(
                f"Still searching for “{self.searching}”. Wait for it to finish, "
                "or press a to stop it.",
                severity="warning",
                markup=False,
            )
            return
        if (
            not self.loaded
        ):  # the index would be empty, and every match dropped as unknown
            self.notify(
                "Still loading the sessions; try again in a moment.", severity="warning"
            )
            return

        def done(query: str | None) -> None:
            if query and not self.searching:
                self.start_search(query)

        self.push_screen(AskSearch(self.found_query), done)

    def start_search(self, query: str) -> None:
        with self.search_lock:
            self.search_token += 1
            token = self.search_token
        self.searching = query
        self.update_title_bar()
        # Textual's notify() doesn't return the notification, so make our own to remove
        # later.
        self.search_notice = Notification(
            f"Searching the transcripts for “{query}”…",
            "AI search",
            "information",
            timeout=SEARCH_TIMEOUT + 30,
            markup=False,  # outlasts the search; removed when it ends
        )
        self.post_message(Notify(self.search_notice))
        self.run_search(token, query, [r.session for r in self.rows])

    def hide_search_notice(self) -> None:
        notice, self.search_notice = self.search_notice, None
        if notice is not None:
            # After any queued Notify for it is handled, so it can't be shown after
            # this.
            self.call_later(self._remove_notice, notice)

    def _remove_notice(self, notice: Notification) -> None:
        # Textual has no public way to remove one notification. If a Textual upgrade
        # changes these internals, the notice just stays until its timeout.
        try:
            if notice in self._notifications:
                self._unnotify(notice)
        except AttributeError, KeyError, TypeError:
            pass

    @work(thread=True, group="search")
    def run_search(self, token: int, query: str, sessions: list[Session]) -> None:
        def started(proc) -> None:
            with self.search_lock:
                if token == self.search_token:
                    self.search_proc = proc
                    return
            stop(proc)  # stopped before claude even started

        try:
            result = search(
                sessions,
                query,
                self.search_model,
                self.claude_home,
                self.search_budget,
                started,
            )
        except Exception as exc:  # including claude killed by something else: report it
            self.call_from_thread(
                self.search_failed, token, exc
            )  # ignored if we stopped it
            return
        self.call_from_thread(self.search_done, token, query, result)

    def stop_search_process(self) -> None:
        """Make any running search's results stale and stop its claude. Touches no
        widgets.
        """
        with self.search_lock:
            self.search_token += 1
            proc, self.search_proc = self.search_proc, None
        if proc is not None and proc.poll() is None:
            stop(proc)

    def cancel_search(self) -> None:
        """Stop a search in progress, if any; its results, if they still arrive, are
        dropped.
        """
        if not self.searching:
            return
        self.stop_search_process()
        self.searching = ""
        self.hide_search_notice()
        self.update_title_bar()

    @ignore_after_quit  # called from the search worker, which can finish after quitting
    def search_done(self, token: int, query: str, result) -> None:
        if token != self.search_token:
            return  # stopped
        with self.search_lock:
            self.search_proc = None
        self.searching = ""
        self.hide_search_notice()
        self.found = {m.session_id: (i, m.reason) for i, m in enumerate(result.matches)}
        self.found_query = query
        # Show every match: no group filter or text search hiding some, the model's
        # order first.
        self.query_one("#group", Select).value = "all"
        self.query_one("#search", Input).value = ""
        self.sort_keys, self.sort_flipped = ("relevance",), False
        self.render_table()
        self.query_one(DataTable).move_cursor(row=0)
        n = len(result.matches)
        msg = f"Found {n} session{'s' * (n != 1)}" if n else "No sessions matched"
        msg += f" (${result.cost_usd:.2f}). Press a to show all."
        if result.unknown:
            msg += (
                f" Ignored {len(result.unknown)} session "
                f"ID{'s' * (len(result.unknown) != 1)} not in the list."
            )
        self.notify(msg)

    @ignore_after_quit
    def search_failed(self, token: int, exc: Exception) -> None:
        if token != self.search_token:
            return
        with self.search_lock:
            self.search_proc = None
        self.searching = ""
        self.hide_search_notice()
        self.update_title_bar()
        self.notify(
            str(exc),
            title="AI search failed",
            severity="error",
            timeout=10,
            markup=False,
        )

    def on_unmount(self) -> None:
        # Don't leave claude running after quitting. The widgets are gone by now: touch
        # none.
        self.stop_search_process()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def search_themes(self) -> None:
        """The command palette's "Theme" entry, with the current theme marked."""
        self.push_screen(
            CommandPalette(
                providers=[MarkedThemeProvider], placeholder="Search for themes…"
            )
        )

    def action_focus_table(self) -> None:
        self.query_one(DataTable).focus()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cc-sessions-ui", description=__doc__)
    add_common_args(p)
    add_search_args(p)
    p.add_argument(
        "--refresh-interval",
        type=refresh_interval_arg,
        default=DEFAULT_REFRESH_INTERVAL,
        metavar="SECONDS",
        help=(
            "refresh the list automatically every SECONDS, at least 1 "
            f"(default: {DEFAULT_REFRESH_INTERVAL:g}; 0 turns it off)"
        ),
    )
    args = p.parse_args(argv)

    session = SessionsApp(
        args.claude_home,
        args.model,
        not args.no_llm,
        args.batch_size,
        args.jobs,
        args.refresh_interval,
        args.search_model,
        args.search_budget,
    ).run()
    if session is None:
        return 0
    if not session.cwd or not os.path.isdir(session.cwd):
        print(
            f"Can't resume {session.session_id}: its folder "
            f"{session.cwd or '(unknown)'} no longer exists.",
            file=sys.stderr,
        )
        return 1
    claude = shutil.which("claude")
    if not claude:
        print("claude is not on PATH", file=sys.stderr)
        return 1
    os.chdir(session.cwd)
    print(f"Resuming {session.session_id} in {short_path(os.getcwd())}…")
    os.execve(
        claude, [claude, "--resume", session.session_id], claude_env(args.claude_home)
    )


if __name__ == "__main__":
    sys.exit(main())
