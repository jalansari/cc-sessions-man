import json
import threading

import pytest
from test_delete import make_home, run_ui

import cc_sessions_man.cli as cli
import cc_sessions_man.search as search_mod
import cc_sessions_man.ui as ui
from cc_sessions_man.scan import scan
from cc_sessions_man.search import SearchStopped, build_prompt, call_claude, search


def fake_search(matches, cost=0.12, calls=None):
    def run(prompt, model, projects, budget, on_start=None):
        if calls is not None:
            calls.append(
                {
                    "prompt": prompt,
                    "model": model,
                    "projects": projects,
                    "budget": budget,
                }
            )
        return matches, cost

    return run


def test_search_keeps_the_models_order_and_drops_unknown_and_repeated_ids(
    tmp_path, monkeypatch
):
    home = make_home(tmp_path, ["aaa", "bbb", "ccc"])
    sessions = scan(home, cache_path=None)
    calls = []
    monkeypatch.setattr(
        search_mod,
        "_run_search",
        fake_search(
            [
                {"session_id": "ccc", "reason": "first   used\n here"},
                {"session_id": "made-up", "reason": "?"},
                {
                    "session_id": "aaa.jsonl",
                    "reason": "also",
                },  # file name instead of ID
                {"session_id": "ccc", "reason": "again"},
                {"nonsense": True},
            ],
            calls=calls,
        ),
    )
    result = search(sessions, "  first   use  ", "sonnet", home, 0.5)
    assert [(m.session_id, m.reason) for m in result.matches] == [
        ("ccc", "first used here"),
        ("aaa", "also"),
    ]
    assert result.unknown == ["made-up"] and result.cost_usd == 0.12
    assert calls[0]["projects"] == home / "projects" and calls[0]["budget"] == 0.5
    assert calls[0]["prompt"].startswith("Query: first use\n")
    with pytest.raises(ValueError):
        search(sessions, "   ", "sonnet", home)


def test_prompt_indexes_every_session(tmp_path):
    home = make_home(tmp_path, ["aaa", "bbb"])
    prompt = build_prompt(scan(home, cache_path=None), "x")
    assert "aaa | 2026-10-01T00:00 |" in prompt and "| /w | prompt bbb" in prompt


class FakePopen:
    """Stands in for claude: records how it was started and replies with `out`."""

    last = None

    def __init__(self, cmd, cwd, **kw):
        self.cmd, self.cwd, self.kw, self.returncode = cmd, cwd, kw, FakePopen.code
        FakePopen.last = self

    def communicate(self, prompt=None, timeout=None):
        return FakePopen.out, "boom on stderr"

    def poll(self):
        return self.returncode


def run_fake(monkeypatch, out, code=0):
    FakePopen.out, FakePopen.code = (
        out if isinstance(out, str) else json.dumps(out)
    ), code
    monkeypatch.setattr(search_mod.subprocess, "Popen", FakePopen)
    return lambda: call_claude("prompt", "sonnet", search_mod.Path("/h/projects"), 0.5)


def test_claude_is_run_read_only_from_an_empty_folder(monkeypatch):
    call = run_fake(
        monkeypatch, {"type": "result", "structured_output": {"matches": []}}
    )
    call()
    cmd, cwd = FakePopen.last.cmd, FakePopen.last.cwd
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--add-dir") + 1] == "/h/projects"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.5"
    assert {"--no-session-persistence", "--strict-mcp-config"} <= set(cmd)
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    system = cmd[cmd.index("--system-prompt") + 1]
    assert (
        "/h/projects" in system
        and "Only sessions in the index you're given count" in system
    )
    assert cwd != "/h/projects"  # a temporary folder
    assert FakePopen.last.kw[
        "start_new_session"
    ]  # its own group: stopping it stops its ripgrep too


def test_claude_errors_become_readable_messages(monkeypatch):
    with pytest.raises(
        RuntimeError, match=r"reached its \$0.5 budget \(spent \$0.62\)"
    ):
        run_fake(
            monkeypatch,
            {
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "total_cost_usd": 0.62,
            },
        )()
    with pytest.raises(RuntimeError, match="Not logged in"):
        run_fake(monkeypatch, {"is_error": True, "result": "Not logged in"}, code=1)()
    with pytest.raises(RuntimeError, match="boom on stderr"):
        run_fake(monkeypatch, "not json", code=1)()
    with pytest.raises(SearchStopped, match=r"stopped \(signal 9\)"):
        run_fake(
            monkeypatch, "", code=-9
        )()  # killed, by us or e.g. the out-of-memory killer
    monkeypatch.setattr(
        search_mod, "call_claude", lambda *a: {"structured_output": None}
    )
    with pytest.raises(RuntimeError, match="didn't return"):
        search_mod._run_search("p", "m", search_mod.Path("/h"), 1)


def start(app, pilot, query):
    """Open the AI search dialog, type `query`, press Enter and wait for its result."""

    async def go():
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#ai-query", ui.Input).value = query
        await pilot.press("enter")
        for _ in range(200):  # only this search: another may be blocked on purpose
            await pilot.pause(0.01)
            if not app.searching:
                break
        await pilot.pause()

    return go()


def test_ui_shows_only_matches_in_the_models_order_with_reasons(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb", "ccc"])
    monkeypatch.setattr(
        search_mod,
        "_run_search",
        fake_search(
            [
                {"session_id": "aaa", "reason": "the oldest"},
                {"session_id": "ccc", "reason": "newest"},
            ]
        ),
    )

    async def steps(app, pilot):
        app.query_one("#search", ui.Input).value = (
            "zzz"  # a text filter that would hide both
        )
        await start(app, pilot, "billing")
        assert [r.session.session_id for r in app.visible_rows()] == [
            "aaa",
            "ccc",
        ]  # relevance, not date
        assert app.title.startswith("AI search “billing”: 2 found")
        assert app.query_one("#search", ui.Input).value == ""
        assert "AI search: the oldest" in str(
            app.query_one("#detail", ui.Static).render()
        )
        # A refresh rebuilding the rows keeps the results.
        app.show(
            scan(home, cache_path=None), {"bbb": {"label": "decision", "pending": "?"}}
        )
        assert [r.session.session_id for r in app.visible_rows()] == ["aaa", "ccc"]
        await pilot.press("s")  # sorting still works on the results
        assert {r.session.session_id for r in app.visible_rows()} == {"aaa", "ccc"}
        await pilot.press("a")  # back to everything, in the default sort
        await pilot.pause()
        assert (
            len(app.visible_rows()) == 3
            and app.found is None
            and app.sort_keys == ui.SORT_MODES[0]
        )

    run_ui(home, monkeypatch, steps)


def test_ui_reports_a_failed_search_and_keeps_the_list(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb"])

    def fail(*a):
        raise RuntimeError("Not logged in")

    monkeypatch.setattr(search_mod, "_run_search", fail)
    notes = []

    async def steps(app, pilot):
        monkeypatch.setattr(
            app, "notify", lambda msg, **kw: notes.append((msg, kw.get("severity")))
        )
        await start(app, pilot, "anything")
        assert app.found is None and len(app.visible_rows()) == 2 and not app.searching
        assert notes == [("Not logged in", "error")]

    run_ui(home, monkeypatch, steps)


def blocked_search(monkeypatch):
    """A search that runs until the returned event is set."""
    release = threading.Event()

    def slow(prompt, model, projects, budget, on_start=None):
        release.wait(5)
        return [{"session_id": "bbb", "reason": "found it"}], 0.1

    monkeypatch.setattr(search_mod, "_run_search", slow)
    return release


def notices(app):
    return [n.message for n in app._notifications]


async def begin(app, pilot, query):
    await pilot.press("f")
    await pilot.pause()
    app.screen.query_one("#ai-query", ui.Input).value = query
    await pilot.press("enter")
    await pilot.pause()


def test_ui_shows_a_notice_until_the_search_ends_and_blocks_f(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb"])
    release = blocked_search(monkeypatch)

    async def steps(app, pilot):
        await begin(app, pilot, "billing")
        searching = "Searching the transcripts for “billing”…"
        assert searching in notices(app)
        await pilot.pause(0.2)
        assert searching in notices(app)  # stays up: no ordinary timeout
        await pilot.press("f")  # blocked while it runs
        await pilot.pause()
        assert not isinstance(app.screen, ui.AskSearch)
        assert any(m.startswith("Still searching for “billing”") for m in notices(app))
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert searching not in notices(app) and list(app.found) == ["bbb"]
        await pilot.press("f")  # free again once it's done
        await pilot.pause()
        assert isinstance(app.screen, ui.AskSearch)
        await pilot.press("escape")
        await pilot.pause()

    run_ui(home, monkeypatch, steps)


def test_ui_stopping_a_search_removes_its_notice_and_drops_its_results(
    tmp_path, monkeypatch
):
    home = make_home(tmp_path, ["aaa", "bbb"])
    release = blocked_search(monkeypatch)

    async def steps(app, pilot):
        await begin(app, pilot, "billing")
        await pilot.press("a")
        await pilot.pause()
        assert not any("billing" in m for m in notices(app)) and not app.searching
        release.set()  # its results arrive anyway: dropped
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.found is None and len(app.visible_rows()) == 2

    run_ui(home, monkeypatch, steps)


class Proc:
    """A running claude process, as far as the UI can tell."""

    def poll(self):
        return None


def record_stops(monkeypatch):
    stopped = []
    monkeypatch.setattr(ui, "stop", lambda proc, *a: stopped.append(proc))
    return stopped


def test_ui_stops_the_claude_process_when_cancelled(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    stopped = record_stops(monkeypatch)

    async def steps(app, pilot):
        proc = Proc()
        app.searching, app.search_proc = "q", proc
        await pilot.press("a")
        assert stopped == [proc] and not app.searching and app.search_proc is None

    run_ui(home, monkeypatch, steps)


def test_quitting_during_a_search_stops_claude_without_errors(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb"])
    release = threading.Event()
    stopped = []
    monkeypatch.setattr(
        ui, "stop", lambda p, *a: (stopped.append(p), release.set())
    )  # it ends claude
    proc = Proc()

    def slow(prompt, model, projects, budget, on_start=None):
        on_start(proc)
        release.wait(5)
        return [], 0.0

    monkeypatch.setattr(search_mod, "_run_search", slow)

    async def steps(app, pilot):
        await begin(app, pilot, "billing")
        assert app.search_proc is proc
        # run_ui then leaves the app: Unmount runs with the widgets already gone.

    run_ui(home, monkeypatch, steps)  # used to raise NoMatches from on_unmount
    assert stopped == [proc]


def test_a_search_stopped_as_claude_starts_still_stops_it(tmp_path, monkeypatch):
    # The worker hands over the process just as the UI stops the search: claude must not
    # be left running unseen.
    home = make_home(tmp_path, ["aaa"])
    stopped = record_stops(monkeypatch)
    proc = Proc()

    async def steps(app, pilot):
        def racing(prompt, model, projects, budget, on_start=None):
            app.stop_search_process()  # the UI stops it first (thread-safe, no widgets)...
            on_start(proc)  # ...then claude starts
            return [], 0.0

        monkeypatch.setattr(search_mod, "_run_search", racing)
        await begin(app, pilot, "q")
        await app.workers.wait_for_complete()
        assert stopped == [proc] and app.search_proc is None

    run_ui(home, monkeypatch, steps)


def test_claude_killed_by_something_else_ends_the_search(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])

    def killed(*a, **k):
        raise SearchStopped("claude was stopped (signal 9) before it finished.")

    monkeypatch.setattr(search_mod, "_run_search", killed)

    async def steps(app, pilot):
        await begin(app, pilot, "q")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.searching and not any(
            m.startswith("Searching") for m in notices(app)
        )
        assert any("signal 9" in m for m in notices(app))
        await pilot.press("f")  # not stuck
        await pilot.pause()
        assert isinstance(app.screen, ui.AskSearch)
        await pilot.press("escape")
        await pilot.pause()

    run_ui(home, monkeypatch, steps)


def test_f_waits_for_the_first_scan(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])

    async def steps(app, pilot):
        app.loaded = False  # as just after launch
        await pilot.press("f")
        await pilot.pause()
        assert not isinstance(app.screen, ui.AskSearch)
        assert any(m.startswith("Still loading") for m in notices(app))

    run_ui(home, monkeypatch, steps)


def test_cli_search_prints_matches(tmp_path, monkeypatch, capsys):
    home = make_home(tmp_path, ["aaa", "bbb"])
    monkeypatch.setattr(
        search_mod,
        "_run_search",
        fake_search([{"session_id": "bbb", "reason": "it's this one"}]),
    )
    assert cli.main(["--claude-home", str(home), "--search", "q"]) == 0
    out = capsys.readouterr().out
    assert "bbb" in out and "it's this one" in out and "aaa" not in out
    assert cli.main(["--claude-home", str(home), "--search", "q", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["session_id"], r["reason"]) for r in rows] == [("bbb", "it's this one")]
    assert rows[0]["resume"].endswith("claude --resume bbb")


@pytest.mark.parametrize(
    "error",
    [RuntimeError("Not logged in"), SearchStopped("claude was stopped (signal 9)")],
)
def test_cli_search_failure_exits_1(tmp_path, monkeypatch, capsys, error):
    home = make_home(tmp_path, ["aaa"])

    def fail(*a):
        raise error

    monkeypatch.setattr(search_mod, "_run_search", fail)
    assert cli.main(["--claude-home", str(home), "--search", "q"]) == 1
    assert f"AI search failed: {error}" in capsys.readouterr().err
