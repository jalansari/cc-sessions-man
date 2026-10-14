import json

from cc_sessions_man.cli import group_of
from cc_sessions_man.scan import (
    ENDED,
    MID_TASK,
    NO_REPLY,
    RUNNING,
    parse_transcript,
    scan,
)


def user(text, **kw):
    return {
        "type": "user",
        "message": {"content": text},
        "cwd": "/w",
        "timestamp": "2026-10-01T10:00:00Z",
        **kw,
    }


def assistant(content, **kw):
    return {
        "type": "assistant",
        "message": {"content": content},
        "timestamp": "2026-10-01T10:01:00Z",
        **kw,
    }


TOOL_USE = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]
TOOL_RESULT = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]


def write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def test_ended_on_text_reply(tmp_path):
    s = parse_transcript(
        write(
            tmp_path / "a.jsonl",
            [
                {"type": "custom-title", "customTitle": "my-title"},
                user("fix it"),
                assistant(TOOL_USE),
                user(TOOL_RESULT),
                assistant([{"type": "text", "text": "Shall I open a PR?"}]),
            ],
        )
    )
    assert s.state == ENDED
    assert s.title == "my-title"
    assert s.started == "2026-10-01T10:00:00Z"
    assert s.last_timestamp == "2026-10-01T10:01:00Z"
    assert s.cwd == "/w"
    assert s.last_user_prompt == "fix it"
    assert s.last_assistant_text == "Shall I open a PR?"


def test_mid_task_and_no_reply(tmp_path):
    assert (
        parse_transcript(
            write(tmp_path / "b.jsonl", [user("go"), assistant(TOOL_USE)])
        ).state
        == MID_TASK
    )
    assert (
        parse_transcript(
            write(
                tmp_path / "c.jsonl",
                [user("go"), assistant(TOOL_USE), user(TOOL_RESULT)],
            )
        ).state
        == MID_TASK
    )
    no_reply = [
        user("go"),
        assistant([{"type": "text", "text": "done"}]),
        user("and another thing"),
    ]
    assert parse_transcript(write(tmp_path / "d.jsonl", no_reply)).state == NO_REPLY


def test_ignores_sidechain_meta_commands_and_thinking(tmp_path):
    s = parse_transcript(
        write(
            tmp_path / "e.jsonl",
            [
                user("real prompt"),
                assistant([{"type": "text", "text": "All done."}]),
                assistant([{"type": "thinking", "thinking": "hmm"}]),
                user("<command-name>/cost</command-name>"),
                user("caveat", isMeta=True),
                assistant(TOOL_USE, isSidechain=True),
            ],
        )
    )
    assert s.state == ENDED
    assert s.title == "real prompt"


def test_empty_transcript_is_skipped(tmp_path):
    assert (
        parse_transcript(
            write(tmp_path / "f.jsonl", [{"type": "summary", "summary": "x"}])
        )
        is None
    )


def test_scan_marks_live_sessions(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(
        proj / "live.jsonl",
        [user("hi"), assistant([{"type": "text", "text": "hello"}])],
    )
    write(
        proj / "old.jsonl", [user("hi"), assistant([{"type": "text", "text": "hello"}])]
    )
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "1.json").write_text(
        json.dumps({"pid": 1, "sessionId": "live", "cwd": "/w", "status": "idle"})
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    states = {s.session_id: (s.state, s.live_status) for s in scan(tmp_path)}
    assert states == {"live": (RUNNING, "idle"), "old": (ENDED, "")}


def test_group_of():
    assert group_of(RUNNING, None) == "running"
    assert group_of(MID_TASK, None) == "interrupted"
    assert group_of(ENDED, "decision") == "decision"
    assert group_of(ENDED, None) == "unclassified"


def test_status_rank_order():
    from cc_sessions_man.cli import GROUPS, STATUS_RANK

    assert STATUS_RANK["running"] == 0
    assert STATUS_RANK["done"] == 99
    assert list(GROUPS) == sorted(STATUS_RANK, key=STATUS_RANK.get)


def test_truncate_words():
    from cc_sessions_man.ui import truncate_words

    assert truncate_words("short title") == "short title"
    assert truncate_words("x" * 50) == "x" * 50
    out = truncate_words(
        "Analyze failing end to end tests in the GitHub workflow for my-app"
    )
    assert out == "Analyze failing end to end tests in the GitHub..."
    assert len(out) <= 50
    # kebab-case falls back to hyphens, without leaving a trailing hyphen
    out = truncate_words("sample-system-metrics-dashboard-with-many-extra-words-here")
    assert out == "sample-system-metrics-dashboard-with-many-extra..."
    # one huge word is cut hard
    assert truncate_words("y" * 80) == "y" * 47 + "..."
    # a break exactly at the limit keeps the whole word
    assert truncate_words("a" * 47 + " bbbbbb") == "a" * 47 + "..."


def test_claude_home_from_env(monkeypatch, tmp_path):
    from pathlib import Path

    from cc_sessions_man.scan import (
        DEFAULT_CLAUDE_HOME,
        claude_env,
        claude_home_from_env,
    )

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert claude_home_from_env() == DEFAULT_CLAUDE_HOME
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "  ")
    assert claude_home_from_env() == DEFAULT_CLAUDE_HOME  # empty means unset, not "."
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-work")
    assert claude_home_from_env() == Path.home() / ".claude-work"
    # resuming against another dir must point claude at it; the default dir needs nothing
    assert claude_env(tmp_path)["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert "CLAUDE_CONFIG_DIR" not in claude_env(DEFAULT_CLAUDE_HOME)


def test_resume_command(tmp_path):
    from cc_sessions_man.cli import resume_command
    from cc_sessions_man.scan import DEFAULT_CLAUDE_HOME

    assert (
        resume_command(DEFAULT_CLAUDE_HOME, "abc", "/w")
        == "cd /w && claude --resume abc"
    )
    assert (
        resume_command(tmp_path, "abc", "/my dir")
        == f"cd '/my dir' && CLAUDE_CONFIG_DIR={tmp_path} claude --resume abc"
    )


def test_scan_cache_reuses_unchanged_transcripts(tmp_path, monkeypatch):
    import cc_sessions_man.scan as scan_mod

    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(
        proj / "a.jsonl", [user("one"), assistant([{"type": "text", "text": "done"}])]
    )
    write(proj / "b.jsonl", [user("two"), assistant(TOOL_USE)])
    cache = tmp_path / "scan.json"

    parsed = []
    real = scan_mod.parse_transcript
    monkeypatch.setattr(
        scan_mod, "parse_transcript", lambda p: parsed.append(p.stem) or real(p)
    )

    first = {s.session_id: s.state for s in scan(tmp_path, cache)}
    assert sorted(parsed) == ["a", "b"]
    parsed.clear()
    assert {s.session_id: s.state for s in scan(tmp_path, cache)} == first
    assert parsed == []  # nothing changed, nothing re-read

    write(
        proj / "b.jsonl",
        [
            user("two"),
            assistant(TOOL_USE),
            user(TOOL_RESULT),
            assistant([{"type": "text", "text": "finished"}]),
        ],
    )
    states = {s.session_id: s.state for s in scan(tmp_path, cache)}
    assert parsed == ["b"] and states["b"] == ENDED

    (proj / "a.jsonl").unlink()
    assert [s.session_id for s in scan(tmp_path, cache)] == ["b"]


def test_scan_skips_transcript_deleted_mid_scan(tmp_path, monkeypatch):
    import cc_sessions_man.scan as scan_mod

    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(
        proj / "a.jsonl", [user("one"), assistant([{"type": "text", "text": "done"}])]
    )
    write(
        proj / "b.jsonl", [user("two"), assistant([{"type": "text", "text": "done"}])]
    )

    real = scan_mod.parse_transcript

    def vanishing(path):
        if path.stem == "a":
            path.unlink()  # gone after glob() listed it
        return real(path)

    monkeypatch.setattr(scan_mod, "parse_transcript", vanishing)
    cache = tmp_path / "scan.json"
    assert [s.session_id for s in scan(tmp_path, cache)] == ["b"]
    assert list(json.loads(cache.read_text())["entries"]) == [str(proj / "b.jsonl")]


def test_relative_config_dir_is_made_absolute(tmp_path, monkeypatch):
    from pathlib import Path

    from cc_sessions_man.cli import resume_command
    from cc_sessions_man.scan import claude_env, claude_home_from_env, home_arg

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "alt-claude")
    assert claude_home_from_env() == tmp_path / "alt-claude"
    assert home_arg("./alt-claude") == tmp_path / "alt-claude"
    # Even a relative Path handed in directly comes out absolute where claude sees it.
    assert claude_env(Path("alt-claude"))["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / "alt-claude"
    )
    assert f"CLAUDE_CONFIG_DIR={tmp_path / 'alt-claude'} claude" in resume_command(
        Path("alt-claude"), "id", "/w"
    )


def test_scan_cache_is_kept_per_home(tmp_path, monkeypatch):
    import cc_sessions_man.scan as scan_mod

    homes = []
    for name in ("home-a", "home-b"):
        proj = tmp_path / name / "projects" / "-w"
        proj.mkdir(parents=True)
        write(
            proj / f"{name}.jsonl",
            [user("hi"), assistant([{"type": "text", "text": "done"}])],
        )
        homes.append(tmp_path / name)

    parsed = []
    real = scan_mod.parse_transcript
    monkeypatch.setattr(
        scan_mod, "parse_transcript", lambda p: parsed.append(p.stem) or real(p)
    )

    for home in homes + homes:  # a, b, a, b with the default cache location
        scan(home)
    assert parsed == ["home-a", "home-b"]  # second round fully cached for both
    assert scan_mod.scan_cache_path(homes[0]) != scan_mod.scan_cache_path(homes[1])


def test_live_sessions_handles_bad_files_and_other_users(tmp_path, monkeypatch):
    import errno

    from cc_sessions_man.scan import live_sessions

    d = tmp_path / "sessions"
    d.mkdir()
    (d / "list.json").write_text("[1, 2]")
    (d / "strpid.json").write_text(json.dumps({"pid": "123", "sessionId": "s"}))
    (d / "nosid.json").write_text(json.dumps({"pid": 5}))
    (d / "broken.json").write_text("{not json")
    (d / "other-user.json").write_text(json.dumps({"pid": 111, "sessionId": "theirs"}))
    (d / "dead.json").write_text(json.dumps({"pid": 222, "sessionId": "gone"}))

    def fake_kill(pid, sig):
        if pid == 111:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr("os.kill", fake_kill)
    assert set(live_sessions(tmp_path)) == {"theirs"}


def test_odd_transcript_lines_are_skipped(tmp_path):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    (proj / "odd.jsonl").write_text(
        "\n".join(
            [
                "null",
                '"just a string"',
                "[1, 2]",
                "42",
                json.dumps(
                    {
                        "type": "user",
                        "message": None,
                        "cwd": "/w",
                        "timestamp": "2026-10-01T10:00:00Z",
                    }
                ),
                json.dumps({"type": "custom-title", "customTitle": 7}),
                json.dumps(
                    {
                        "type": "user",
                        "message": {"content": "real prompt"},
                        "cwd": None,
                        "timestamp": "2026-10-01T10:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": None,
                        "timestamp": "2026-10-01T10:00:30Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": "oops",
                        "timestamp": "2026-10-01T10:00:40Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": None},
                                {"type": "text", "text": "ok"},
                            ]
                        },
                        "timestamp": "2026-10-01T10:01:00Z",
                    }
                ),
            ]
        )
        + "\n"
    )
    write(
        proj / "fine.jsonl",
        [user("hello"), assistant([{"type": "text", "text": "hi"}])],
    )

    sessions = {s.session_id: s for s in scan(tmp_path, cache_path=None)}
    assert set(sessions) == {"odd", "fine"}
    odd = sessions["odd"]
    assert (odd.state, odd.title, odd.cwd, odd.last_assistant_text) == (
        ENDED,
        "real prompt",
        "/w",
        "\nok",
    )


def test_one_unparseable_transcript_does_not_hide_the_rest(tmp_path, monkeypatch):
    import cc_sessions_man.scan as scan_mod

    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(
        proj / "a.jsonl", [user("one"), assistant([{"type": "text", "text": "done"}])]
    )
    write(
        proj / "b.jsonl", [user("two"), assistant([{"type": "text", "text": "done"}])]
    )
    real = scan_mod.parse_transcript

    def picky(path):
        if path.stem == "a":
            raise TypeError("shape we don't understand")
        return real(path)

    monkeypatch.setattr(scan_mod, "parse_transcript", picky)
    assert [s.session_id for s in scan(tmp_path, cache_path=None)] == ["b"]


def test_live_sessions_detects_reused_pid(tmp_path, monkeypatch):
    import os

    import cc_sessions_man.scan as scan_mod
    from cc_sessions_man.scan import live_sessions

    me = os.getpid()
    real_start = scan_mod._proc_start(me)
    assert real_start is not None  # running on Linux, /proc available

    d = tmp_path / "sessions"
    d.mkdir()
    (d / "same.json").write_text(
        json.dumps({"pid": me, "sessionId": "same", "procStart": real_start})
    )
    (d / "reused.json").write_text(
        json.dumps({"pid": me, "sessionId": "reused", "procStart": "1"})
    )
    (d / "old-format.json").write_text(
        json.dumps({"pid": me, "sessionId": "old-format"})
    )
    assert set(live_sessions(tmp_path)) == {"same", "old-format"}

    # Without /proc (e.g. macOS), fall back to the PID check alone.
    monkeypatch.setattr(scan_mod, "_proc_start", lambda pid: None)
    assert set(live_sessions(tmp_path)) == {"same", "reused", "old-format"}


def test_live_session_with_null_fields_keeps_transcript_cwd(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(
        proj / "live.jsonl",
        [user("hi"), assistant([{"type": "text", "text": "hello"}])],
    )
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "1.json").write_text(
        json.dumps({"pid": 1, "sessionId": "live", "cwd": None, "status": 5})
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    monkeypatch.setattr("cc_sessions_man.scan._proc_start", lambda pid: None)
    [s] = scan(tmp_path, cache_path=None)
    assert (s.state, s.cwd, s.live_status) == (RUNNING, "/w", "")


def test_concurrent_scan_cache_saves_do_not_collide(tmp_path):
    import threading

    from cc_sessions_man.scan import _save_scan_cache

    path = tmp_path / "scan.json"
    errors = []

    def saver(n):
        try:
            for i in range(200):
                _save_scan_cache(
                    path,
                    {f"/t/{n}-{i}.jsonl": {"mtime": i, "size": i, "session": None}},
                )
        except Exception as exc:  # e.g. FileNotFoundError from a shared temp name
            errors.append(exc)

    threads = [threading.Thread(target=saver, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(path.read_text())["version"] >= 1  # a complete file, not a mix
    assert list(tmp_path.glob("*.tmp")) == []


def test_malformed_scan_cache_is_ignored(tmp_path):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(
        proj / "a.jsonl", [user("one"), assistant([{"type": "text", "text": "done"}])]
    )
    cache = tmp_path / "scan.json"
    for junk in (
        "[]",
        "null",
        "7",
        json.dumps({"version": 1, "entries": []}),
        json.dumps({"version": 1, "entries": {str(proj / "a.jsonl"): "nope"}}),
    ):
        cache.write_text(junk)
        assert [s.session_id for s in scan(tmp_path, cache)] == ["a"]


def test_unparseable_transcript_is_not_reread_every_scan(tmp_path, monkeypatch):
    import cc_sessions_man.scan as scan_mod

    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    write(proj / "bad.jsonl", [user("x")])
    write(
        proj / "ok.jsonl", [user("hi"), assistant([{"type": "text", "text": "done"}])]
    )
    real = scan_mod.parse_transcript
    attempts = []

    def picky(path):
        attempts.append(path.stem)
        if path.stem == "bad":
            raise TypeError("shape we don't understand")
        return real(path)

    monkeypatch.setattr(scan_mod, "parse_transcript", picky)
    cache = tmp_path / "scan.json"
    assert [s.session_id for s in scan(tmp_path, cache)] == ["ok"]
    written = cache.stat().st_mtime_ns
    attempts.clear()
    assert [s.session_id for s in scan(tmp_path, cache)] == ["ok"]
    assert attempts == []  # neither re-read...
    assert cache.stat().st_mtime_ns == written  # ...nor the cache rewritten

    write(proj / "bad.jsonl", [user("x"), user("y")])  # the file changes: try again
    scan(tmp_path, cache)
    assert attempts == ["bad"]


def test_context_tokens_from_last_main_reply(tmp_path):
    def reply(text, usage, **kw):
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}], "usage": usage},
            "timestamp": "2026-10-01T10:01:00Z",
            **kw,
        }

    s = parse_transcript(
        write(
            tmp_path / "a.jsonl",
            [
                user("go"),
                reply(
                    "first",
                    {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 1000,
                    },
                ),
                user("more"),
                reply(
                    "second",
                    {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 306,
                        "cache_read_input_tokens": 133269,
                        "output_tokens": 1339,
                    },
                ),
                reply(
                    "subagent", {"input_tokens": 999999}, isSidechain=True
                ),  # not the main context
            ],
        )
    )
    assert (
        s.context_tokens == 2 + 306 + 133269
    )  # latest main reply; output tokens not counted

    odd = parse_transcript(
        write(
            tmp_path / "b.jsonl",
            [
                user("go"),
                reply(
                    "x",
                    {
                        "input_tokens": "12",
                        "cache_read_input_tokens": True,
                        "cache_creation_input_tokens": 5,
                    },
                ),
            ],
        )
    )
    assert odd.context_tokens == 5  # non-integers ignored

    none = parse_transcript(
        write(
            tmp_path / "c.jsonl",
            [user("go"), assistant([{"type": "text", "text": "hi"}])],
        )
    )
    assert none.context_tokens == 0


def test_format_tokens():
    from cc_sessions_man.ui import format_tokens

    assert [
        format_tokens(n) for n in (0, 850, 1234, 9950, 133577, 999500, 1_200_000)
    ] == ["", "0.8k", "1.2k", "10k", "134k", "1.0M", "1.2M"]


def test_truncate_start_keeps_the_end_of_a_path():
    from cc_sessions_man.ui import truncate_start

    assert truncate_start("~/short") == "~/short"
    long = "~/code/acme/my-app/.claude/skills/changelog-writer"
    out = truncate_start(long)
    assert len(out) == 40 and out.startswith("…") and long.endswith(out[1:])


def test_context_colour_bands():
    from cc_sessions_man.ui import context_style

    blue, green, yellow, red = "#61afef", "#4ec96b", "#f0b429", "bold #f25f5c"
    cases = {
        0: blue,
        249_999: blue,
        250_000: green,
        499_999: green,
        500_000: yellow,
        799_999: yellow,
        800_000: red,
        1_500_000: red,
    }
    assert {n: context_style(n) for n in cases} == cases


def test_model_from_last_main_reply(tmp_path):
    def reply(text, model, **kw):
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}], "model": model},
            "timestamp": "2026-10-01T10:01:00Z",
            **kw,
        }

    s = parse_transcript(
        write(
            tmp_path / "a.jsonl",
            [
                user("go"),
                reply("first", "claude-opus-5"),
                user("more"),
                reply("second", "claude-opus-5-5"),  # switched with /model
                reply(
                    "interrupted", "<synthetic>"
                ),  # Claude Code's own placeholder, not a model
                reply(
                    "subagent", "claude-haiku-4-5", isSidechain=True
                ),  # not the main thread
            ],
        )
    )
    assert s.model == "claude-opus-5-5"

    odd = parse_transcript(write(tmp_path / "b.jsonl", [user("go"), reply("x", None)]))
    assert odd.model == ""


def test_short_model():
    from cc_sessions_man.ui import short_model

    cases = {
        "claude-opus-5-5": "opus-5-5",
        "claude-haiku-4-5-20251001": "haiku-4-5",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "sonnet-4-5",  # Bedrock
        "claude-sonnet-4-5@20250929": "sonnet-4-5",  # Vertex
        "anthropic/claude-fable-5-1": "fable-5-1",  # proxy
        "": "",
    }
    assert {m: short_model(m) for m in cases} == cases
    assert len(short_model("some-other-provider-model-with-a-very-long-name")) <= 20
