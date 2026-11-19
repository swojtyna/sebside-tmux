"""Groups: `@sebside_group` on a session limits its sidebar to the cards of that group (live, default and closed)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from fakes import NOW, SERVER_PID, SOCKET, FakePane, FakeTmuxServer, pane, record
from sebside import cli
from sebside.cards import Board, build
from sebside.config import Config
from sebside.errors import EXIT_USAGE, StateError
from sebside.model import OPTION_GROUP, OPTION_ID, CardRecord, Group, Meter, State, valid_group
from sebside.render import frame_rows, group_rows, text_width, to_plain
from sebside.store import Store, from_dict, to_dict
from sebside.tmux import PANE_FORMAT, Tmux, parse_panes
from sebside.tui import App


# ids ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["g1", "team-a", "x.y_z", "A" * 100])
def test_valid_group_ids(value):
    assert valid_group(value)


@pytest.mark.parametrize("value", ["", ".hidden", "a b", "a/b", "../x", "a;b", "é", "A" * 101, "a\nb"])
def test_invalid_group_ids(value):
    assert not valid_group(value)


# tmux ---------------------------------------------------------------------------------------------
def test_list_panes_reads_the_group_and_drops_an_invalid_one():
    server = FakeTmuxServer(
        [
            FakePane("%1", "a", options={OPTION_GROUP: "g1"}),
            FakePane("%2", "b"),
            FakePane("%3", "c", options={OPTION_GROUP: "not a group"}),
        ]
    )
    panes = parse_panes(server(["tmux", "list-panes"], 1.0).stdout)
    assert [(p.session, p.group) for p in panes] == [("a", "g1"), ("b", ""), ("c", "")]
    assert ":" + OPTION_GROUP + "}" in PANE_FORMAT  # free text: separators are replaced by tmux


# cards --------------------------------------------------------------------------------------------
def _board(group: str):
    panes = [
        pane("%1", "a1", group="g1", card_id="c1"),
        pane("%2", "a2", group="g1"),
        pane("%3", "b1", group="g2"),
        pane("%4", "plain"),
    ]
    records = {
        "c1": record("c1", pane_id="%1", title="card one", group="g1"),
        "old1": record("old1", pane_id=None, closed_at=NOW - 10, group="g1"),
        "old2": record("old2", pane_id=None, closed_at=NOW - 20, group="g2"),
        "old0": record("old0", pane_id=None, closed_at=NOW - 30),
        "cron": record("cron", pane_id=None, title="outside", group="g2"),
    }
    return build(panes, records, Config(), NOW, group=group)


def test_sidebar_with_a_group_shows_only_that_group():
    board = _board("g1")
    assert sorted(card.title for card in board.live) == ["a2", "card one"]
    assert [card.card_id for card in board.history] == ["old1"]
    other = _board("g2")
    assert sorted(card.title for card in other.live) == ["b1", "outside"]
    assert [card.card_id for card in other.history] == ["old2"]


def test_sidebar_without_a_group_shows_everything_as_before():
    board = _board("")
    assert sorted(card.title for card in board.live) == ["a2", "b1", "card one", "outside", "plain"]
    assert [card.card_id for card in board.history] == ["old1", "old2", "old0"]


def test_counter_counts_only_the_group():
    panes = [pane("%1", "a", group="g1", card_id="w"), pane("%2", "b", group="g2", card_id="e")]
    records = {"w": record("w", State.WAITING, pane_id="%1"), "e": record("e", State.ERROR, pane_id="%2")}
    counts = build(panes, records, Config(), NOW, group="g1").counts()
    assert counts[State.WAITING] == 1 and counts[State.ERROR] == 0


def test_card_without_a_stored_group_follows_its_pane():
    panes = [pane("%1", "a", group="g1", card_id="x")]
    card = build(panes, {"x": record("x", pane_id="%1")}, Config(), NOW, group="g1").live[0]
    assert card.card_id == "x" and card.group == "g1"


def test_stored_group_wins_over_the_pane():
    panes = [pane("%1", "a", group="g1", card_id="x")]
    records = {"x": record("x", pane_id="%1", group="g2")}
    assert build(panes, records, Config(), NOW, group="g1").live == []
    assert build(panes, records, Config(), NOW, group="g2").live[0].card_id == "x"


def test_history_limit_applies_within_the_group():
    records = {f"g{i}": record(f"g{i}", pane_id=None, closed_at=NOW - i, group="g1") for i in range(3)}
    records |= {f"o{i}": record(f"o{i}", pane_id=None, closed_at=NOW + i, group="g2") for i in range(5)}
    board = build([], records, Config(history=2), NOW, group="g1")
    assert [card.card_id for card in board.history] == ["g0", "g1"]


# store --------------------------------------------------------------------------------------------
def test_old_card_file_without_group_reads_and_round_trips():
    old = {"schema": 1, "id": "a", "state": "working", "title": "T", "rev": 2}
    loaded = from_dict(old)
    assert loaded is not None and loaded.group == "" and loaded.title == "T"
    assert to_dict(loaded)["group"] == ""
    assert from_dict(to_dict(CardRecord("a", group="g1"))) == CardRecord("a", group="g1")


def test_invalid_group_in_a_card_file_is_ignored(store):
    store.ensure_dir()
    store.path("a").write_text(json.dumps({"schema": 1, "id": "a", "state": "working", "group": "../x"}))
    loaded = store.load("a")
    assert loaded is not None and loaded.group == ""


# cli ----------------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"), "PWD": "/work"}


@pytest.fixture
def in_tmux(env: dict[str, str]) -> dict[str, str]:
    return {**env, "TMUX": f"{SOCKET},{SERVER_PID},0", "TMUX_PANE": "%4"}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTmuxServer:
    server = FakeTmuxServer([FakePane("%4", "work", options={OPTION_GROUP: "g1"}), FakePane("%5", "other")])
    monkeypatch.setattr(cli.Tmux, "create", staticmethod(lambda socket=None, **_: Tmux(socket, server)))
    monkeypatch.setattr(cli.Tmux, "available", staticmethod(lambda: True))
    return server


def test_card_set_stores_the_pane_group_and_never_writes_it(fake, in_tmux):
    assert cli.main(["card", "set", "--id", "a", "--state", "working"], in_tmux) == 0
    record_ = Store.from_env(in_tmux).load("a")
    assert record_ is not None and record_.group == "g1"
    sets = [argv for argv in fake.calls if "set-option" in argv]
    assert sets and not any(OPTION_GROUP in argv for argv in sets)


def test_card_set_group_flag_overrides_and_empty_clears(fake, in_tmux, env):
    cli.main(["card", "set", "--id", "a", "--state", "working", "--group", "g2"], in_tmux)
    assert Store.from_env(env).load("a").group == "g2"
    cli.main(["card", "set", "--id", "a", "--state", "working", "--group", ""], in_tmux)
    assert Store.from_env(env).load("a").group == ""


def test_card_set_outside_tmux_keeps_the_group_unless_given(fake, in_tmux, env):
    cli.main(["card", "set", "--id", "a", "--state", "working"], in_tmux)
    cli.main(["card", "set", "--id", "a", "--state", "done"], env)
    assert Store.from_env(env).load("a").group == "g1"
    cli.main(["card", "set", "--id", "cron", "--state", "done", "--group", "g2"], env)
    assert Store.from_env(env).load("cron").group == "g2"


def test_card_set_in_a_pane_without_group_stores_none(fake, env):
    other = {**env, "TMUX": f"{SOCKET},{SERVER_PID},0", "TMUX_PANE": "%5"}
    cli.main(["card", "set", "--id", "b", "--state", "working"], other)
    assert Store.from_env(env).load("b").group == ""


@pytest.mark.parametrize("bad", ["a b", ".x", "x;y", "../etc"])
def test_invalid_group_flag_is_a_usage_error_and_writes_nothing(fake, in_tmux, bad):
    assert cli.main(["card", "set", "--id", "a", "--state", "working", "--group", bad], in_tmux) == EXIT_USAGE
    assert Store.from_env(in_tmux).load("a") is None
    assert "set-option" not in fake.commands()


def test_card_list_shows_the_group_and_filters_by_it(fake, in_tmux, capsys):
    cli.main(["card", "set", "--id", "a", "--state", "waiting"], in_tmux)
    capsys.readouterr()
    assert cli.main(["card", "list"], in_tmux) == 0
    everything = json.loads(capsys.readouterr().out)
    assert {card["title"] or card["session"]: card["group"] for card in everything["live"]} == {
        "work": "g1",
        "other": "",
    }
    assert cli.main(["card", "list", "--group", "g1"], in_tmux) == 0
    assert [card["card_id"] for card in json.loads(capsys.readouterr().out)["live"]] == ["a"]
    assert cli.main(["card", "list", "--group", "a b"], in_tmux) == EXIT_USAGE


# sidebar ------------------------------------------------------------------------------------------
def test_sidebar_in_a_grouped_session_shows_only_its_group(server, tmux, store):
    server.panes += [
        FakePane("%1", "a", options={OPTION_GROUP: "g1", OPTION_ID: "x"}),
        FakePane("%2", "b", options={OPTION_GROUP: "g2"}),
        FakePane("%9", "a", options={OPTION_GROUP: "g1"}),
    ]
    store.write(record("x", State.WAITING, pane_id="%1", title="mine"))
    app = App(Config(), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False, own_pane="%9")
    app.refresh()
    assert app.group == "g1"
    assert [card.title for card in app.board.live] == ["mine"]
    app.own_pane = "%2"
    app.refresh()
    assert [card.title for card in app.board.live] == ["b"]


def test_sidebar_without_a_group_shows_every_session(server, tmux, store):
    server.panes += [FakePane("%1", "a", options={OPTION_GROUP: "g1"}), FakePane("%2", "b")]
    app = App(Config(), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False, own_pane="%2")
    app.refresh()
    assert app.group == ""
    assert sorted(card.title for card in app.board.live) == ["a", "b"]


# footer: store ------------------------------------------------------------------------------------
def test_group_file_is_private_and_atomic(store):
    store.write_group(Group("g1", "Release 4.18", "freeze Thu", NOW))
    assert store.load_group("g1") == Group("g1", "Release 4.18", "freeze Thu", NOW)
    assert stat.S_IMODE(store.group_path("g1").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.groups_dir.stat().st_mode) == 0o700
    assert [p.name for p in store.groups_dir.iterdir()] == ["g1.json"]
    assert store.clear_group("g1") and not store.clear_group("g1")
    assert store.load_group("g1") is None


def test_group_files_that_are_not_ours_or_not_valid_are_ignored(store, tmp_path):
    store.write_group(Group("ok", "T"))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"title": "evil"}))
    store.group_path("link").symlink_to(outside)
    store.group_path("broken").write_text("{nope")
    store.group_path("empty").write_text(json.dumps({"title": "  "}))
    store.group_path("typed").write_text(json.dumps({"title": "T", "note": 5}))
    assert [store.load_group(name) for name in ("link", "broken", "empty", "typed", "missing", "../x")] == [None] * 6


def test_group_path_refuses_an_invalid_id(store):
    with pytest.raises(StateError):
        store.group_path("../cards/x")


def test_dir_stamp_changes_when_a_group_file_changes(store):
    store.ensure_dir()
    before = store.dir_stamp()
    store.write_group(Group("g1", "T"))
    assert store.dir_stamp() != before


# footer: render -----------------------------------------------------------------------------------
def _plain(rows):
    return [to_plain(row).rstrip() for row in rows]


def test_title_wraps_by_words_up_to_group_lines():
    group = Group("g", "one two three four five six seven", "a note")
    assert _plain(group_rows(group, 13, Config())) == ["one two", "three four", "five six se…", "a note"]
    assert _plain(group_rows(group, 13, Config(group_lines=2))) == ["one two", "three four …", "a note"]


def test_title_that_fits_has_no_ellipsis():
    rows = _plain(group_rows(Group("g", "one two three four five six"), 13, Config()))
    assert rows == ["one two", "three four", "five six"]
    assert not any("…" in row for row in rows)


def test_note_is_one_dimmed_line_cut_with_ellipsis():
    rows = group_rows(Group("g", "T", "a very long note that does not fit"), 13, Config())
    assert _plain(rows) == ["T", "a very long…"]
    assert all(style.dim for _, style in rows[1] if _ not in ("", " "))
    assert all(style.bold for ch, style in rows[0] if ch == "T")


def test_wide_and_control_characters_stay_inside_the_width():
    rows = group_rows(Group("g", "日本語テキスト\x1b[31m 日本語テキスト", "x\ny"), 9, Config())
    assert all(len(row) == 9 for row in rows)
    assert all(text_width(to_plain(row)) <= 9 for row in rows)


def test_no_group_no_rows():
    assert group_rows(None, 30, Config()) == []


def test_group_footer_sits_between_counter_and_meter():
    board = Board([])
    meter = Meter("week", 50, "", NOW)
    rows = _plain(frame_rows(board, None, 30, 14, Config(), NOW, meter=meter, group=Group("g", "Title", "note")))
    counter = next(i for i, row in enumerate(rows) if row.startswith("nothing needs you"))
    assert rows[counter + 1 : counter + 3] == ["Title", "note"]
    assert rows[counter + 3].startswith("week ")


def test_frame_without_a_group_is_unchanged():
    board = Board([])
    meter = Meter("week", 50, "", NOW)
    assert frame_rows(board, None, 30, 14, Config(), NOW, meter=meter) == frame_rows(
        board, None, 30, 14, Config(), NOW, meter=meter, group=None
    )


# footer: cli --------------------------------------------------------------------------------------
def test_group_set_then_clear(env):
    assert cli.main(["group", "set", "--id", "g1", "--title", "Release\n4.18", "--note", "freeze Thu"], env) == 0
    group = Store.from_env(env).load_group("g1")
    assert group is not None and (group.title, group.note) == ("Release 4.18", "freeze Thu")
    assert cli.main(["group", "set", "--id", "g1", "--title", "Next"], env) == 0
    group = Store.from_env(env).load_group("g1")
    assert group is not None and (group.title, group.note) == ("Next", "")
    assert cli.main(["group", "clear", "--id", "g1"], env) == 0
    assert Store.from_env(env).load_group("g1") is None
    assert cli.main(["group", "clear", "--id", "g1"], env) == 0  # nothing to clear is fine


@pytest.mark.parametrize(
    "argv",
    [
        ["group", "set", "--id", "a b", "--title", "T"],
        ["group", "set", "--id", "../x", "--title", "T"],
        ["group", "set", "--id", "g", "--title", " \n "],
        ["group", "clear", "--id", ".x"],
    ],
)
def test_invalid_group_commands_are_usage_errors(env, argv, capsys):
    assert cli.main(argv, env) == EXIT_USAGE
    assert capsys.readouterr().err.startswith("sebside: ")
    assert not Store.from_env(env).groups_dir.exists()


def test_group_commands_never_call_tmux(fake, in_tmux):
    cli.main(["group", "set", "--id", "g1", "--title", "T"], in_tmux)
    cli.main(["group", "clear", "--id", "g1"], in_tmux)
    assert fake.calls == []


# footer: sidebar ----------------------------------------------------------------------------------
def test_sidebar_draws_its_group_footer(server, tmux, store):
    server.panes += [FakePane("%1", "a", options={OPTION_GROUP: "g1"}), FakePane("%2", "b")]
    store.write_group(Group("g1", "Release train", "freeze Thu"))
    app = App(Config(), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False, own_pane="%1")
    app.refresh()
    rows = _plain(app.rows(30, 12))
    assert "Release train" in rows and "freeze Thu" in rows
    app.own_pane = "%2"  # a session without a group: footer as before
    app.refresh()
    assert app.footer is None
    assert not any("Release train" in row for row in _plain(app.rows(30, 12)))


def test_sidebar_without_a_group_file_has_the_old_footer(server, tmux, store):
    server.panes.append(FakePane("%1", "a", options={OPTION_GROUP: "g1"}))
    app = App(Config(), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False, own_pane="%1")
    app.refresh()
    assert app.footer is None
    assert app.rows(30, 12)[-4:] == frame_rows(app.board, app.selected_key, 30, 12, Config(), NOW)[-4:]
