"""Integration tests against a real tmux binary on a private server.

Every test starts its own server on a socket in a short temporary directory (`tmux -S <dir>/s -f /dev/null`) and
kills only that server afterwards. The user's default tmux server is never contacted: `TMUX` is removed from the
environment and every call passes `-S`.
"""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sebside import cli
from sebside.cards import build
from sebside.config import Config
from sebside.model import OPTION_GROUP, OPTION_ID, OPTION_STATE, State
from sebside.store import Store
from sebside.tmux import Tmux
from sebside.tui import App

pytestmark = [
    pytest.mark.tmux,
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed"),
]


class Server:
    def __init__(self, socket: Path, env: dict[str, str]) -> None:
        self.socket = socket
        self.env = env

    def tmux(self, *args: str) -> str:
        done = subprocess.run(
            ["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
            check=True,
        )
        return done.stdout.strip()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    for name in ("TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
    short = Path(tempfile.mkdtemp(prefix="sbs-", dir="/tmp"))  # unix socket paths must stay short
    socket = short / "s"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "TERM": "xterm-256color"}
    handle = Server(socket, env)
    handle.tmux("new-session", "-d", "-s", "alpha", "-x", "100", "-y", "30", "sleep 600")
    handle.tmux("new-session", "-d", "-s", "beta", "-x", "100", "-y", "30", "sleep 600")
    try:
        yield handle
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"], capture_output=True, env=env, timeout=10, check=False
        )
        shutil.rmtree(short, ignore_errors=True)


def env_for(server: Server, tmp_path: Path, pane: str | None = None) -> dict[str, str]:
    env = {
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SEBSIDE_TMUX_SOCKET": str(server.socket),
        "PATH": os.environ.get("PATH", ""),
    }
    if pane:
        pid = server.tmux("display-message", "-p", "-t", pane, "#{pid}")
        env |= {"TMUX": f"{server.socket},{pid},0", "TMUX_PANE": pane}
    return env


def test_list_panes_reads_every_pane(server: Server):
    panes = Tmux.create(server.socket).list_panes()
    assert panes is not None
    assert sorted(p.session for p in panes) == ["alpha", "beta"]
    assert all(p.socket == str(server.socket) and p.server_pid > 0 for p in panes)


def test_card_set_end_to_end(server: Server, tmp_path: Path):
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    env = env_for(server, tmp_path, pane)
    assert cli.main(["card", "set", "--id", "job", "--state", "waiting", "--title", "Deploy"], env) == 0
    assert server.tmux("show-options", "-pqv", "-t", pane, OPTION_ID) == "job"
    assert server.tmux("show-options", "-pqv", "-t", pane, OPTION_STATE) == "waiting"
    store = Store.from_env(env)
    panes = Tmux.create(server.socket).list_panes()
    assert panes is not None
    board = build(panes, store.load_all(), Config(), time.time())
    titles = {card.title: card for card in board.live}
    assert titles["Deploy"].state is State.WAITING and titles["Deploy"].session == "alpha"
    assert "beta" in titles


def test_card_update_changes_text_only(server: Server, tmp_path: Path):
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


def test_closed_session_is_reconciled_into_history(server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pane = server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}")
    env = env_for(server, tmp_path, pane)
    cli.main(["card", "set", "--id", "b", "--state", "working", "--resume", "sleep 1"], env)
    server.tmux("kill-session", "-t", "=beta")  # the test harness, not sebside, closes the session
    later = time.time() + 10
    monkeypatch.setattr(cli.time, "time", lambda: later)
    plain = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")}
    assert cli.main(["reconcile"], plain) == 0
    record = Store.from_env(env).load("b")
    assert record is not None and record.closed


def test_resume_argv_runs_without_shell(server: Server):
    tmux = Tmux.create(server.socket)
    done = tmux.new_session("resumed", None, ("sh", "-c", "sleep 30", "ignored; echo not-a-command"))
    assert done.ok, done.stderr
    assert "resumed" in server.tmux("list-sessions", "-F", "#{session_name}").split()
    assert server.tmux("display-message", "-p", "-t", "=resumed:", "#{pane_current_command}") in ("sh", "sleep")


REPO = Path(__file__).resolve().parents[2]


def plugin_env(server: Server) -> dict[str, str]:
    pid = server.tmux("display-message", "-p", "#{pid}")
    bin_dir = str(Path(shutil.which("sebside") or REPO / ".venv" / "bin" / "sebside").parent)
    return {**server.env, "TMUX": f"{server.socket},{pid},0", "PATH": f"{bin_dir}:{server.env['PATH']}"}


def test_plugin_installs_hook_and_key_and_toggles_sidebar(server: Server):
    env = plugin_env(server)
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=env, check=True, timeout=20)
    assert "session-closed[97]" in server.tmux("show-hooks", "-g")
    assert "toggle.sh" in server.tmux("list-keys", "-T", "prefix", "S")
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    subprocess.run(["bash", str(REPO / "scripts" / "toggle.sh"), pane], env=env, check=True, timeout=20)
    roles = server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role}").split()
    assert roles == ["sidebar"]
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=env, check=True, timeout=20)  # reload is idempotent
    assert server.tmux("show-hooks", "-g").count("session-closed[97]") == 1
    subprocess.run(["bash", str(REPO / "scripts" / "toggle.sh"), pane], env=env, check=True, timeout=20)
    assert server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role}").split() == []


def test_uninstall_removes_hook_key_and_sidebar(server: Server, tmp_path: Path):
    env = plugin_env(server)
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=env, check=True, timeout=20)
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    subprocess.run(["bash", str(REPO / "scripts" / "toggle.sh"), pane], env=env, check=True, timeout=20)
    assert cli.main(["uninstall"], env_for(server, tmp_path)) == 0
    assert "session-closed[97]" not in server.tmux("show-hooks", "-g")
    keys = subprocess.run(
        ["tmux", "-S", str(server.socket), "list-keys", "-T", "prefix", "S"],
        capture_output=True,
        text=True,
        env=server.env,
        timeout=10,
        check=False,
    )
    assert "toggle.sh" not in keys.stdout
    assert server.tmux("list-panes", "-a", "-F", "#{@sebside_role}").split() == []


# Review regressions ---------------------------------------------------------------------------------------------


def test_session_closed_hook_really_closes_the_card(server: Server, tmp_path: Path):
    env = env_for(server, tmp_path)
    server.tmux("set-environment", "-g", "XDG_STATE_HOME", env["XDG_STATE_HOME"])
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=plugin_env(server), check=True, timeout=20)
    pane = server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}")
    assert cli.main(["card", "set", "--id", "hooked", "--state", "working"], env_for(server, tmp_path, pane)) == 0
    time.sleep(2.2)  # past the grace period that protects a card that was set a moment ago
    server.tmux("kill-session", "-t", "=beta")  # the test harness closes the session; the hook must notice
    store = Store.from_env(env)
    for _ in range(50):
        record = store.load("hooked")
        if record is not None and record.closed:
            break
        time.sleep(0.1)
    assert record is not None and record.closed


def test_hook_works_with_quote_space_and_hash_in_the_sebside_path(server: Server, tmp_path: Path):
    odd = tmp_path / "it's a #dir $HOME"
    odd.mkdir()
    real = shutil.which("sebside") or str(REPO / ".venv" / "bin" / "sebside")
    (odd / "sebside").symlink_to(real)
    env = env_for(server, tmp_path)
    server.tmux("set-environment", "-g", "XDG_STATE_HOME", env["XDG_STATE_HOME"])
    plugin = plugin_env(server)
    plugin["PATH"] = f"{odd}:{plugin['PATH']}"
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=plugin, check=True, timeout=20)
    assert "##dir" in server.tmux("show-hooks", "-g")
    pane = server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}")
    cli.main(["card", "set", "--id", "odd", "--state", "working"], env_for(server, tmp_path, pane))
    time.sleep(2.2)
    server.tmux("kill-session", "-t", "=beta")
    store = Store.from_env(env)
    for _ in range(50):
        record = store.load("odd")
        if record is not None and record.closed:
            break
        time.sleep(0.1)
    assert record is not None and record.closed


def test_invalid_width_option_falls_back_to_default(server: Server):
    env = plugin_env(server)
    server.tmux("set-option", "-g", "@sebside-width", "wide; rm -rf /")
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    subprocess.run(["bash", str(REPO / "scripts" / "toggle.sh"), pane], env=env, check=True, timeout=20)
    widths = server.tmux("list-panes", "-t", "=alpha:", "-F", "#{@sebside_role} #{pane_width}").splitlines()
    sidebar = [line.split()[1] for line in widths if line.startswith("sidebar")]
    assert sidebar == ["36"]


def test_separators_in_a_real_directory_cannot_forge_panes(server: Server, tmp_path: Path):
    evil = tmp_path / f"a{chr(0x1E)}%99{chr(0x1F)}b"
    evil.mkdir()
    server.tmux("new-session", "-d", "-s", "evil", "-c", str(evil), "sleep 600")
    panes = Tmux.create(server.socket).list_panes()
    assert panes is not None
    assert sorted(p.session for p in panes) == ["alpha", "beta", "evil"]
    assert all(p.pane_id != "%99" for p in panes)
    assert any(p.cwd.endswith("a?%99?b") for p in panes)


# Auto-open, focus and `sebside open` ---------------------------------------------------------------------------


def roles_of(server: Server, target: str) -> list[str]:
    return server.tmux("list-panes", "-t", target, "-F", "#{pane_id} #{@sebside_role}").splitlines()


def sidebar_count(server: Server, target: str) -> int:
    return sum(1 for line in roles_of(server, target) if line.endswith(" sidebar"))


def wait_for(check, seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def source_plugin(server: Server) -> None:
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=plugin_env(server), check=True, timeout=20)


def test_open_if_missing_opens_once_and_is_quiet(server: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
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


def test_auto_on_installs_hooks_at_fixed_indexes_and_reload_is_idempotent(server: Server):
    server.tmux("set-hook", "-g", "session-created[0]", "set-option -g @mine yes")
    server.tmux("set-option", "-g", "@sebside-auto", "on")
    source_plugin(server)
    source_plugin(server)
    hooks = server.tmux("show-hooks", "-g")
    assert hooks.count("session-created[96]") == 1 and hooks.count("after-new-window[98]") == 1
    assert "session-created[0]" in hooks and "session-closed[97]" in hooks
    assert "open --if-missing --target #{window_id}" in hooks


def test_auto_on_opens_the_sidebar_in_new_sessions_and_windows(server: Server):
    server.tmux("set-option", "-g", "@sebside-auto", "on")
    source_plugin(server)
    server.tmux("new-session", "-d", "-s", "gamma", "-x", "100", "-y", "30", "sleep 600")
    assert wait_for(lambda: sidebar_count(server, "=gamma:") == 1)
    server.tmux("new-window", "-d", "-t", "=gamma:", "sleep 600")
    assert wait_for(lambda: all(sidebar_count(server, f"=gamma:{i}") == 1 for i in (0, 1)))
    time.sleep(0.5)
    assert sidebar_count(server, "=gamma:0") == 1 and sidebar_count(server, "=gamma:1") == 1
    assert sidebar_count(server, "=alpha:") == 0  # existing sessions are left alone


def test_auto_off_removes_the_hooks_and_nothing_opens(server: Server):
    server.tmux("set-option", "-g", "@sebside-auto", "on")
    source_plugin(server)
    server.tmux("set-option", "-g", "@sebside-auto", "off")
    source_plugin(server)
    hooks = server.tmux("show-hooks", "-g")
    assert "session-created[96]" not in hooks and "after-new-window[98]" not in hooks
    assert "session-closed[97]" in hooks
    server.tmux("new-session", "-d", "-s", "delta", "-x", "100", "-y", "30", "sleep 600")
    time.sleep(1.0)
    assert sidebar_count(server, "=delta:") == 0


def test_auto_default_is_off(server: Server):
    source_plugin(server)
    hooks = server.tmux("show-hooks", "-g")
    assert "session-created[96]" not in hooks and "after-new-window[98]" not in hooks


def test_focus_main_after_switch_lands_in_the_main_pane(server: Server, tmp_path: Path):
    window = server.tmux("display-message", "-p", "-t", "=beta:", "#{window_id}")
    assert cli.main(["open", "--target", window], env_for(server, tmp_path)) == 0
    sidebar = next(line.split()[0] for line in roles_of(server, window) if line.endswith(" sidebar"))
    main = next(line.split()[0] for line in roles_of(server, window) if not line.endswith(" sidebar"))
    server.tmux("select-pane", "-t", sidebar)  # the cursor is in the sidebar of the target window
    master, slave = pty.openpty()
    client = subprocess.Popen(
        ["tmux", "-S", str(server.socket), "attach", "-t", "=alpha"],
        stdin=slave, stdout=slave, stderr=slave, env=server.env, start_new_session=True,
    )  # fmt: skip
    try:
        assert wait_for(lambda: server.tmux("list-clients").strip() != "")
        tmux = Tmux.create(server.socket)
        assert tmux.show("beta", "0", sidebar, focus_main=True).ok
        assert server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}") == main
        server.tmux("select-pane", "-t", sidebar)
        assert tmux.show("beta", "0", sidebar).ok  # default behaviour is unchanged
        assert server.tmux("display-message", "-p", "-t", "=beta:", "#{pane_id}") == sidebar
    finally:
        client.kill()
        client.wait(timeout=10)
        os.close(master)
        os.close(slave)


# Closing the sidebar when it is alone ------------------------------------------------------------------------


def start_session_with_sidebar(server: Server, tmp_path: Path, name: str, *, mains: int = 1) -> str:
    """A session whose main panes wait for Enter (`read`), plus a real running sidebar. Returns the window id."""
    server.tmux("new-session", "-d", "-s", name, "-x", "100", "-y", "30", "read x")
    for _ in range(mains - 1):
        server.tmux("split-window", "-d", "-t", f"={name}:", "read x")
    window = server.tmux("display-message", "-p", "-t", f"={name}:", "#{window_id}")
    env = env_for(server, tmp_path)
    bin_dir = str(Path(shutil.which("sebside") or REPO / ".venv" / "bin" / "sebside").parent)
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "SEBSIDE_BIN": f"{bin_dir}/sebside"}
    assert cli.main(["open", "--target", window], env) == 0
    assert wait_for(lambda: sidebar_count(server, window) == 1)
    time.sleep(2.5)  # a refresh (1.5 s) has seen the main pane
    return window


def sessions(server: Server) -> list[str]:
    return server.tmux("list-sessions", "-F", "#{session_name}").split()


def main_panes(server: Server, window: str) -> list[str]:
    return [line.split()[0] for line in roles_of(server, window) if not line.endswith(" sidebar")]


def test_sidebar_and_session_end_when_the_main_pane_exits(server: Server, tmp_path: Path):
    window = start_session_with_sidebar(server, tmp_path, "gamma")
    server.tmux("send-keys", "-t", main_panes(server, window)[0], "Enter")
    assert wait_for(lambda: "gamma" not in sessions(server))
    assert sessions(server) == ["alpha", "beta"]


def test_sidebar_stays_while_another_main_pane_lives(server: Server, tmp_path: Path):
    window = start_session_with_sidebar(server, tmp_path, "gamma", mains=2)
    first, second = main_panes(server, window)
    server.tmux("send-keys", "-t", first, "Enter")
    assert wait_for(lambda: main_panes(server, window) == [second])
    time.sleep(3.5)
    assert sidebar_count(server, window) == 1 and "gamma" in sessions(server)
    assert main_panes(server, window) == [second]


def test_close_alone_off_keeps_the_sidebar_and_the_session(server: Server, tmp_path: Path):
    server.tmux("set-option", "-g", "@sebside-close-alone", "off")
    window = start_session_with_sidebar(server, tmp_path, "gamma")
    server.tmux("send-keys", "-t", main_panes(server, window)[0], "Enter")
    assert wait_for(lambda: main_panes(server, window) == [])
    time.sleep(3.5)
    assert sidebar_count(server, window) == 1 and "gamma" in sessions(server)


# Groups --------------------------------------------------------------------------------------------------------


def test_group_option_on_a_session_is_inherited_by_its_panes(server: Server):
    server.tmux("set-option", "-t", "alpha", OPTION_GROUP, "g1")
    server.tmux("split-window", "-d", "-t", "=alpha:", "sleep 600")
    panes = Tmux.create(server.socket).list_panes()
    assert panes is not None
    assert sorted((p.session, p.group) for p in panes) == [("alpha", "g1"), ("alpha", "g1"), ("beta", "")]


def grouped_server(server: Server, tmp_path: Path) -> dict[str, str]:
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


def test_each_sidebar_shows_its_own_group(server: Server, tmp_path: Path):
    env = grouped_server(server, tmp_path)
    store = Store.from_env(env)
    seen: dict[str, tuple[list[str], list[str]]] = {}
    for session in ("alpha", "beta", "gamma"):
        own = server.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")
        app = App(Config(), Tmux.create(server.socket), store, home=str(tmp_path), own_pane=own)
        app.refresh()
        live = sorted(card.card_id or card.title for card in app.board.live)
        seen[session] = (live, sorted(card.card_id for card in app.board.history))
    assert seen["alpha"] == (["one"], ["gone-1"])
    assert seen["beta"] == (["two"], ["gone-2"])
    assert seen["gamma"] == (["gamma", "one", "two"], ["gone-1", "gone-2"])  # no group: everything, as before


def sidebar_text(server: Server, window: str) -> str:
    sidebar = next(line.split()[0] for line in roles_of(server, window) if line.endswith(" sidebar"))
    return server.tmux("capture-pane", "-p", "-t", sidebar)  # the test harness reads the screen, not sebside


def test_real_sidebars_show_their_group_and_footer(server: Server, tmp_path: Path):
    env = grouped_server(server, tmp_path)
    assert cli.main(["group", "set", "--id", "g1", "--title", "Backend team", "--note", "review Thu"], env) == 0
    bin_dir = str(Path(shutil.which("sebside") or REPO / ".venv" / "bin" / "sebside").parent)
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "SEBSIDE_BIN": f"{bin_dir}/sebside"}
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        server.tmux("set-environment", "-g", name, env[name])  # the sidebar processes read the same state
    windows = {s: server.tmux("display-message", "-p", "-t", f"={s}:", "#{window_id}") for s in ("alpha", "gamma")}
    for window in windows.values():
        assert cli.main(["open", "--target", window], env) == 0
    assert wait_for(lambda: "Backend team" in sidebar_text(server, windows["alpha"]))
    alpha = sidebar_text(server, windows["alpha"])
    assert "review Thu" in alpha and "gone-1" in alpha
    assert "beta" not in alpha and "gamma" not in alpha and "gone-2" not in alpha
    assert wait_for(lambda: "gone-2" in sidebar_text(server, windows["gamma"]))
    gamma = sidebar_text(server, windows["gamma"])
    assert "Backend team" not in gamma and "gone-1" in gamma and "alpha" in gamma
    assert cli.main(["group", "set", "--id", "g1", "--title", "Renamed"], env) == 0
    assert wait_for(lambda: "Renamed" in sidebar_text(server, windows["alpha"]))  # the dir stamp


def test_real_sidebar_redraws_after_card_update(server: Server, tmp_path: Path):
    pane = server.tmux("display-message", "-p", "-t", "=alpha:", "#{pane_id}")
    set_ = ["card", "set", "--id", "job", "--state", "working", "--title", "Deploy"]
    assert cli.main(set_, env_for(server, tmp_path, pane)) == 0
    env = env_for(server, tmp_path)
    bin_dir = str(Path(shutil.which("sebside") or REPO / ".venv" / "bin" / "sebside").parent)
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "SEBSIDE_BIN": f"{bin_dir}/sebside"}
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        server.tmux("set-environment", "-g", name, env[name])  # the sidebar process reads the same state
    window = server.tmux("display-message", "-p", "-t", "=alpha:", "#{window_id}")
    assert cli.main(["open", "--target", window], env) == 0
    assert wait_for(lambda: "Deploy" in sidebar_text(server, window))
    assert "step 3 of 5" not in sidebar_text(server, window)
    assert cli.main(["card", "update", "--id", "job", "--subtitle", "step 3 of 5"], env) == 0
    assert wait_for(lambda: "step 3 of 5" in sidebar_text(server, window))
    assert "Deploy" in sidebar_text(server, window)
