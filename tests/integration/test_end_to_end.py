"""End to end: sebside, sebside.tmux and toggle.sh against `TmuxWorld`, a fake tmux server that behaves like real
tmux on the paths used (sessions, windows, panes, options, environment, hooks, key bindings, formats, run-shell).

The bash scripts run for real in bash; only `tmux` is answered by the fake (see `fakes.run_script`). Hooks that tmux
would run fire synchronously and start `sebside` as `cli.main` in this process. Timing (the grace period, the
sidebar's refresh) runs on an injected clock instead of waiting. The same scenarios against a real tmux server, for a
check by hand: `tests/manual/real_tmux.py` (see CONTRIBUTING.md).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import TmuxWorld, run_script, sebside_program
from sebside import cli
from sebside.cards import build
from sebside.config import Config, load_from_env
from sebside.model import OPTION_GROUP, OPTION_ID, OPTION_STATE, State
from sebside.render import to_plain
from sebside.store import Store
from sebside.tmux import ALLOWED, OPEN_EXTRA, UNINSTALL_EXTRA, Tmux
from sebside.tui import App, Poller

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "sebside.tmux"
TOGGLE = REPO / "scripts" / "toggle.sh"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The clock `cli` reads (`time.time()` in `main`), injected so grace periods pass without waiting."""
    fake = Clock(time.time())
    monkeypatch.setattr(cli, "time", SimpleNamespace(time=fake))
    return fake


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> TmuxWorld:
    """A server with sessions alpha and beta (100x30, `sleep 600`); every `Tmux` that sebside creates talks to it."""
    for name in ("TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
    socket = tmp_path / "s"
    socket.touch()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TERM": "xterm-256color"}
    world = TmuxWorld(str(socket), environment=dict(env))
    world.programs["sebside"] = sebside_program(cli.main)

    def create(socket: Path | None = None, *, uninstall: bool = False, opener: bool = False) -> Tmux:
        allowed = ALLOWED | (UNINSTALL_EXTRA if uninstall else frozenset()) | (OPEN_EXTRA if opener else frozenset())
        return Tmux(socket, world, "/usr/bin/tmux", allowed=allowed)

    monkeypatch.setattr(cli.Tmux, "create", staticmethod(create))
    monkeypatch.setattr(cli.Tmux, "available", staticmethod(lambda: True))
    world.tmux("new-session", "-d", "-s", "alpha", "-x", "100", "-y", "30", "sleep 600")
    world.tmux("new-session", "-d", "-s", "beta", "-x", "100", "-y", "30", "sleep 600")
    return world


def env_for(server: TmuxWorld, tmp_path: Path, pane: str | None = None) -> dict[str, str]:
    env = {
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SEBSIDE_TMUX_SOCKET": server.socket,
        "PATH": "/usr/bin:/bin",
    }
    if pane:
        env |= {"TMUX": f"{server.socket},{server.pid},0", "TMUX_PANE": pane}
    return env


def test_list_panes_reads_every_pane(server: TmuxWorld):
    panes = Tmux(Path(server.socket), server).list_panes()
    assert panes is not None
    assert sorted(p.session for p in panes) == ["alpha", "beta"]
    assert all(p.socket == server.socket and p.server_pid > 0 for p in panes)


def test_card_set_end_to_end(server: TmuxWorld, tmp_path: Path, clock: Clock):
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    env = env_for(server, tmp_path, pane)
    assert cli.main(["card", "set", "--id", "job", "--state", "waiting", "--title", "Deploy"], env) == 0
    assert server.tmux("show-options", "-pqv", "-t", pane, OPTION_ID) == "job"
    assert server.tmux("show-options", "-pqv", "-t", pane, OPTION_STATE) == "waiting"
    store = Store.from_env(env)
    panes = Tmux(Path(server.socket), server).list_panes()
    assert panes is not None
    board = build(panes, store.load_all(), Config(), clock())
    titles = {card.title: card for card in board.live}
    assert titles["Deploy"].state is State.WAITING and titles["Deploy"].session == "alpha"
    assert "beta" in titles


def test_card_update_changes_text_only(server: TmuxWorld, tmp_path: Path):
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    assert cli.main(["card", "set", "--id", "job", "--state", "done"], env_for(server, tmp_path, pane)) == 0
    before = Store.from_env(env_for(server, tmp_path)).load("job")
    assert cli.main(["card", "update", "--id", "job", "--subtitle", "ctx 40%"], env_for(server, tmp_path)) == 0
    after = Store.from_env(env_for(server, tmp_path)).load("job")
    assert before is not None and after is not None and after.subtitle == "ctx 40%"
    assert after.state is State.DONE
    assert (after.tmux, after.rev, after.updated_at) == (before.tmux, before.rev, before.updated_at)
    assert server.tmux("show-options", "-pqv", "-t", pane, OPTION_ID) == "job"
    assert server.tmux("show-options", "-pqv", "-t", pane, OPTION_STATE) == "done"


def test_closed_session_is_reconciled_into_history(server: TmuxWorld, tmp_path: Path, clock: Clock):
    pane = server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}")
    env = env_for(server, tmp_path, pane)
    cli.main(["card", "set", "--id", "b", "--state", "working", "--resume", "sleep 1"], env)
    server.tmux("kill-session", "-t", "=beta")  # the test harness, not sebside, closes the session
    clock.now += 10
    plain = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")}
    assert cli.main(["reconcile"], plain) == 0
    record = Store.from_env(env).load("b")
    assert record is not None and record.closed


def test_resume_argv_runs_without_shell(server: TmuxWorld):
    tmux = Tmux(Path(server.socket), server)
    argv = ("sh", "-c", "sleep 30", "ignored; echo not-a-command")
    done = tmux.new_session("resumed", None, argv)
    assert done.ok, done.stderr
    assert "resumed" in server.tmux("list-sessions", "-F", "#{session_name}").split()
    assert server.tmux("display-message", "-p", "-t", "=resumed:", "#{pane_current_command}") in ("sh", "sleep")
    assert server.sessions["resumed"].window().panes[0].argv == argv  # exec'd as given: no shell joined the words


def plugin_env(server: TmuxWorld) -> dict[str, str]:
    """The environment tmux gives `run-shell` and key bindings: the server's own, plus $TMUX."""
    return {**server.environment, "TMUX": f"{server.socket},{server.pid},0"}


def source_plugin(server: TmuxWorld, env: dict[str, str] | None = None, shims: tuple[str, ...] = ("sebside",)) -> None:
    done = run_script(server, PLUGIN, env=env or plugin_env(server), shims=shims)
    assert done.ok, done.stderr


def toggle(server: TmuxWorld, pane: str) -> None:
    done = run_script(server, TOGGLE, pane, env=plugin_env(server), shims=("sebside",))
    assert done.ok, done.stderr


# One plugin scenario (install, toggle on, reload, toggle off) in three tests, at most two bash runs each.
def test_plugin_installs_hook_and_key_and_toggle_opens_the_sidebar(server: TmuxWorld):
    source_plugin(server)
    assert "session-closed[97]" in server.tmux("show-hooks", "-g")
    assert "toggle.sh" in server.tmux("list-keys", "-T", "prefix", "S")
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    toggle(server, pane)
    roles = server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role}").split()
    assert roles == ["sidebar"]


def test_plugin_reload_is_idempotent(server: TmuxWorld):
    source_plugin(server)
    source_plugin(server)
    assert server.tmux("show-hooks", "-g").count("session-closed[97]") == 1


def test_toggle_twice_opens_then_closes_the_sidebar(server: TmuxWorld):
    server.tmux("set-environment", "-g", "SEBSIDE_BIN", "sebside")  # what the plugin sets on load
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    toggle(server, pane)
    assert server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role}").split() == ["sidebar"]
    toggle(server, pane)
    assert server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role}").split() == []


def test_uninstall_removes_hook_key_and_sidebar(server: TmuxWorld, tmp_path: Path):
    source_plugin(server)
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    toggle(server, pane)
    assert cli.main(["uninstall"], env_for(server, tmp_path)) == 0
    assert "session-closed[97]" not in server.tmux("show-hooks", "-g")
    keys = server.client(server.socket, ["list-keys", "-T", "prefix", "S"])
    assert "toggle.sh" not in keys.stdout
    assert server.tmux("list-panes", "-a", "-F", "#{@sebside_role}").split() == []


# Review regressions ---------------------------------------------------------------------------------------------


def test_session_closed_hook_really_closes_the_card(server: TmuxWorld, tmp_path: Path, clock: Clock):
    env = env_for(server, tmp_path)
    server.tmux("set-environment", "-g", "XDG_STATE_HOME", env["XDG_STATE_HOME"])
    source_plugin(server)
    pane = server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}")
    assert cli.main(["card", "set", "--id", "hooked", "--state", "working"], env_for(server, tmp_path, pane)) == 0
    clock.now += 2.2  # past the grace period that protects a card that was set a moment ago
    server.tmux("kill-session", "-t", "=beta")  # the test harness closes the session; the hook must notice
    record = Store.from_env(env).load("hooked")
    assert record is not None and record.closed


def test_hook_works_with_quote_space_and_hash_in_the_sebside_path(server: TmuxWorld, tmp_path: Path, clock: Clock):
    odd = tmp_path / "it's a #dir $HOME"
    odd.mkdir()
    program = odd / "sebside"
    program.write_text("#!/bin/sh\nexit 99\n")  # found on PATH by the plugin; tmux runs it through `programs`
    program.chmod(0o755)
    server.programs[str(program)] = sebside_program(cli.main)
    del server.programs["sebside"]
    env = env_for(server, tmp_path)
    server.tmux("set-environment", "-g", "XDG_STATE_HOME", env["XDG_STATE_HOME"])
    plugin = plugin_env(server)
    plugin["PATH"] = f"{odd}:{plugin['PATH']}"
    source_plugin(server, plugin, shims=())
    assert "##dir" in server.tmux("show-hooks", "-g")
    pane = server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}")
    cli.main(["card", "set", "--id", "odd", "--state", "working"], env_for(server, tmp_path, pane))
    clock.now += 2.2
    server.tmux("kill-session", "-t", "=beta")
    record = Store.from_env(env).load("odd")
    assert record is not None and record.closed


def test_invalid_width_option_falls_back_to_default(server: TmuxWorld):
    server.tmux("set-option", "-g", "@sebside-width", "wide; rm -rf /")
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    toggle(server, pane)
    widths = server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role} #{pane_width}").splitlines()
    sidebar = [line.split()[1] for line in widths if line.startswith("sidebar")]
    assert sidebar == ["36"]


def test_separators_in_a_real_directory_cannot_forge_panes(server: TmuxWorld, tmp_path: Path):
    evil = tmp_path / f"a{chr(0x1E)}%99{chr(0x1F)}b"
    evil.mkdir()
    server.tmux("new-session", "-d", "-s", "evil", "-c", str(evil), "sleep 600")
    panes = Tmux(Path(server.socket), server).list_panes()
    assert panes is not None
    assert sorted(p.session for p in panes) == ["alpha", "beta", "evil"]
    assert all(p.pane_id != "%99" for p in panes)
    assert any(p.cwd.endswith("a?%99?b") for p in panes)


# Auto-open, focus and `sebside open` ---------------------------------------------------------------------------


def roles_of(server: TmuxWorld, target: str) -> list[str]:
    return server.tmux("list-panes", "-t", target, "-F", "#{pane_id} #{@sebside_role}").splitlines()


def sidebar_count(server: TmuxWorld, target: str) -> int:
    return sum(1 for line in roles_of(server, target) if line.endswith(" sidebar"))


def test_open_if_missing_opens_once_and_is_quiet(server: TmuxWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    window = server.tmux("display-message", "-p", "-t", "=alpha:", "#{window_id}")
    env = env_for(server, tmp_path)
    assert cli.main(["open", "--if-missing", "--target", window], env) == 0
    assert cli.main(["open", "--if-missing", "--target", window], env) == 0
    assert sidebar_count(server, window) == 1
    panes = server.tmux("list-panes", "-t", window, "-F", "#{pane_left} #{pane_active} #{@sebside_role} #{pane_width}")
    left, main = sorted(panes.splitlines())  # "0 0 sidebar 36" sorts before the main pane
    assert left.startswith("0 0 sidebar") and left.endswith(" 36")  # leftmost, not active
    assert main.split()[1] == "1"  # focus stayed in the main pane
    assert capsys.readouterr().out == ""


def test_auto_on_installs_hooks_at_fixed_indexes_and_reload_is_idempotent(server: TmuxWorld):
    server.tmux("set-hook", "-g", "session-created[0]", "set-option -g @mine yes")
    server.tmux("set-option", "-g", "@sebside-auto", "on")
    source_plugin(server)
    source_plugin(server)
    hooks = server.tmux("show-hooks", "-g")
    assert hooks.count("session-created[96]") == 1 and hooks.count("after-new-window[98]") == 1
    assert "session-created[0]" in hooks and "session-closed[97]" in hooks
    assert "open --if-missing --target #{window_id}" in hooks


def test_auto_on_opens_the_sidebar_in_new_sessions_and_windows(server: TmuxWorld):
    server.tmux("set-option", "-g", "@sebside-auto", "on")
    source_plugin(server)
    server.tmux("new-session", "-d", "-s", "gamma", "-x", "100", "-y", "30", "sleep 600")
    assert sidebar_count(server, "=gamma:") == 1
    server.tmux("new-window", "-d", "-t", "=gamma:", "sleep 600")
    assert sidebar_count(server, "=gamma:0") == 1 and sidebar_count(server, "=gamma:1") == 1
    assert sidebar_count(server, "=alpha:") == 0  # existing sessions are left alone


def test_auto_off_removes_the_hooks_and_nothing_opens(server: TmuxWorld):
    server.tmux("set-option", "-g", "@sebside-auto", "on")
    source_plugin(server)
    server.tmux("set-option", "-g", "@sebside-auto", "off")
    source_plugin(server)
    hooks = server.tmux("show-hooks", "-g")
    assert "session-created[96]" not in hooks and "after-new-window[98]" not in hooks
    assert "session-closed[97]" in hooks
    server.tmux("new-session", "-d", "-s", "delta", "-x", "100", "-y", "30", "sleep 600")
    assert sidebar_count(server, "=delta:") == 0


def test_auto_default_is_off(server: TmuxWorld):
    source_plugin(server)
    hooks = server.tmux("show-hooks", "-g")
    assert "session-created[96]" not in hooks and "after-new-window[98]" not in hooks


def test_focus_main_after_switch_lands_in_the_main_pane(server: TmuxWorld, tmp_path: Path):
    window = server.tmux("display-message", "-p", "-t", "=beta:", "#{window_id}")
    assert cli.main(["open", "--target", window], env_for(server, tmp_path)) == 0
    sidebar = next(line.split()[0] for line in roles_of(server, window) if line.endswith(" sidebar"))
    main = next(line.split()[0] for line in roles_of(server, window) if not line.endswith(" sidebar"))
    server.tmux("select-pane", "-t", sidebar)  # the cursor is in the sidebar of the target window
    server.attach("alpha")  # a client attached to another session
    assert server.tmux("list-clients").strip() != ""
    tmux = Tmux(Path(server.socket), server)
    assert tmux.show("beta", "0", sidebar, focus_main=True).ok
    assert server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}") == main
    server.tmux("select-pane", "-t", sidebar)
    assert tmux.show("beta", "0", sidebar).ok  # default behaviour is unchanged
    assert server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}") == sidebar


# Closing the sidebar when it is alone ------------------------------------------------------------------------


class Sidebar:
    """The sidebar program running in its pane, as `sebside sidebar` builds it (`run_sidebar`, then `run` takes
    `$TMUX_PANE` as its own pane), with the environment tmux gives a pane program (the server's global environment)
    and the poll loop on an injected monotonic clock. When it decides to close, its process ends and tmux closes the
    pane. `screen` is what it paints into its pane (what `capture-pane` would show)."""

    def __init__(self, server: TmuxWorld, pane: str) -> None:
        self.server, self.pane, self.clock = server, pane, Clock(1000.0)
        env = {**server.environment, "TMUX": f"{server.socket},{server.pid},0", "TMUX_PANE": pane}
        self.app = App(load_from_env(env), cli.Tmux.create(Path(server.socket)), Store.from_env(env), home=env["HOME"])
        self.app.own_pane = pane
        self.poller = Poller(self.app, monotonic=self.clock)

    def run_for(self, seconds: float, step: float = 0.25) -> bool:
        """Let the sidebar loop run; True when it exited (and tmux closed its pane)."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now += step
            if self.poller.due() and self.poller.poll():
                self.server.program_exits(self.pane)
                return True
        return False

    def screen(self) -> str:
        width = int(self.server.tmux("display-message", "-p", "-t", self.pane, "#{pane_width}"))
        return "\n".join(to_plain(row) for row in self.app.rows(width, 30))

    def shows(self, text: str, within: float = 8.0, step: float = 0.25) -> bool:
        """Let the loop run until `text` is on its screen (like waiting for `capture-pane` to show it)."""
        end = self.clock.now + within
        while self.clock.now < end:
            self.clock.now += step
            if self.poller.due():
                self.poller.poll()
            if text in self.screen():
                return True
        return False


def start_session_with_sidebar(server: TmuxWorld, tmp_path: Path, name: str, *, mains: int = 1) -> tuple[str, Sidebar]:
    """A session whose main panes wait for Enter (`read`), plus a running sidebar. Returns the window id."""
    server.tmux("new-session", "-d", "-s", name, "-x", "100", "-y", "30", "read x")
    for _ in range(mains - 1):
        server.tmux("split-window", "-d", "-t", f"={name}:", "read x")
    window = server.tmux("display-message", "-p", "-t", f"={name}:", "#{window_id}")
    env = env_for(server, tmp_path)
    assert cli.main(["open", "--target", window], env) == 0
    assert sidebar_count(server, window) == 1
    pane = next(line.split()[0] for line in roles_of(server, window) if line.endswith(" sidebar"))
    sidebar = Sidebar(server, pane)
    assert not sidebar.run_for(2.5)  # a refresh (1.5 s) has seen the main pane
    return window, sidebar


def sessions(server: TmuxWorld) -> list[str]:
    return server.tmux("list-sessions", "-F", "#{session_name}").split()


def main_panes(server: TmuxWorld, window: str) -> list[str]:
    return [line.split()[0] for line in roles_of(server, window) if not line.endswith(" sidebar")]


def test_sidebar_and_session_end_when_the_main_pane_exits(server: TmuxWorld, tmp_path: Path):
    window, sidebar = start_session_with_sidebar(server, tmp_path, "gamma")
    server.program_exits(main_panes(server, window)[0])  # Enter in the main pane ends `read`
    assert sidebar.run_for(Config().refresh_seconds)  # it notices on its next refresh
    assert "gamma" not in sessions(server)
    assert sessions(server) == ["alpha", "beta"]


def test_sidebar_stays_while_another_main_pane_lives(server: TmuxWorld, tmp_path: Path):
    window, sidebar = start_session_with_sidebar(server, tmp_path, "gamma", mains=2)
    first, second = main_panes(server, window)
    server.program_exits(first)
    assert main_panes(server, window) == [second]
    assert not sidebar.run_for(3.5)
    assert sidebar_count(server, window) == 1 and "gamma" in sessions(server)
    assert main_panes(server, window) == [second]


def test_close_alone_off_keeps_the_sidebar_and_the_session(server: TmuxWorld, tmp_path: Path):
    server.tmux("set-option", "-g", "@sebside-close-alone", "off")
    window, sidebar = start_session_with_sidebar(server, tmp_path, "gamma")
    server.program_exits(main_panes(server, window)[0])
    assert main_panes(server, window) == []
    assert not sidebar.run_for(3.5)
    assert sidebar_count(server, window) == 1 and "gamma" in sessions(server)


def test_fake_bridge_closes_its_pipes(server: TmuxWorld):
    before = len(os.listdir("/dev/fd"))
    source_plugin(server)
    assert len(os.listdir("/dev/fd")) == before


# Groups --------------------------------------------------------------------------------------------------------


def test_group_option_on_a_session_is_inherited_by_its_panes(server: TmuxWorld):
    server.tmux("set-option", "-t", "alpha", OPTION_GROUP, "g1")
    server.tmux("split-window", "-d", "-t", "=alpha:", "sleep 600")
    panes = Tmux(Path(server.socket), server).list_panes()
    assert panes is not None
    assert sorted((p.session, p.group) for p in panes) == [("alpha", "g1"), ("alpha", "g1"), ("beta", "")]


def grouped_server(server: TmuxWorld, tmp_path: Path) -> dict[str, str]:
    """alpha in group g1, beta in group g2, gamma without a group; a card and a closed card in each group."""
    server.tmux("new-session", "-d", "-s", "gamma", "-x", "100", "-y", "30", "sleep 600")
    server.tmux("set-option", "-t", "alpha", OPTION_GROUP, "g1")
    server.tmux("set-option", "-t", "beta", OPTION_GROUP, "g2")
    for session, card in (("alpha", "one"), ("beta", "two")):
        pane = server.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")
        assert cli.main(["card", "set", "--id", card, "--state", "working"], env_for(server, tmp_path, pane)) == 0
    env = env_for(server, tmp_path)
    for card, group in (("gone-1", "g1"), ("gone-2", "g2")):
        assert cli.main(["card", "set", "--id", card, "--state", "done", "--group", group], env) == 0
        assert cli.main(["card", "clear", "--id", card], env) == 0
    return env


def test_each_sidebar_shows_its_own_group(server: TmuxWorld, tmp_path: Path):
    env = grouped_server(server, tmp_path)
    store = Store.from_env(env)
    seen: dict[str, tuple[list[str], list[str]]] = {}
    for session in ("alpha", "beta", "gamma"):
        own = server.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")
        app = App(Config(), cli.Tmux.create(Path(server.socket)), store, home=str(tmp_path), own_pane=own)
        app.refresh()
        live = sorted(card.card_id or card.title for card in app.board.live)
        seen[session] = (live, sorted(card.card_id for card in app.board.history))
    assert seen["alpha"] == (["one"], ["gone-1"])
    assert seen["beta"] == (["two"], ["gone-2"])
    assert seen["gamma"] == (["gamma", "one", "two"], ["gone-1", "gone-2"])  # no group: everything, as before


def open_sidebar(server: TmuxWorld, env: dict[str, str], window: str) -> Sidebar:
    """`sebside open` adds the pane; tmux starts the sidebar program in it."""
    assert cli.main(["open", "--target", window], env) == 0
    pane = next(line.split()[0] for line in roles_of(server, window) if line.endswith(" sidebar"))
    return Sidebar(server, pane)


def footer_server(server: TmuxWorld, tmp_path: Path) -> dict[str, str]:
    """`grouped_server` with a footer for g1, the sidebar processes reading the same state."""
    env = grouped_server(server, tmp_path)
    assert cli.main(["group", "set", "--id", "g1", "--title", "Backend team", "--note", "review Thu"], env) == 0
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        server.tmux("set-environment", "-g", name, env[name])  # the sidebar processes read the same state
    return env


# One scenario (sidebars in alpha and gamma) in two tests, so each stays far under the CPU limit.
def test_real_sidebar_shows_its_group_and_footer(server: TmuxWorld, tmp_path: Path):
    env = footer_server(server, tmp_path)
    sidebar = open_sidebar(server, env, server.tmux("display-message", "-p", "-t", "=alpha:", "#{window_id}"))
    assert sidebar.shows("Backend team")
    alpha = sidebar.screen()
    assert "review Thu" in alpha and "gone-1" in alpha
    assert "beta" not in alpha and "gamma" not in alpha and "gone-2" not in alpha
    assert cli.main(["group", "set", "--id", "g1", "--title", "Renamed"], env) == 0
    assert sidebar.shows("Renamed", within=0.25)  # the dir stamp: at the next frame tick


def test_real_sidebar_without_a_group_shows_everything_and_no_footer(server: TmuxWorld, tmp_path: Path):
    env = footer_server(server, tmp_path)
    sidebar = open_sidebar(server, env, server.tmux("display-message", "-p", "-t", "=gamma:", "#{window_id}"))
    assert sidebar.shows("gone-2")
    gamma = sidebar.screen()
    assert "Backend team" not in gamma and "gone-1" in gamma and "alpha" in gamma


def test_real_sidebar_redraws_after_card_update(server: TmuxWorld, tmp_path: Path):
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    set_ = ["card", "set", "--id", "job", "--state", "working", "--title", "Deploy"]
    assert cli.main(set_, env_for(server, tmp_path, pane)) == 0
    env = env_for(server, tmp_path)
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        server.tmux("set-environment", "-g", name, env[name])  # the sidebar process reads the same state
    window = server.tmux("display-message", "-p", "-t", "=alpha:", "#{window_id}")
    sidebar = open_sidebar(server, env, window)
    assert sidebar.shows("Deploy")
    assert "step 3 of 5" not in sidebar.screen()
    assert cli.main(["card", "update", "--id", "job", "--subtitle", "step 3 of 5"], env) == 0
    assert sidebar.shows("step 3 of 5", within=0.25)  # the dir stamp: at the next frame tick
    assert "Deploy" in sidebar.screen()
