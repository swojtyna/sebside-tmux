from pathlib import Path

import pytest

from fakes import FakePane, FakeTmuxServer
from sebside.errors import ForbiddenCommand
from sebside.model import OPTION_ID, OPTION_ROLE
from sebside.tmux import (
    ALLOWED,
    FIELD_SEP,
    PANE_FORMAT,
    RECORD_SEP,
    UNINSTALL_EXTRA,
    Completed,
    Tmux,
    check_command,
    parse_panes,
    server_from_env,
)


def test_allowlist_is_exactly_read_navigate_resume_and_own_options():
    assert {
        "list-panes",
        "display-message",
        "show-options",
        "set-option",
        "switch-client",
        "select-window",
        "select-pane",
        "new-session",
    } == ALLOWED
    assert {"set-hook", "list-keys", "unbind-key", "kill-pane"} == UNINSTALL_EXTRA
    assert not ALLOWED & UNINSTALL_EXTRA


@pytest.mark.parametrize(
    "command",
    [
        "send-keys",
        "kill-session",
        "kill-server",
        "kill-pane",
        "kill-window",
        "run-shell",
        "if-shell",
        "respawn-pane",
        "pipe-pane",
        "source-file",
        "set-hook",
        "bind-key",
        "unbind-key",
        "split-window",
        "new-window",
        "capture-pane",
        "set-buffer",
        "load-buffer",
    ],
)
def test_dangerous_commands_are_refused_before_running(command):
    calls: list[list[str]] = []
    client = Tmux(None, lambda argv, timeout: calls.append(argv) or Completed(0, "", ""))
    with pytest.raises(ForbiddenCommand):
        client.run(command, "-t", "x")
    assert calls == []


def test_uninstall_extras_are_narrow():
    extended = ALLOWED | UNINSTALL_EXTRA
    check_command("set-hook", ("-gu", "session-closed[97]"), extended)
    check_command("kill-pane", ("-t", "%12"), extended)
    for command, arguments in [
        ("set-hook", ("-g", "session-closed[97]", "run-shell x")),
        ("set-hook", ("-gu", "session-closed[0]")),
        ("kill-pane", ("-a", "-t", "%1")),
        ("kill-pane", ("-t", "work")),
    ]:
        with pytest.raises(ForbiddenCommand):
            check_command(command, arguments, extended)


def test_argument_ending_in_semicolon_is_refused():
    with pytest.raises(ForbiddenCommand):
        check_command("switch-client", ("-t", "a;"))


def test_shell_format_is_refused_outside_set_option():
    with pytest.raises(ForbiddenCommand):
        check_command("list-panes", ("-a", "-F", "#(rm -rf ~)"))
    with pytest.raises(ForbiddenCommand):
        check_command("new-session", ("-d", "-s", "x", "-c", "#(id)", "--", "true"))


def test_new_session_needs_argv_after_double_dash():
    check_command("new-session", ("-d", "-s", "x", "--", "vim", "notes.md"))
    with pytest.raises(ForbiddenCommand):
        check_command("new-session", ("-d", "-s", "x", "vim notes.md"))


def test_display_message_only_prints():
    with pytest.raises(ForbiddenCommand):
        check_command("display-message", ("-t", "%1", "hello"))
    check_command("display-message", ("-p", "-t", "%1", "#{pane_id}"))


def test_set_option_only_pane_scoped_sebside_options():
    check_command("set-option", ("-p", "-t", "%1", "@sebside_state", "working"))
    check_command("set-option", ("-p", "-u", "-t", "%1", "@sebside_id"))
    check_command("set-option", ("-p", "-t", "%1", "@sebside_id", "-5"))
    for bad in [
        ("-g", "@sebside_state", "x"),
        ("-t", "%1", "@sebside_state", "x"),
        ("-p", "-t", "%1", "status", "off"),
        ("-p", "-t", "%1", "@card_state", "x"),
        ("-p", "-t", "%1", "@sebside-key", "x"),
        ("-p", "-F", "-t", "%1", "@sebside_id", "#{pane_id}"),
        ("-pg", "-t", "%1", "@sebside_id", "x"),
    ]:
        with pytest.raises(ForbiddenCommand):
            check_command("set-option", bad)


def test_argv_uses_private_socket_when_given():
    calls: list[list[str]] = []
    client = Tmux(Path("/tmp/x.sock"), lambda argv, timeout: calls.append(argv) or Completed(0, "", ""), "tmux")
    client.list_panes()
    assert calls == [["tmux", "-S", "/tmp/x.sock", "list-panes", "-a", "-F", PANE_FORMAT]]


def test_format_is_built_from_field_list():
    for name in ("session_name", "pane_id", "pane_current_path", "window_activity", "socket_path", "pid"):
        assert "#{" + name + "}" in PANE_FORMAT or ":" + name + "}" in PANE_FORMAT
    assert ":" + OPTION_ID + "}" in PANE_FORMAT and ":" + OPTION_ROLE + "}" in PANE_FORMAT
    assert "@card_" not in PANE_FORMAT


def test_parse_panes():
    server = FakeTmuxServer(
        [
            FakePane("%1", "api", cwd="/srv/api", command="npm", attached=True),
            FakePane("%2", "build", options={OPTION_ID: "b1"}),
            FakePane("%3", "build", options={OPTION_ROLE: "sidebar"}),
        ]
    )
    panes = parse_panes("".join(server.record(p) + "\n" for p in server.panes))
    assert [p.pane_id for p in panes] == ["%1", "%2", "%3"]
    first = panes[0]
    assert (first.session, first.cwd, first.command, first.session_attached) == ("api", "/srv/api", "npm", True)
    assert (first.socket, first.server_pid) == ("/tmp/fake.sock", 4242)
    assert panes[1].card_id == "b1"
    assert panes[2].is_sidebar and not panes[1].is_sidebar


def test_malformed_record_is_skipped_not_guessed():
    server = FakeTmuxServer([FakePane("%1", "ok")])
    broken = "only" + FIELD_SEP + "three" + FIELD_SEP + "fields" + RECORD_SEP
    assert [p.pane_id for p in parse_panes(broken + "\n" + server.record(server.panes[0]))] == ["%1"]


def test_list_panes_no_server_is_empty_and_other_errors_unknown():
    assert Tmux(None, lambda a, t: Completed(1, "", "no server running on /tmp/x")).list_panes() == []
    assert Tmux(None, lambda a, t: Completed(124, "", "tmux timed out")).list_panes() is None


def test_server_from_env():
    server = server_from_env({"TMUX": "/tmp/tmux-1/default,1234,0"})
    assert server is not None and (server.socket, server.pid) == ("/tmp/tmux-1/default", 1234)
    assert server_from_env({}) is None


def test_show_switches_then_selects_window_and_pane(server, tmux):
    assert tmux.show("work", "2", "%7").ok
    assert server.commands() == ["switch-client", "select-window", "select-pane"]
    assert server.calls[0][-1] == "=work"
    assert server.calls[1][-1] == "=work:2"


def test_show_stops_when_switch_fails(server, tmux):
    server.fail.add("switch-client")
    assert not tmux.show("work", "2", "%7").ok
    assert server.commands() == ["switch-client"]


def test_new_session_passes_argv_without_shell(server, tmux):
    tmux.new_session("job", "/tmp", ("vim", "a b.txt"))
    assert server.calls[-1][3:] == ["new-session", "-d", "-s", "job", "-c", "/tmp", "--", "vim", "a b.txt"]


def test_prefix_falls_back_to_c_b():
    assert Tmux(None, lambda argv, timeout: Completed(1, "", "no server")).prefix() == "C-b"
