import re

from rich.text import Text
from test_delete import make_home, run_ui
from textual import constants

THEME_KEYS = {"ctrl+t", "ctrl+y", "ctrl+r"}


def test_ctrl_t_cycles_themes_forwards_and_backwards(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    names, seen = [], []

    async def steps(app, pilot):
        names.extend(sorted(app.available_themes))
        seen.append(app.theme)
        for key in ("ctrl+t", "ctrl+t", "ctrl+y"):
            await pilot.press(key)
            seen.append(app.theme)

    run_ui(home, monkeypatch, steps)
    start = names.index(seen[0])
    assert seen[1:] == [names[(start + n) % len(names)] for n in (1, 2, 1)]


def test_cycling_wraps_around_the_ends(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    names, seen = [], []

    async def steps(app, pilot):
        names.extend(sorted(app.available_themes))
        app.theme = names[-1]
        await pilot.press("ctrl+t")
        seen.append(app.theme)
        await pilot.press("ctrl+y")
        seen.append(app.theme)

    run_ui(home, monkeypatch, steps)
    assert seen == [names[0], names[-1]]


def test_ctrl_r_resets_to_the_default_theme(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    seen = []

    async def steps(app, pilot):
        await pilot.press("ctrl+t")
        seen.append(app.theme)
        await pilot.press("ctrl+r")
        seen.append(app.theme)

    run_ui(home, monkeypatch, steps)
    assert seen[0] != constants.DEFAULT_THEME
    assert seen[1] == constants.DEFAULT_THEME


def test_ctrl_r_follows_textual_theme(tmp_path, monkeypatch):
    """TEXTUAL_THEME sets Textual's default theme, and Ctrl+R goes back to it."""
    home = make_home(tmp_path, ["a1"])
    monkeypatch.setattr(constants, "DEFAULT_THEME", "nord")
    seen = []

    async def steps(app, pilot):
        await pilot.press("ctrl+r")
        seen.append(app.theme)

    run_ui(home, monkeypatch, steps)
    assert seen == ["nord"]


def test_theme_notices_replace_each_other(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    toasts, theme = [], []

    async def steps(app, pilot):
        for _ in range(5):
            await pilot.press("ctrl+t")
        await pilot.pause()
        toasts.extend(str(t.render()) for t in app.screen.query("Toast"))
        theme.append(app.theme)

    run_ui(home, monkeypatch, steps, notifications=True)
    assert len(toasts) == 1 and theme[0] in toasts[0]


def test_theme_keys_are_in_the_keys_panel_but_not_the_footer(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    footer, panel = [], []

    async def steps(app, pilot):
        footer.extend(k.key for k in app.screen.query("FooterKey"))
        await pilot.press("k")
        await pilot.pause()
        with app.console.capture() as capture:
            app.console.print(app.screen.query_one("BindingsTable").render())
        panel.append(Text.from_ansi(capture.get()).plain)

    run_ui(home, monkeypatch, steps)
    assert footer and not THEME_KEYS & set(footer)
    for key, description in (
        ("t", "Next theme"),
        ("y", "Previous theme"),
        ("r", "Default theme"),
    ):
        assert re.search(rf"\^{key} +{description}", panel[0])
