"""cc-sessions-ui: a terminal UI for browsing Claude Code sessions."""

import argparse
import functools
import os
import re
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits
from textual.content import Content
from textual.css.query import NoMatches
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import ThemeProvider
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static

from .classify import DEFAULT_BATCH_SIZE, DEFAULT_JOBS, Cache, classify, error_labels
from .cli import CACHE_PATH, GROUPS, STATUS_RANK, add_common_args, detail_of, group_of, refresh_interval_arg, short_path
from .delete import trash_session
from .rename import rename_session
from .scan import Session, claude_env, live_sessions, scan

PENDING = {"decision", "action", "interrupted"}
DEFAULT_REFRESH_INTERVAL = 30.0  # seconds between automatic refreshes; 0 turns them off

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
    ("status", "Status"), ("started", "Started"), ("last", "Last activity"),
    ("title", "Title"), ("cwd", "Folder"), ("context", "Context"), ("model", "Model"),
]

# How each column sorts: key function and whether it runs descending by default.
SORT_KEYS = {
    "status": (lambda r: STATUS_RANK[r.group], False),  # needs you first
    "started": (lambda r: r.session.started, True),  # newest first
    "last": (lambda r: r.session.last_timestamp, True),  # newest first
    "title": (lambda r: r.session.title.lower(), False),
    "cwd": (lambda r: r.session.cwd, False),
    "context": (lambda r: r.session.context_tokens, True),  # largest first
    "model": (lambda r: (not r.session.model, short_model(r.session.model)), False),  # A to Z, blanks last
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
    """Width a sort mark can add to a heading: " v", plus the number in a multi-column sort."""
    numbered = any(key in mode for mode in SORT_MODES)
    return 2 + (len(str(max(map(len, SORT_MODES)))) if numbered else 0)


COLUMN_NAMES = {
    "status": "status", "started": "start", "last": "last activity", "title": "title", "cwd": "folder",
    "context": "context size", "model": "model",
}
TITLE_MAX = 50
FOLDER_MAX = 40


def truncate_start(text: str, limit: int = FOLDER_MAX) -> str:
    """Keep the end of a path, which tells folders apart: "…/my-app/.claude/skills/x"."""
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def truncate_words(text: str, limit: int = TITLE_MAX, marker: str = "...") -> str:
    """Shorten text to at most `limit` chars, cutting at a word boundary and adding `marker`.

    Spaces are preferred breaks; hyphens, underscores and slashes are used when there are
    no spaces (kebab-case titles). A single word longer than the limit is cut hard.
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


# Context size colour bands: (upper bound, exclusive; style). Each band starts where the last ends.
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
    """Model ID for the table: claude-opus-5-5 -> opus-5-5, claude-haiku-4-5-20251001 -> haiku-4-5.

    Bedrock and Vertex IDs shorten the same way; anything else is capped at MODEL_MAX.
    """
    return truncate_words(MODEL_SUFFIX.sub("", MODEL_PREFIX.sub("", model, count=1), count=1), MODEL_MAX)


def local_time(ts: str) -> str:
    """ISO UTC timestamp -> 'YYYY-MM-DD HH:MM' in local time."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
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
    TitleBar { dock: top; height: 1; background: $panel; color: $foreground; padding: 0 1; }
    TitleBar #title-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    TitleBar #title-right { width: auto; padding-left: 2; color: #f25f5c; text-style: bold; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="title-left")
        yield Static(id="title-right")

    def show(self, left: Text, right: str) -> None:
        self.query_one("#title-left", Static).update(left)
        self.query_one("#title-right", Static).update(right)


class SessionTable(DataTable):
    # Re-label DataTable's own Enter binding so it shows in the footer.
    BINDINGS = [Binding("enter", "select_cursor", "Resume")]


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
            yield Label(f"Rename session {self.session.session_id[:8]}  (Enter = save, Esc = cancel)")
            yield Input(value=self.session.title, id="new-title")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        title = " ".join(event.value.split())
        if not title:
            self.notify("The title can't be empty.", severity="warning")
            return
        self.dismiss(title if title != self.session.title else None)

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
        lines += [f"  • {truncate_words(s.title or s.session_id, 60)}" for s in self.sessions[:10]]
        if n > 10:
            lines.append(f"  … and {n - 10} more")
        lines += ["", "You can restore them from the Trash. y = delete, n / Esc = cancel"]
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
    """Drop a worker result or queued event that arrives after quitting removed the widgets.

    Only a missing widget on a torn-down screen is ignored; any other NoMatches still raises.
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
            "s", "cycle_sort", "Sort",
            tooltip="Cycle the sort order:\n1) status, last activity\n2) last activity, status\n"
            "3) folder, status, last activity\n4) last activity, folder, status",
        ),
        Binding("p", "pending", "Pending"),
        Binding("a", "all", "All"),
        Binding("/", "focus_search", "Search"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("u", "unmark_all", "Unmark all"),
        # Most terminals send plain Space for Shift+Space; this only fires where Shift is reported.
        Binding("shift+space", "clear_marks_or_mark", "Unmark all", show=False),
        Binding("n", "rename", "Rename"),
        Binding("d", "delete", "Delete"),
        Binding("k", "toggle_keys", "Keys"),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self, claude_home: Path, model: str, use_llm: bool,
        batch_size: int = DEFAULT_BATCH_SIZE, jobs: int = DEFAULT_JOBS,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    ):
        super().__init__()
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
        self.failed: dict[str, dict] = {}  # in-memory "error" labels, shown until reclassified
        # Deletes and renames made while a load is scanning, applied to its (older) results.
        self.load_deleted: set[str] = set()
        self.load_renamed: dict[str, tuple[str, float]] = {}
        self.shown_signature = None  # what show() last rendered, to skip no-change refreshes
        self.error_notified = False  # shown the "classification failed" notice this run

    def compose(self) -> ComposeResult:
        yield TitleBar()
        with Horizontal(id="filters"):
            options = [("All", "all"), ("Needs you", "pending")] + [(v, k) for k, v in GROUPS.items()]
            yield Select(options, value="all", allow_blank=False, id="group")
            yield Input(placeholder="Filter by title, folder or session ID  (/)", id="search")
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
        self.load_deleted, self.load_renamed = set(), {}
        classify_now = self.use_llm and not (auto and self.classify_paused)
        self.load(quiet, classify_now, dict(self.failed))

    def auto_refresh(self) -> None:
        """Timer tick: rescan quietly, and classify anything new or changed.

        Skipped while a load is still running, and while a dialog, the command palette or
        another screen is open, so nothing changes under the user mid-action. While
        classification is paused after an error, it rescans but doesn't call the model.
        """
        if self.loading or len(self.screen_stack) > 1:
            return
        self.cache.reload()
        self.start_load(quiet=True, auto=True)

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def load_finished(self, outcome: bool | None) -> None:
        """outcome: True if classification hit errors, False if it ran cleanly, None if it didn't run."""
        self.loading = False
        if outcome is not None:
            self.classify_paused = outcome
        self.set_status("Classification paused after an error. Press r to retry." if self.classify_paused else "")
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

    def _load(self, quiet: bool, classify_now: bool, failed: dict[str, dict]) -> bool | None:
        if not quiet:  # auto refreshes don't flash "Scanning…" every interval
            self.call_from_thread(self.set_status, "Scanning transcripts…")
        sessions = scan(self.claude_home)
        # One split, used both to mark rows "Classifying…" and to choose what to send.
        cached, todo = self.cache.partition(sessions)
        send = todo if classify_now else []
        # Sessions not being sent keep any earlier "Classification failed" label.
        labels = {**cached, **{s.session_id: failed[s.session_id] for s in todo if s.session_id in failed}}
        # Show the list straight away; unclassified rows fill in as batches come back.
        # The classifying set is handed to the main thread with it, which owns that state.
        self.call_from_thread(self.show, sessions, labels, {s.session_id for s in send})
        if not send:
            return None
        self.call_from_thread(self.set_status, f"Classifying 0/{len(send)} with {self.model}…")
        errors = []

        def on_batch(new: dict[str, dict], batch: list[Session], err: Exception | None) -> None:
            if err:
                errors.append(err)
                new = {**new, **error_labels(batch, err, skip=new)}
            self.call_from_thread(self.apply_batch, new, [s.session_id for s in batch], len(send), err)

        classify(send, self.cache, self.model, self.batch_size, self.jobs, on_batch=on_batch)
        return bool(errors)

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def apply_batch(
        self, new: dict[str, dict], batch_ids: list[str], total: int, err: Exception | None = None
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
                f"{msg}\nAuto-refresh won't classify until it works again. Press r to retry.",
                title="Classification failed", severity="error", timeout=10,
            )
        self.classifying -= set(batch_ids)
        self.set_status(f"Classifying {total - len(self.classifying)}/{total} with {self.model}…")
        self.render_table()

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def set_status(self, msg: str) -> None:
        self.sub_title = msg
        self.update_title_bar()

    def update_title_bar(self) -> None:
        left = Text(self.title)
        if self.sub_title:
            left.append(f"  —  {self.sub_title}", style="dim")
        self.query_one(TitleBar).show(left, f"{len(self.marked)} marked" if self.marked else "")

    @ignore_after_quit  # called from the load worker, which can finish after quitting
    def show(self, sessions: list[Session], labels: dict[str, dict], classifying: set[str] | None = None) -> None:
        # The scan may predate a delete or rename made while it ran: re-apply those.
        sessions = [s for s in sessions if s.session_id not in self.load_deleted]
        for s in sessions:
            if s.session_id in self.load_renamed:
                s.title, s.mtime = self.load_renamed[s.session_id]
        if classifying is not None:
            self.classifying = classifying - self.load_deleted
        # Keep a mark unless its session is really gone; a transcript skipped on one scan
        # (e.g. mid-rewrite) mustn't silently lose it.
        old_paths = {r.session.session_id: r.session.path for r in self.rows}
        found = {s.session_id for s in sessions}
        self.marked = {
            sid for sid in self.marked
            if sid in found or (sid in old_paths and old_paths[sid].exists())
        }
        self.rows = [Row(s, labels) for s in sessions]
        signature = (
            tuple((r.session.session_id, r.session.mtime, r.session.state, r.session.live_status,
                   r.session.title, r.group, r.detail) for r in self.rows),
            frozenset(self.classifying), frozenset(self.marked),
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
            if group == "pending" and r.group not in PENDING:
                continue
            if group not in ("all", "pending") and r.group != group:
                continue
            s = r.session
            if needle and needle not in f"{s.title} {s.cwd} {s.session_id}".lower():
                continue
            out.append(r)
        # Stable sorts, least significant first, so each key orders ties in the one before.
        for i, name in reversed(list(enumerate(self.sort_keys))):
            out.sort(key=SORT_KEYS[name][0], reverse=self.descending(i))
        return out

    def descending(self, i: int) -> bool:
        """Whether the i-th sort key runs descending; a second header click flips the primary."""
        return SORT_KEYS[self.sort_keys[i]][1] ^ (i == 0 and self.sort_flipped)

    def add_columns(self, table: DataTable, cells: list[tuple]) -> None:
        """Column headings marked with the sort: ^ ascending, v descending.

        With several sort keys, the marks are numbered by precedence: Status ^1, Last activity v2.

        Each column gets an explicit width: the widest of its cells and its heading with the
        longest mark it can get. `cells` holds every session's cells, not only the visible
        ones, so sorting, searching and filtering never resize a column or move the headings;
        widths only change when sessions are added, removed or change.
        Left to size itself, a new column starts as wide as its heading and the table only
        measures the cells once it's idle, so quick header clicks showed squashed frames.
        """
        for n, (key, label) in enumerate(COLUMNS):
            width = max([cell_len(label) + mark_room(key), *(cell_len(str(row[n])) for row in cells)])
            if key in self.sort_keys:
                i = self.sort_keys.index(key)
                mark = "v" if self.descending(i) else "^"
                label = f"{label} {mark}{i + 1 if len(self.sort_keys) > 1 else ''}"
            table.add_column(Text(label, justify="right") if key in RIGHT_ALIGNED else label, key=key, width=width)

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
            Text(format_tokens(s.context_tokens), style=context_style(s.context_tokens), justify="right"),
            Text(short_model(s.model)),
        )

    def render_table(self) -> None:
        table = self.query_one(DataTable)
        selected = self.current_row()
        # Where the cursor sits on screen, so the selected row can stay put after the rebuild.
        screen_offset = table.cursor_row - round(table.scroll_y)
        scroll_x = table.scroll_x
        rows = self.visible_rows()
        # Every session's cells, so column widths don't depend on the search or group filter.
        cells = {r.session.session_id: self.row_cells(r) for r in self.rows}
        table.clear(columns=True)  # re-add columns so the headings show the current sort
        self.add_columns(table, list(cells.values()))
        for r in rows:
            table.add_row(*cells[r.session.session_id], key=r.session.session_id)
        if selected:
            index = next(
                (i for i, r in enumerate(rows) if r.session.session_id == selected.session.session_id), None
            )
            if index is not None:
                # clear() reset the scroll; restore it so the row keeps its place on screen
                # instead of being scrolled just into view at the bottom edge.
                table.move_cursor(row=index, scroll=False)
                table.scroll_to(y=max(0, index - screen_offset), animate=False, immediate=True)
        table.scroll_to(x=scroll_x, animate=False, immediate=True)  # and don't jump sideways
        counts = {g: sum(r.group == g for r in self.rows) for g in GROUPS}
        # self.title also sets the terminal window title.
        self.title = f"Claude Code sessions: {len(rows)} shown, " + ", ".join(
            f"{GROUPS[g].lower()} {n}" for g, n in counts.items() if n and g in PENDING | {"running", "error"}
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
        detail.update(text)

    # Events and actions -----------------------------------------------------

    @ignore_after_quit  # a highlight queued by a rebuild can arrive after quitting
    def on_data_table_row_highlighted(self, _event) -> None:
        self.update_detail()

    def widgets_present(self) -> bool:
        """Whether the main screen still has every widget the callbacks use (False while quitting)."""
        main = self.screen_stack[0] if self.screen_stack else None
        return bool(main) and all(main.query(w) for w in (DataTable, TitleBar, "#detail", "#group", "#search"))

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

    def action_resume(self) -> None:
        r = self.current_row()
        if r is None:
            return
        if r.group == "running":
            self.notify("That session is still open in another terminal.", severity="warning")
            return
        if not r.session.cwd or not os.path.isdir(r.session.cwd):
            # claude --resume looks the session up by folder; from anywhere else it either
            # can't find it or resumes in the wrong working tree.
            self.notify(
                f"Its folder {short_path(r.session.cwd) or '(unknown)'} no longer exists, so it can't be resumed.",
                title="Can't resume", severity="error",
            )
            return
        self.exit(r.session)

    def action_toggle_mark(self) -> None:
        r = self.current_row()
        if r is None:
            return
        self.marked ^= {r.session.session_id}
        table = self.query_one(DataTable)
        row = table.cursor_row
        self.render_table()
        table.move_cursor(row=min(row + 1, table.row_count - 1))  # step down, like a file manager

    def action_unmark_all(self) -> None:
        """Unmark everything. Never marks: "u to be safe" must not set up a delete."""
        if self.marked:
            self.marked.clear()
            self.render_table()

    def action_clear_marks_or_mark(self) -> None:
        """Shift+Space: unmark everything; with nothing marked, mark the current row like Space."""
        if self.marked:
            self.action_unmark_all()
        else:
            self.action_toggle_mark()

    def action_rename(self) -> None:
        r = self.current_row()
        if r is None:
            return
        if r.session.session_id in live_sessions(self.claude_home):
            self.notify("That session is still open; use /rename in it.", severity="warning")
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
        # session isn't sent for classification again just because of its title. (A batch
        # still in flight for this session is fine: only a title line was added, so its
        # label still fits.)
        label = self.cache.get(s)
        s.title = title
        try:
            s.mtime = s.path.stat().st_mtime
        except OSError:
            # Gone between the rename and now (e.g. Claude Code cleanup). The next scan drops it.
            self.render_table()
            self.notify("Renamed, but the transcript has since disappeared.", severity="warning")
            return
        if label:
            self.cache.put(s, label["label"], label["pending"])
            self.cache.save()
        self.load_renamed[s.session_id] = (title, s.mtime)  # in case a scan in progress has the old one
        self.render_table()
        self.notify(f"Renamed to “{title}”.")

    def action_delete(self) -> None:
        if self.marked:
            targets = [r.session for r in self.rows if r.session.session_id in self.marked]
        else:
            r = self.current_row()
            targets = [r.session] if r else []
        live = live_sessions(self.claude_home)  # re-check: one may have been resumed since the scan
        running = [s for s in targets if s.session_id in live]
        targets = [s for s in targets if s.session_id not in live]
        if running:
            self.notify(f"Skipping {len(running)} running session(s).", severity="warning")
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
                # The transcript is in the Trash, so the session is gone even though some
                # side files weren't moved. Drop the row; keeping it would point at nothing.
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
            self.notify(msg, title="Deleted, but some files were left behind", severity="warning", timeout=10)

    def action_toggle_keys(self) -> None:
        """k: show or hide Textual's keys panel (the same one as Ctrl+P > Keys)."""
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

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
        """r: rescan and classify, retrying failed sessions and lifting a pause if it works."""
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
        """Show every session: reset the group filter and clear the search."""
        self.query_one("#group", Select).value = "all"
        self.query_one("#search", Input).value = ""

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def search_themes(self) -> None:
        """The command palette's "Theme" entry, with the current theme marked."""
        self.push_screen(CommandPalette(providers=[MarkedThemeProvider], placeholder="Search for themes…"))

    def action_focus_table(self) -> None:
        self.query_one(DataTable).focus()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cc-sessions-ui", description=__doc__)
    add_common_args(p)
    p.add_argument(
        "--refresh-interval", type=refresh_interval_arg, default=DEFAULT_REFRESH_INTERVAL, metavar="SECONDS",
        help=f"refresh the list automatically every SECONDS, at least 1 (default: {DEFAULT_REFRESH_INTERVAL:g}; 0 turns it off)",
    )
    args = p.parse_args(argv)

    session = SessionsApp(
        args.claude_home, args.model, not args.no_llm, args.batch_size, args.jobs, args.refresh_interval,
    ).run()
    if session is None:
        return 0
    if not session.cwd or not os.path.isdir(session.cwd):
        print(f"Can't resume {session.session_id}: its folder {session.cwd or '(unknown)'} no longer exists.", file=sys.stderr)
        return 1
    claude = shutil.which("claude")
    if not claude:
        print("claude is not on PATH", file=sys.stderr)
        return 1
    os.chdir(session.cwd)
    print(f"Resuming {session.session_id} in {short_path(os.getcwd())}…")
    os.execve(claude, [claude, "--resume", session.session_id], claude_env(args.claude_home))


if __name__ == "__main__":
    sys.exit(main())
