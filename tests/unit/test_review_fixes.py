"""Regression tests for the issues found in the v0.1 review (one test per fix)."""

import json
import os
import signal
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from fakes import NOW, SERVER_PID, SOCKET, FakePane, FakeTmuxServer, pane, record
from sebside import cli
from sebside.cards import build
from sebside.config import Config
from sebside.errors import EXIT_USAGE, ForbiddenCommand, StateError
from sebside.log import setup
from sebside.model import OPTION_ID, CardRecord, State
from sebside.render import card_rows, text_width, to_plain
from sebside.store import Store, file_name, from_dict, to_dict
from sebside.tmux import FIELD_SEP, PANE_FIELDS, PANE_FORMAT, RECORD_SEP, Completed, Tmux, check_command, parse_panes
from sebside.tui import App, _exit_on_signal, split_pending, terminal_size


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PWD": "/work",
        "TMUX": f"{SOCKET},{SERVER_PID},0",
        "TMUX_PANE": "%4",
    }


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTmuxServer:
    server = FakeTmuxServer([FakePane("%4", "work"), FakePane("%5", "other")])
    monkeypatch.setattr(cli.Tmux, "create", staticmethod(lambda socket=None, *, uninstall=False: Tmux(socket, server)))
    monkeypatch.setattr(cli.Tmux, "available", staticmethod(lambda: True))
    return server


# 1: the hook passes --socket after the subcommand
def test_socket_and_verbose_are_accepted_after_the_subcommand():
    parser = cli._parser()
    assert parser.parse_args(["reconcile", "--socket", "/s"]).socket == "/s"
    assert parser.parse_args(["--socket", "/a", "reconcile"]).socket == "/a"
    assert parser.parse_args(["card", "list", "-v"]).verbose is True
    assert parser.parse_args(["reconcile"]).socket is None


def test_plugin_hook_command_parses():
    text = (Path(__file__).resolve().parents[2] / "sebside.tmux").read_text()
    assert "--socket #{q:socket_path} reconcile" in text
    args = cli._parser().parse_args(["--socket", "/tmp/s", "reconcile"])
    assert args.command == "reconcile"


# 2: non-finite numbers in a card file
def test_nan_and_infinity_in_card_files_do_not_crash(store: Store):
    store.ensure_dir()
    store.path("nan").write_text('{"id": "nan", "state": "working", "updated_at": NaN}')
    store.path("big").write_text('{"id": "big", "state": "working", "updated_at": 1e400, "closed_at": 1e400}')
    records = store.load_all()
    assert "nan" not in records
    assert records["big"].updated_at == 0.0 and records["big"].closed_at is None
    board = build([], records, Config(), NOW)
    assert card_rows(board.live[0], 36, False, Config(), NOW)


# 3: a stale pane option must not close a card that lives on another pane
def test_card_set_does_not_close_a_card_live_elsewhere(fake: FakeTmuxServer, env: dict[str, str]):
    cli.main(["card", "set", "--id", "elsewhere", "--state", "working", "--pane", "%5"], env)
    fake.pane("%4").options[OPTION_ID] = "elsewhere"  # copied or stale option
    cli.main(["card", "set", "--id", "mine", "--state", "working"], env)
    other = Store.from_env(env).load("elsewhere")
    assert other is not None and not other.closed


def test_card_clear_without_id_does_not_close_a_card_live_elsewhere(fake: FakeTmuxServer, env: dict[str, str]):
    cli.main(["card", "set", "--id", "elsewhere", "--state", "working", "--pane", "%5"], env)
    fake.pane("%4").options[OPTION_ID] = "elsewhere"
    assert cli.main(["card", "clear"], env) == 0
    other = Store.from_env(env).load("elsewhere")
    assert other is not None and not other.closed
    assert OPTION_ID not in fake.pane("%4").options


# 4: reconcile must not close a card that card set refreshed meanwhile
def test_close_skips_cards_changed_since_the_decision(store: Store):
    store.write(record("a", rev=1))
    store.write(record("a", rev=2))  # card set ran between the decision and the close
    assert store.close(["a"], NOW, {"a": 1}) == []
    assert store.close(["a"], NOW, {"a": 2}) == ["a"]


# 5: errors during refresh stay visible and the sidebar keeps running
def test_refresh_errors_go_to_the_message_line(tmux: Tmux, store: Store, monkeypatch: pytest.MonkeyPatch):
    app = App(Config(), tmux, store, clock=lambda: NOW)

    def broken() -> dict[str, CardRecord]:
        raise StateError("cannot read cards: disk on fire")

    monkeypatch.setattr(store, "load_all", broken)
    app.refresh()
    assert "disk on fire" in app.message


def test_sigterm_unwinds_so_the_terminal_is_restored():
    with pytest.raises(SystemExit) as raised:
        _exit_on_signal(signal.SIGTERM, None)
    assert raised.value.code == 128 + signal.SIGTERM


# 8: narrow panes with wide characters never overflow
@pytest.mark.parametrize("width", range(1, 12))
def test_narrow_widths_with_wide_characters(width: int):
    from sebside.model import Card

    card = Card("k", "i", State.WAITING, "日本語タイトル", "説明説明", "説明" * 20, extra="日本", since=NOW - 5)
    for row in card_rows(card, width, True, Config(), NOW):
        assert len(row) == width
        assert text_width(to_plain(row)) == width


# 9: terminal size from the fd, with a fallback
def test_terminal_size_falls_back_when_fd_is_not_a_tty(tmp_path: Path):
    with (tmp_path / "f").open("w") as handle:
        assert terminal_size(handle.fileno(), 40) == (40, 24)


# 10: an escape sequence split across two reads is not an Esc
def test_split_pending_keeps_partial_escape_sequences():
    assert split_pending("j\x1b") == ("j", "\x1b")
    assert split_pending("\x1b[") == ("", "\x1b[")
    assert split_pending("\x1bO") == ("", "\x1bO")
    assert split_pending("\x1b[A") == ("\x1b[A", "")
    assert split_pending("kq") == ("kq", "")


# 11: the parse cache notices a replaced file even with the same size and mtime
def test_cache_notices_replaced_file_with_same_mtime_and_size(store: Store):
    store.write(record("a", desc="first"))
    path = store.path("a")
    before = path.stat()
    assert store.load("a").desc == "first"  # type: ignore[union-attr]
    other = path.with_name("other.tmp")
    other.write_text(path.read_text().replace("first", "secnd"))
    os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
    other.replace(path)
    assert store.load("a").desc == "secnd"  # type: ignore[union-attr]


# 12: a file whose name does not match its id is ignored
def test_file_name_must_match_card_id(store: Store):
    store.ensure_dir()
    (store.cards_dir / "a.json").write_text(json.dumps(to_dict(record("b"))))
    assert store.load_all() == {}
    assert file_name("b") == "b.json"


# 14: a one-word resume command is never run through sh -c
def test_single_word_resume_runs_without_shell(server: FakeTmuxServer, tmux: Tmux):
    tmux.new_session("job", None, ("agent",))
    argv = server.calls[-1]
    assert argv[argv.index("--") + 1 :] == ["env", "--", "agent"]


# 15: separators inside free-text fields cannot forge records
def test_free_text_fields_have_separators_replaced_by_tmux():
    for name in ("pane_current_path", "pane_current_command", "session_name", OPTION_ID):
        assert "#{s/[" + FIELD_SEP + RECORD_SEP + "]/?/:" + name + "}" in PANE_FORMAT


def test_records_with_invalid_ids_or_numbers_are_skipped():
    good = FakeTmuxServer([FakePane("%1", "ok")])
    record_text = good.record(good.panes[0])
    values = record_text.rstrip(RECORD_SEP).split(FIELD_SEP)
    forged_pane = values.copy()
    forged_pane[PANE_FIELDS.index("pane_id")] = "%1; evil"
    forged_pid = values.copy()
    forged_pid[PANE_FIELDS.index("pid")] = "12x"
    output = "".join(FIELD_SEP.join(v) + RECORD_SEP for v in (forged_pane, forged_pid)) + record_text
    assert [p.pane_id for p in parse_panes(output)] == ["%1"]


# 16: non-UTF-8 input never crashes card set or leaves temporary files
def test_surrogates_in_cwd_are_escaped_and_in_resume_refused(fake: FakeTmuxServer, env: dict[str, str]):
    bad = "/work/\udcff"
    assert cli.main(["card", "set", "--id", "a", "--state", "working", "--cwd", bad], env) == 0
    loaded = Store.from_env(env).load("a")
    assert loaded is not None and loaded.cwd == "/work/�"
    assert cli.main(["card", "set", "--id", "a", "--state", "working", "--resume", "agent \udcff"], env) == EXIT_USAGE


def test_write_failure_cleans_up_temporary_file(store: Store):
    with pytest.raises(StateError):
        store.write(record("a", title="\udcff"))
    assert [p.name for p in store.cards_dir.iterdir()] == []


# 17: card ids with control characters are not cards
def test_ids_with_control_characters_are_rejected():
    assert from_dict({"id": "evil\x1b[2J", "state": "working"}) is None
    assert from_dict({"id": "fine-id", "state": "working"}) is not None


# 18: run exactly what was confirmed, read from the checked descriptor
def test_resume_refused_when_command_changed_after_confirmation(server: FakeTmuxServer, tmux: Tmux, store: Store):
    app = App(Config(resume_programs=("agent",)), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False)
    server.panes.append(FakePane("%2", "web"))
    store.write(record("c", closed_at=NOW - 5, resume=("agent", "--resume", "c")))
    app.refresh()
    app.selected_key = "closed:c"
    app.handle_key("enter")
    loaded = store.load("c")
    assert loaded is not None
    store.write(replace(loaded, resume=("agent", "--evil")))
    app.handle_key("enter")
    assert "new-session" not in server.commands()
    assert "changed" in app.message


def test_load_trusted_checks_mode_on_the_file_it_reads(store: Store):
    store.write(record("a", resume=("agent",)))
    assert store.load_trusted("a")[0] is not None
    os.chmod(store.path("a"), 0o640)
    loaded, reason = store.load_trusted("a")
    assert loaded is None and "chmod" in reason


# 20: no format expansion in the resume directory
def test_new_session_refuses_hash_in_directory():
    with pytest.raises(ForbiddenCommand):
        check_command("new-session", ("-d", "-s", "x", "-c", "/tmp/#{pane_id}", "--", "agent"))
    check_command("new-session", ("-d", "-s", "x", "-c", "/tmp/ok", "--", "agent"))


def test_resume_drops_directory_with_hash(server: FakeTmuxServer, tmux: Tmux, store: Store, tmp_path: Path):
    hashed = tmp_path / "a#b"
    hashed.mkdir()
    app = App(Config(resume_programs=("agent",)), tmux, store, clock=lambda: NOW, socket_exists=lambda p: False)
    server.panes.append(FakePane("%2", "web"))
    store.write(record("c", closed_at=NOW - 5, resume=("agent",), cwd=str(hashed)))
    app.refresh()
    app.selected_key = "closed:c"
    app.handle_key("enter")
    app.handle_key("enter")
    argv = next(a for a in server.calls if "new-session" in a)
    assert "-c" not in argv


# 22: private state root and debug log
def test_state_root_forced_private_and_debug_log_owner_only(tmp_path: Path):
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    Store(root).ensure_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    log = setup(True, root)
    assert log is not None and stat.S_IMODE(log.stat().st_mode) == 0o600


def test_unused_helpers_stay_importable():
    assert Completed(0, "", "").ok
    assert pane("%1", "s").pane_id == "%1"
