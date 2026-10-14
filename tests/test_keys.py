import re

from test_delete import footer_and_keys_panel, make_home, run_ui

import cc_sessions_man.ui as ui


def test_j_and_k_move_like_down_and_up(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1", "b2", "c3"])
    rows = []

    async def steps(app, pilot):
        table = app.query_one(ui.DataTable)
        for key in ("j", "j", "k", "down", "up"):
            await pilot.press(key)
            rows.append(table.cursor_row)

    run_ui(home, monkeypatch, steps)
    assert rows == [1, 2, 1, 2, 1]


def test_h_and_l_scroll_like_left_and_right(tmp_path, monkeypatch):
    """With a row cursor, left and right scroll the table sideways."""
    home = make_home(tmp_path, ["a1"])
    seen = []

    async def steps(app, pilot):
        table = app.query_one(ui.DataTable)
        for key in ("l", "l", "h", "right", "left"):
            await pilot.press(key)
            await pilot.pause()
            seen.append(table.scroll_target_x)

    run_ui(home, monkeypatch, steps, size=(40, 20))  # narrower than the table
    assert seen[0] > 0 and seen[1] > seen[0]
    assert seen[2] == seen[0] and seen[3] == seen[1] and seen[4] == seen[0]


def test_vim_keys_are_in_the_keys_panel_but_not_the_footer(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    seen = []

    async def steps(app, pilot):
        seen.append(await footer_and_keys_panel(app, pilot))

    run_ui(home, monkeypatch, steps)
    footer, panel = seen[0]
    assert footer and not {"h", "j", "k", "l", "ctrl+k"} & footer
    for keys, description in (
        ("↑ k", "Cursor up"),
        ("↓ j", "Cursor down"),
        ("← h", "Cursor left"),
        ("→ l", "Cursor right"),
        (r"\^k", "Keys"),
    ):
        assert re.search(rf"{keys} +{description}", panel), (keys, panel)
