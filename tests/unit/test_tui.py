import os
from pathlib import Path

import pytest

from fakes import NOW, FakePane, FakeTmuxServer, record
from sebside.config import Config
from sebside.model import OPTION_ID, State
from sebside.render import to_plain
from sebside.store import Store
from sebside.tmux import Tmux
from sebside.tui import App, decode_keys, paint, session_name


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
    assert app.message == "showing api"


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
