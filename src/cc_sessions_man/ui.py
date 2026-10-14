"""cc-sessions-ui: a terminal UI for browsing Claude Code sessions."""

import argparse
import os
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits
from textual.content import Content
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import ThemeProvider
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static

from .classify import DEFAULT_BATCH_SIZE, DEFAULT_JOBS, Cache, classify, error_labels
from .cli import CACHE_PATH, GROUPS, STATUS_RANK, add_common_args, detail_of, group_of, short_path
from .delete import trash_session
from .rename import rename_session
from .scan import Session, claude_env, live_sessions, scan

PENDING = {"decision", "action", "interrupted"}

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

COLUMNS = [("status", "Status"), ("started", "Started"), ("last", "Last activity"), ("title", "Title"), ("cwd", "Folder")]

# How each column sorts: key function and whether it runs descending by default.
SORT_KEYS = {
    "status": (lambda r: STATUS_RANK[r.group], False),  # needs you first
    "started": (lambda r: r.session.started, True),  # newest first
    "last": (lambda r: r.session.last_timestamp, True),  # newest first
    "title": (lambda r: r.session.title.lower(), False),
    "cwd": (lambda r: r.session.cwd, False),
}

# The orders the s key cycles through: (primary, secondary, tertiary).
SORT_MODES = [
    ("status", "last"),
    ("last", "status"),
    ("cwd", "status", "last"),
    ("last", "cwd", "status"),
]
COLUMN_NAMES = {"status": "status", "started": "start", "last": "last activity", "title": "title", "cwd": "folder"}
TITLE_MAX = 50


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
        Binding("escape", "focus_table", "Table", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self, claude_home: Path, model: str, use_llm: bool,
        batch_size: int = DEFAULT_BATCH_SIZE, jobs: int = DEFAULT_JOBS,
    ):
        super().__init__()
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
        self.add_columns(table)
        table.focus()
        self.start_load()

    # Data loading ---------------------------------------------------------

    def start_load(self) -> None:
        # Only one load at a time: a thread worker can't be cancelled while it waits on
        # claude, so a second run would classify the same sessions twice.
        self.loading = True
        self.error_notified = False
        self.load()

    def load_finished(self) -> None:
        self.loading = False
        self.set_status("")

    @work(thread=True)
    def load(self) -> None:
        try:
            self._load()
        finally:
            self.call_from_thread(self.load_finished)

    def _load(self) -> None:
        self.call_from_thread(self.set_status, "Scanning transcripts…")
        sessions = scan(self.claude_home)
        # One split, used both to mark rows "Classifying…" and to choose what to send.
        cached, todo = self.cache.partition(sessions)
        # Show the list straight away; unclassified rows fill in as batches come back.
        # The classifying set is handed to the main thread with it, which owns that state.
        queued = {s.session_id for s in todo} if self.use_llm else set()
        self.call_from_thread(self.show, sessions, cached, queued)
        if not (self.use_llm and todo):
            return
        self.call_from_thread(self.set_status, f"Classifying 0/{len(todo)} with {self.model}…")

        def on_batch(new: dict[str, dict], batch: list[Session], err: Exception | None) -> None:
            if err:
                new = {**new, **error_labels(batch, err, skip=new)}
            self.call_from_thread(self.apply_batch, new, [s.session_id for s in batch], len(todo), err)

        classify(todo, self.cache, self.model, self.batch_size, self.jobs, on_batch=on_batch)

    def apply_batch(
        self, new: dict[str, dict], batch_ids: list[str], total: int, err: Exception | None = None
    ) -> None:
        for r in self.rows:
            if r.session.session_id in new:
                r.update(new)
        if err and not self.error_notified:
            self.error_notified = True  # once per run; the rows carry the detail
            msg = next(iter(new.values()))["pending"] if new else str(err)
            self.notify(f"{msg}\nPress r to retry when it finishes.", title="Classification failed", severity="error", timeout=10)
        self.classifying -= set(batch_ids)
        self.set_status(f"Classifying {total - len(self.classifying)}/{total} with {self.model}…")
        self.render_table()

    def set_status(self, msg: str) -> None:
        self.sub_title = msg
        self.update_title_bar()

    def update_title_bar(self) -> None:
        left = Text(self.title)
        if self.sub_title:
            left.append(f"  —  {self.sub_title}", style="dim")
        self.query_one(TitleBar).show(left, f"{len(self.marked)} marked" if self.marked else "")

    def show(self, sessions: list[Session], labels: dict[str, dict], classifying: set[str] | None = None) -> None:
        if classifying is not None:
            self.classifying = classifying
        self.rows = [Row(s, labels) for s in sessions]
        self.marked &= {s.session_id for s in sessions}
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
            fn, descending = SORT_KEYS[name]
            out.sort(key=fn, reverse=descending ^ (i == 0 and self.sort_flipped))
        return out

    def add_columns(self, table: DataTable) -> None:
        """Column headings, with ^1 / ^2 / ^3 on the primary, secondary and tertiary sort."""
        for key, label in COLUMNS:
            if key in self.sort_keys:
                label = f"{label} ^{self.sort_keys.index(key) + 1}"
            table.add_column(label, key=key)

    def render_table(self) -> None:
        table = self.query_one(DataTable)
        selected = self.current_row()
        # Where the cursor sits on screen, so the selected row can stay put after the rebuild.
        screen_offset = table.cursor_row - round(table.scroll_y)
        table.clear(columns=True)  # re-add columns so the headings show the current sort
        self.add_columns(table)
        rows = self.visible_rows()
        for r in rows:
            s = r.session
            status = self.status_text(r)
            if s.session_id in self.marked:
                status = Text.assemble(("● ", "bold #f25f5c"), status)
            table.add_row(
                status,
                local_time(s.started),
                local_time(s.last_timestamp),
                truncate_words(s.title),
                short_path(s.cwd),
                key=s.session_id,
            )
        if selected:
            index = next(
                (i for i, r in enumerate(rows) if r.session.session_id == selected.session.session_id), None
            )
            if index is not None:
                # clear() reset the scroll; restore it so the row keeps its place on screen
                # instead of being scrolled just into view at the bottom edge.
                table.move_cursor(row=index, scroll=False)
                table.scroll_to(y=max(0, index - screen_offset), animate=False, immediate=True)
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
        detail.update(text)

    # Events and actions -----------------------------------------------------

    def on_data_table_row_highlighted(self, _event) -> None:
        self.update_detail()

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
        self.rows = [r for r in self.rows if r.session.session_id not in deleted]
        self.render_table()
        if deleted:
            self.notify(f"Moved {len(deleted)} session(s) to the Trash.")
        for msg in failed:
            self.notify(msg, title="Delete failed", severity="error")
        for msg in leftovers:
            self.notify(msg, title="Deleted, but some files were left behind", severity="warning", timeout=10)

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
        if self.loading:
            self.notify("Still scanning or classifying. Refresh when it finishes.", severity="warning")
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
    args = p.parse_args(argv)

    session = SessionsApp(args.claude_home, args.model, not args.no_llm, args.batch_size, args.jobs).run()
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
