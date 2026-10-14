import pytest
from rich.text import Text

import cc_sessions_man.ui as ui
from cc_sessions_man.paths import Completer, problem_span, split_path, subfolders


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home folder with Alpha/, alps/, beta/, .hidden/, a file, and Alpha/inner/."""
    for name in ("Alpha/inner", "alps", "beta", ".hidden"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "file.txt").write_text("")
    (tmp_path / "beta-link").symlink_to(tmp_path / "beta")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_split_path():
    assert split_path("") == ("~/", "")
    assert split_path("~") == ("~/", "")
    assert split_path("Doc") == ("~/", "Doc")
    assert split_path("~/") == ("~/", "")
    assert split_path("~/Documents/j") == ("~/Documents/", "j")
    assert split_path("/tmp/") == ("/tmp/", "")


def test_subfolders_lists_folders_only_and_hides_dot_folders(home):
    assert subfolders("~/", "") == [
        "Alpha",
        "alps",
        "beta",
        "beta-link",
    ]  # A to Z, ignoring case
    assert subfolders("~/", "al") == ["alps"]  # case-sensitive prefix
    assert subfolders("~/", ".") == [".hidden"]  # hidden only when asked for
    with pytest.raises(OSError):
        subfolders("~/missing/", "")


def test_tab_cycles_through_matches_and_restarts_after_an_edit(home):
    c = Completer()
    assert c.complete("") == "~/Alpha"  # empty: starts in the home folder
    assert c.complete("~/Alpha") == "~/alps"
    assert c.complete("~/alps") == "~/beta"
    assert c.complete("~/beta") == "~/beta-link"
    assert c.complete("~/beta-link") == "~/Alpha"  # wraps round
    assert c.complete("~/b") == "~/beta"  # an edit: a new round for prefix "b"
    assert c.complete("~/beta") == "~/beta-link"
    assert c.complete("~/Alpha/") == "~/Alpha/inner"  # just after "/": the prefix is ""
    assert c.complete("~/Alpha/inner") == "~/Alpha/inner"  # the only match stays
    assert c.complete("~/zz") == "~/zz"  # no match: unchanged
    assert c.complete("~/missing/x") == "~/missing/x"
    assert c.complete(str(home) + "/al") == str(home) + "/alps"  # absolute paths too


def test_problem_span_marks_the_bad_part(home):
    assert problem_span("") is None
    assert problem_span("~/") is None
    assert problem_span("~/Alpha") is None  # an exact folder name is a valid prefix
    assert problem_span("~/Alpha/") is None
    assert problem_span("~/Al") is None
    assert problem_span("~/zz") == (2, 4)  # no folder starts with "zz": the name
    assert problem_span("zz") == (0, 2)
    assert problem_span("~/missing/x") == (
        0,
        10,
    )  # the folder doesn't exist: the folder part
    assert problem_span("~/file.txt/") == (0, 11)  # a file isn't a folder
    assert problem_span("~/Alpha/inner/deeper") == (14, 20)


def test_highlighter_colours_the_bad_part_red(home):
    text = Text("~/missing/x")
    ui.PathProblems().highlight(text)
    assert [(s.start, s.end) for s in text.spans] == [(0, 10)]
    fine = Text("~/Alpha")
    ui.PathProblems().highlight(fine)
    assert fine.spans == []


def test_unknown_user_in_tilde_is_red_not_a_crash(home):
    from cc_sessions_man.paths import to_path

    text = "~no_such_user_zz/x"
    assert problem_span(text) == (
        0,
        len("~no_such_user_zz/"),
    )  # used to raise RuntimeError
    assert Completer().complete(text) == text
    assert to_path(text) is None


def test_dot_and_dotdot_are_valid(home):
    assert problem_span("~/Alpha/..") is None
    assert problem_span("~/Alpha/.") is None
    assert problem_span("~/Alpha/.x") == (
        8,
        10,
    )  # other hidden names still need a match


def test_unreadable_folder_is_red_even_for_a_bare_name(home):
    home.chmod(0o300)  # can enter, can't list
    try:
        assert problem_span("foo") == (0, 3)  # used to be an empty span: nothing red
        assert problem_span("~/foo") == (0, 2)
    finally:
        home.chmod(0o700)


def test_to_path_treats_a_bare_name_as_in_home_like_completion(
    home, tmp_path_factory, monkeypatch
):
    from cc_sessions_man.paths import to_path

    elsewhere = tmp_path_factory.mktemp("cwd")
    (elsewhere / "Alpha").mkdir()
    monkeypatch.chdir(elsewhere)  # a same-named folder where the app was started
    assert to_path("Alpha") == home / "Alpha"
    assert to_path("~/Alpha/") == home / "Alpha"
    assert to_path("  ~/beta  ") == home / "beta"


def test_highlighter_checks_the_disk_once_per_text(home, monkeypatch):
    calls = []
    monkeypatch.setattr(ui, "problem_span", lambda t: calls.append(t) or None)
    h = ui.PathProblems()
    for text in ("~/a", "~/a", "~/a", "~/ab", "~/ab"):  # e.g. cursor blinks re-render
        h.highlight(Text(text))
    assert calls == ["~/a", "~/ab"]


def test_retyping_the_filled_text_starts_a_new_round(home):
    from textual.app import App

    class One(App):
        def compose(self):
            yield ui.PathInput(id="p")

    async def go():
        app = One()
        async with app.run_test() as pilot:
            field = app.query_one(ui.PathInput)
            field.focus()
            await pilot.press("tab", "tab")
            assert field.value == "~/alps"
            await pilot.press("backspace", "s")  # an edit that ends on the same text
            await pilot.press("tab")
            assert field.value == "~/alps"  # a new round for "alps", not on to beta

    import asyncio

    asyncio.run(go())
