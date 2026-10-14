from test_delete import make_home, run_ui
from textual.widgets._footer import FooterKey
from textual.widgets._key_panel import BindingsTable

import cc_sessions_man.ui as ui

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
    assert seen[0] != ui.DEFAULT_THEME
    assert seen[1] == ui.DEFAULT_THEME == "textual-dark"


def test_theme_keys_are_in_the_keys_panel_but_not_the_footer(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    footer, panel = [], []

    async def steps(app, pilot):
        footer.extend(k.key for k in app.screen.query(FooterKey))
        await pilot.press("k")
        await pilot.pause()
        with app.console.capture() as capture:
            app.console.print(app.screen.query_one(BindingsTable).render())
        panel.append(capture.get())

    run_ui(home, monkeypatch, steps)
    assert footer and not THEME_KEYS & set(footer)
    for description in ("Next theme", "Previous theme", "Default theme"):
        assert description in panel[0]
