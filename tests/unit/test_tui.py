import os
from pathlib import Path

import pytest

from fakes import NOW, FakePane, FakeTmuxServer, record
from sebside.config import Config
from sebside.model import OPTION_ID, State
from sebside.render import to_plain
from sebside.store import Store
from sebside.tmux import Tmux
from sebside.tui import App, decode_keys, paint, session_name, split_pending


@pytest.fixture
def app(server: FakeTmuxServer, tmux: Tmux, store: Store) -> App:
    return App(Config(resume_programs=("agent",)), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False)


def test_decode_keys():
    assert decode_keys("\x1b[A\x1b[Bjk\r?q\x1bxyz") == [
        "up",
        "down",
        "down",
        "up",
        "enter",
        "explain",
        "quit",
        "esc",
        "yes",
    ]
    assert decode_keys("\x1bOA") == ["up"]


def test_session_name_is_safe_and_unique():
    assert session_name("my.job: build", set()) == "my-job-build"
    assert session_name("x", {"x", "x-2"}) == "x-3"
    assert session_name("...", set()) == "resumed"


def test_refresh_builds_board_and_selects_first(server, app, store):
    server.panes += [FakePane("%1", "api", options={OPTION_ID: "a"}), FakePane("%2", "web")]
    store.write(record("a", State.WAITING, title="Deploy"))
    app.refresh()
    assert [card.title for card in app.board.live] == ["Deploy", "web"]
    assert app.selected_key == "card:a"


def test_refresh_reconciles_vanished_cards_into_history(server, app, store):
    server.panes.append(FakePane("%2", "web"))
    store.write(record("gone", title="old", pane_id="%1"))
    app.refresh()
    loaded = store.load("gone")
    assert loaded is not None and loaded.closed_at == NOW
    assert [card.card_id for card in app.board.history] == ["gone"]


def test_visible_done_card_becomes_seen(server, app, store):
    server.panes.append(FakePane("%1", "api", attached=True, options={OPTION_ID: "d"}))
    store.write(record("d", State.DONE, rev=2))
    app.refresh()
    loaded = store.load("d")
    assert loaded is not None and loaded.seen_rev == 2
    assert app.board.live[0].state is State.QUIET


def test_keys_move_and_quit(server, app):
    server.panes += [FakePane("%1", "a"), FakePane("%2", "b"), FakePane("%3", "c")]
    app.refresh()
    app.handle_key("down")
    app.handle_key("down")
    app.handle_key("down")
    assert app.selected_key == "pane:%3"
    app.handle_key("top")
    assert app.selected_key == "pane:%1"
    assert app.handle_key("quit") is False


def test_enter_switches_to_live_card_and_never_types(server, app):
    server.panes.append(FakePane("%1", "api", window="3"))
    app.refresh()
    app.handle_key("enter")
    assert server.commands()[-3:] == ["switch-client", "select-window", "select-pane"]
    assert "send-keys" not in server.commands()
    assert app.message == ""


def test_enter_with_focus_main_lands_in_the_main_pane(server, app):
    from sebside.model import OPTION_ROLE

    server.tmux_options["@sebside-focus"] = "main"
    server.panes += [FakePane("%1", "api", options={OPTION_ROLE: "sidebar"}), FakePane("%2", "api")]
    app.refresh()
    app.selected_key = next(card.key for card in app.board.cards if card.session == "api")
    app.handle_key("enter")
    assert server.calls[-1][-2:] == ["-t", "%2"]
    assert server.commands()[-1] == "select-pane"


def test_explain_toggles(server, app):
    server.panes.append(FakePane("%1", "api"))
    app.refresh()
    app.handle_key("explain")
    assert app.mode == "explain"
    assert any("Why: api" in to_plain(r) for r in app.rows(36, 20))
    app.handle_key("esc")
    assert app.mode == "list"


def _closed_app(server: FakeTmuxServer, app: App, store: Store, argv: tuple[str, ...]) -> None:
    server.panes.append(FakePane("%2", "web"))
    store.write(record("c1", title="agent one", closed_at=NOW - 10, resume=argv, cwd="/"))
    app.refresh()
    app.selected_key = "closed:c1"


def test_resume_asks_first_then_runs_argv_in_new_session(server, app, store):
    _closed_app(server, app, store, ("agent", "--resume", "c1"))
    app.handle_key("enter")
    assert app.mode == "confirm"
    assert "new-session" not in server.commands()
    app.handle_key("enter")
    new = next(argv for argv in server.calls if "new-session" in argv)
    assert new[new.index("--") + 1 :] == ["agent", "--resume", "c1"]
    assert new[new.index("-s") + 1] == "agent-one"
    assert app.message == "resumed in session agent-one"


def test_resume_cancel(server, app, store):
    _closed_app(server, app, store, ("agent",))
    app.handle_key("enter")
    app.handle_key("esc")
    assert app.mode == "list" and "new-session" not in server.commands()


def test_resume_refused_for_program_not_in_allowlist(server, app, store):
    _closed_app(server, app, store, ("bash", "-c", "rm -rf ~"))
    app.handle_key("enter")
    app.handle_key("enter")
    assert "new-session" not in server.commands()
    assert "resume_programs" in app.message


def test_resume_refused_when_file_is_not_private(server, app, store):
    _closed_app(server, app, store, ("agent",))
    os.chmod(store.path("c1"), 0o644)
    app.handle_key("enter")
    app.handle_key("enter")
    assert "new-session" not in server.commands()
    assert "refused" in app.message


def test_closed_card_with_live_twin_switches_instead(server, app, store):
    server.panes.append(FakePane("%1", "api", options={OPTION_ID: "a"}))
    store.write(record("a"))
    app.refresh()
    from dataclasses import replace

    twin = replace(app.board.live[0], key="closed:a", live=False)
    app.board.history.append(twin)
    app.selected_key = "closed:a"
    app.handle_key("enter")
    assert "switch-client" in server.commands() and "new-session" not in server.commands()


def test_paint_moves_home_and_clears_line_ends(server, app):
    server.panes.append(FakePane("%1", "api"))
    app.refresh()
    text = paint(app.rows(36, 10))
    assert text.startswith("\x1b[H") and text.count("\x1b[K") == 10


def test_tmux_not_answering_keeps_last_board(app, store, tmp_path: Path):
    from sebside.tmux import Completed

    broken = App(Config(), Tmux(None, lambda a, t: Completed(124, "", "timeout")), store, clock=lambda: NOW)
    broken.refresh()
    assert "did not answer" in broken.message


# closing when alone -----------------------------------------------------------------------------------
def alone_app(app: App, server, *panes: FakePane) -> App:
    server.panes += panes
    app.own_pane = "%1"
    app.refresh()
    return app


def sidebar_and_main(window: str = "0") -> tuple[FakePane, FakePane]:
    return (
        FakePane("%1", "api", window=window, options={"@sebside_role": "sidebar"}),
        FakePane("%2", "api", window=window),
    )


def test_sidebar_closes_when_the_main_pane_is_gone(server, app):
    sidebar, main = sidebar_and_main()
    alone_app(app, server, sidebar, main)
    assert app.alone() is False and app.companion_seen
    server.panes.remove(main)
    app.refresh()
    assert app.alone() is True
    assert not any(call[3 if call[1] == "-S" else 1] in ("kill-pane", "kill-window") for call in server.calls)


def test_sidebar_stays_while_any_main_pane_remains(server, app):
    sidebar, main = sidebar_and_main()
    second = FakePane("%3", "api")
    alone_app(app, server, sidebar, main, second)
    app.alone()
    server.panes.remove(main)
    app.refresh()
    assert app.alone() is False


def test_sidebar_ignores_main_panes_of_other_windows(server, app):
    sidebar, main = sidebar_and_main()
    elsewhere = FakePane("%9", "api", window="1")
    alone_app(app, server, sidebar, main, elsewhere)
    app.alone()
    server.panes.remove(main)
    app.refresh()
    assert app.alone() is True


def test_sidebar_that_never_had_a_companion_stays(server, app):
    sidebar, _ = sidebar_and_main()
    alone_app(app, server, sidebar)
    assert app.alone() is False


def test_close_alone_off_keeps_the_sidebar(server, app):
    sidebar, main = sidebar_and_main()
    alone_app(app, server, sidebar, main)
    app.alone()
    server.panes.remove(main)
    server.tmux_options["@sebside-close-alone"] = "off"
    app.refresh()
    assert app.alone() is False


def test_unknown_own_pane_never_closes(server, app):
    sidebar, main = sidebar_and_main()
    alone_app(app, server, sidebar, main)
    app.alone()
    app.own_pane = ""
    server.panes.remove(main)
    app.refresh()
    assert app.alone() is False


# mouse -------------------------------------------------------------------------------------------
def _three_cards(server: FakeTmuxServer, app: App) -> None:
    server.panes += [FakePane("%1", "a"), FakePane("%2", "b"), FakePane("%3", "c")]
    app.refresh()


def test_decode_sgr_mouse_reports():
    assert decode_keys("\x1b[<0;5;3M") == ["click:2"]
    assert decode_keys("\x1b[<64;5;3M\x1b[<65;5;3M") == ["up", "down"]
    assert decode_keys("\x1b[<0;5;3m") == []  # release
    assert decode_keys("\x1b[<2;5;3M\x1b[<32;5;3M") == []  # right button, drag
    assert decode_keys("j\x1b[<0;12;10Mk") == ["down", "click:9", "up"]


def test_cut_mouse_report_stays_pending():
    assert split_pending("j\x1b[<0;5") == ("j", "\x1b[<0;5")
    assert split_pending("\x1b[<") == ("", "\x1b[<")
    assert split_pending("\x1b[<0;5;3M") == ("\x1b[<0;5;3M", "")


def test_click_selects_and_activates_the_card_under_the_row(server, app):
    _three_cards(server, app)
    app.rows(36, 40)
    app.handle_key("click:5")  # second card spans rows 4-7
    assert app.selected_key == "pane:%2"
    assert server.commands()[-3:] == ["switch-client", "select-window", "select-pane"]
    assert app.message == ""


def test_click_respects_scroll_offset_and_multi_row_cards(server, app):
    _three_cards(server, app)
    app.selected_key = "pane:%3"
    app.rows(36, 12)  # room for 8 body rows: scrolled so rows 0-3 are card b, rows 4-7 card c
    assert app.row_keys[:8] == ["pane:%2"] * 4 + ["pane:%3"] * 4
    app.handle_key("click:3")  # the rule line of card b
    assert app.selected_key == "pane:%2"


def test_click_on_footer_and_empty_rows_does_nothing(server, app):
    _three_cards(server, app)
    app.rows(36, 40)
    before = len(server.calls)
    for row in (12, 30, 39, 99, -1):
        app.handle_key(f"click:{row}")
    assert app.selected_key == "pane:%1" and len(server.calls) == before


def test_click_on_recently_closed_header_does_nothing(server, app, store):
    _closed_app(server, app, store, ("agent",))
    app.selected_key = "pane:%2"
    app.rows(36, 40)
    header = app.row_keys.index("closed:c1") - 1
    assert app.row_keys[header] is None
    app.handle_key(f"click:{header}")
    assert app.selected_key == "pane:%2" and app.mode == "list"


def test_click_on_closed_card_asks_first_and_clicks_never_confirm(server, app, store):
    _closed_app(server, app, store, ("agent", "--resume", "c1"))
    app.selected_key = "pane:%2"
    app.rows(36, 40)
    row = app.row_keys.index("closed:c1")
    app.handle_key(f"click:{row}")
    assert app.mode == "confirm" and app.selected_key == "closed:c1"
    app.rows(36, 40)
    for index in range(40):
        app.handle_key(f"click:{index}")
    assert app.mode == "confirm" and "new-session" not in server.commands()


def test_wheel_moves_the_selection(server, app):
    _three_cards(server, app)
    app.handle_key("down")
    assert app.selected_key == "pane:%2"
    app.handle_key("up")
    assert app.selected_key == "pane:%1"


def test_current_session_comes_from_the_own_pane(server, app):
    server.panes += [FakePane("%1", "a"), FakePane("%2", "b")]
    assert app.current_session is None
    app.refresh()
    app.own_pane = "%2"
    assert app.current_session == "b"
    dots = [i for i, r in enumerate(app.rows(36, 40)) if to_plain(r).rstrip().endswith("●")]
    assert dots and app.row_keys[dots[0]] == "pane:%2"


def test_mouse_modifier_bits_do_not_hide_the_button():
    assert decode_keys("\x1b[<4;5;3M") == ["click:2"]  # shift
    assert decode_keys("\x1b[<16;5;3M") == ["click:2"]  # ctrl
    assert decode_keys("\x1b[<68;5;3M") == ["up"]
    assert decode_keys("\x1b[<32;5;3M\x1b[<35;5;3M") == []  # motion
    assert decode_keys("\x1b[<0;5;3M\x1b[<0;5;3m") == ["click:2"]  # press and release in one read


def test_code_watch_reloads_only_after_a_stable_change():
    from sebside.tui import CodeWatch

    state: list[tuple[tuple[str, int, int], ...] | None] = [(("a.py", 1, 1),)]
    watch = CodeWatch(lambda: state[0], interval=3.0)
    assert watch.changed(0.0) is False
    state[0] = (("a.py", 2, 1),)
    assert watch.changed(1.0) is False  # too soon
    assert watch.changed(4.0) is False  # first sighting of the change
    state[0] = (("a.py", 3, 1),)  # still being written
    assert watch.changed(8.0) is False
    state[0] = None  # unreadable mid-upgrade
    assert watch.changed(12.0) is False
    state[0] = (("a.py", 3, 1),)
    assert watch.changed(16.0) is False
    assert watch.changed(20.0) is True


def test_code_fingerprint_follows_file_changes(tmp_path: Path):
    from sebside.tui import code_fingerprint

    (tmp_path / "m.py").write_text("x")
    before = code_fingerprint(tmp_path)
    (tmp_path / "m.py").write_text("xyz")
    assert before is not None and code_fingerprint(tmp_path) != before
    assert code_fingerprint(tmp_path / "missing") == ()
