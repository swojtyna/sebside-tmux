import json
import stat
from pathlib import Path

import pytest

from fakes import SERVER_PID, SOCKET, FakePane, FakeTmuxServer
from sebside import cli
from sebside.errors import EXIT_STATE, EXIT_TMUX, EXIT_USAGE
from sebside.model import OPTION_ID, OPTION_STATE, State
from sebside.store import Store
from sebside.tmux import Tmux


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PWD": "/work/dir",
    }


@pytest.fixture
def in_tmux(env: dict[str, str]) -> dict[str, str]:
    return {**env, "TMUX": f"{SOCKET},{SERVER_PID},0", "TMUX_PANE": "%4"}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTmuxServer:
    server = FakeTmuxServer([FakePane("%4", "work")])

    def create(socket: Path | None = None, *, uninstall: bool = False) -> Tmux:
        from sebside.tmux import ALLOWED, UNINSTALL_EXTRA

        return Tmux(socket, server, allowed=ALLOWED | UNINSTALL_EXTRA if uninstall else ALLOWED)

    monkeypatch.setattr(cli.Tmux, "create", staticmethod(create))
    monkeypatch.setattr(cli.Tmux, "available", staticmethod(lambda: True))
    return server


def store_of(env: dict[str, str]) -> Store:
    return Store.from_env(env)


def test_card_set_inside_tmux_writes_file_and_two_pane_options(fake, in_tmux, capsys):
    code = cli.main(
        ["card", "set", "--id", "job-1", "--state", "working", "--title", "Build", "--desc", "x\ny"], in_tmux
    )
    assert code == 0
    assert fake.pane("%4").options == {OPTION_ID: "job-1", OPTION_STATE: "working"}
    assert all("-S" in argv and "-t" in argv for argv in fake.calls)
    record = store_of(in_tmux).load("job-1")
    assert record is not None
    assert (record.state, record.title, record.desc, record.rev) == (State.WORKING, "Build", "x y", 1)
    assert record.tmux is not None and (record.tmux.socket, record.tmux.server_pid, record.tmux.pane) == (
        SOCKET,
        SERVER_PID,
        "%4",
    )
    assert record.cwd == "/work/dir"
    path = store_of(in_tmux).path("job-1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "job-1: working (rev 1, pane %4)" in capsys.readouterr().out


def test_card_set_again_keeps_fields_and_bumps_rev(fake, in_tmux):
    cli.main(["card", "set", "--id", "j", "--state", "working", "--title", "Build"], in_tmux)
    cli.main(["card", "set", "--id", "j", "--state", "done", "--desc", "214 ok"], in_tmux)
    record = store_of(in_tmux).load("j")
    assert record is not None and (record.title, record.desc, record.rev, record.state) == (
        "Build",
        "214 ok",
        2,
        State.DONE,
    )
    assert fake.pane("%4").options[OPTION_STATE] == "done"


def test_card_set_outside_tmux_never_calls_tmux(fake, env):
    assert cli.main(["card", "set", "--id", "cron", "--state", "error", "--desc", "backup failed"], env) == 0
    assert fake.calls == []
    record = store_of(env).load("cron")
    assert record is not None and record.tmux is None


def test_new_id_on_same_pane_closes_previous_card(fake, in_tmux):
    cli.main(["card", "set", "--id", "old", "--state", "working"], in_tmux)
    cli.main(["card", "set", "--id", "new", "--state", "waiting"], in_tmux)
    old = store_of(in_tmux).load("old")
    assert old is not None and old.closed
    assert fake.pane("%4").options[OPTION_ID] == "new"


def test_resume_is_stored_as_argv(fake, in_tmux):
    cli.main(["card", "set", "--id", "a", "--state", "working", "--resume", "agent --resume 'x y'"], in_tmux)
    record = store_of(in_tmux).load("a")
    assert record is not None and record.resume == ("agent", "--resume", "x y")
    assert not any("resume" in name for name in fake.pane("%4").options)


@pytest.mark.parametrize(
    "argv",
    [
        ["card", "set", "--id", "a", "--state", "sleeping"],
        ["card", "set", "--id", " ", "--state", "working"],
        ["card", "set", "--id", "a\nb", "--state", "working"],
        ["card", "set", "--id", "a", "--state", "working", "--resume", "unclosed 'quote"],
    ],
)
def test_invalid_input_is_a_usage_error_and_writes_nothing(fake, in_tmux, argv, capsys):
    assert cli.main(argv, in_tmux) == EXIT_USAGE
    assert not store_of(in_tmux).cards_dir.exists() or not list(store_of(in_tmux).cards_dir.glob("*.json"))
    assert "set-option" not in fake.commands()
    assert capsys.readouterr().err.startswith("sebside: ")


def test_tmux_refusal_is_exit_3(fake, in_tmux):
    fake.fail.add("display-message")
    assert cli.main(["card", "set", "--id", "a", "--state", "working"], in_tmux) == EXIT_TMUX


def test_unwritable_state_dir_is_exit_4(fake, env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    assert cli.main(["card", "set", "--id", "a", "--state", "working"], {**env, "SEBSIDE_STATE_DIR": str(blocker)}) in (
        EXIT_STATE,
    )


def test_clear_closes_or_forgets(fake, in_tmux):
    cli.main(["card", "set", "--id", "a", "--state", "working"], in_tmux)
    assert cli.main(["card", "clear"], in_tmux) == 0
    assert fake.pane("%4").options == {}
    record = store_of(in_tmux).load("a")
    assert record is not None and record.closed
    assert cli.main(["card", "clear", "--id", "a", "--forget"], in_tmux) == 0
    assert store_of(in_tmux).load("a") is None


def test_list_prints_json(fake, in_tmux, capsys):
    cli.main(["card", "set", "--id", "a", "--state", "waiting", "--title", "Deploy"], in_tmux)
    capsys.readouterr()
    assert cli.main(["card", "list"], in_tmux) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["live"][0]["title"] == "Deploy" and data["live"][0]["state"] == "waiting"
    assert data["history"] == []


def test_reconcile_closes_cards_of_vanished_panes(fake, in_tmux, monkeypatch):
    cli.main(["card", "set", "--id", "a", "--state", "working"], in_tmux)
    fake.panes.clear()
    fake.panes.append(FakePane("%5", "other"))
    monkeypatch.setattr(cli.time, "time", lambda: 9_999_999_999.0)
    assert cli.main(["reconcile"], in_tmux) == 0
    record = store_of(in_tmux).load("a")
    assert record is not None and record.closed


def test_config_get_and_path(env, capsys, tmp_path):
    (tmp_path / "config" / "sebside").mkdir(parents=True)
    (tmp_path / "config" / "sebside" / "config.toml").write_text("width = 44\n")
    assert cli.main(["config", "get", "width"], env) == 0
    assert capsys.readouterr().out.strip() == "44"
    assert cli.main(["config", "path"], env) == 0
    assert capsys.readouterr().out.strip().endswith("sebside/config.toml")
    assert cli.main(["config", "get", "nope"], env) == EXIT_USAGE


def test_sidebar_outside_tmux_is_exit_3(env, fake):
    assert cli.main([], env) == EXIT_TMUX


def test_uninstall_removes_only_sebside_things(fake, in_tmux):
    fake.panes.append(FakePane("%9", "work", options={"@sebside_role": "sidebar"}))
    fake.pane("%4").options[OPTION_ID] = "x"
    assert cli.main(["uninstall"], in_tmux) == 0
    commands = fake.commands()
    assert "set-hook" in commands and "kill-pane" in commands
    kill = next(argv for argv in fake.calls if "kill-pane" in argv)
    assert kill[-1] == "%9"
    assert OPTION_ID not in fake.pane("%4").options
