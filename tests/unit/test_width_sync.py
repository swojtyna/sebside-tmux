from sebside.model import OPTION_SHARED_WIDTH
from sebside.tui import WidthSync


class Server:
    """A global option table and the resize calls, as tmux would see them."""

    def __init__(self, shared: str = "") -> None:
        self.options = {OPTION_SHARED_WIDTH: shared} if shared else {}
        self.calls: list[tuple[str, ...]] = []

    def option(self, name: str) -> str:
        return self.options.get(name, "")

    def run(self, subcommand: str, *arguments: str):
        self.calls.append((subcommand, *arguments))
        if subcommand == "set-option":
            self.options[arguments[1]] = arguments[2]


def test_first_sidebar_publishes_and_others_follow_a_drag():
    server = Server()
    one, two = WidthSync(), WidthSync()
    one.tick(server, "%1", 38)
    assert server.options[OPTION_SHARED_WIDTH] == "38"
    two.tick(server, "%2", 38)
    assert ("resize-pane", "-t", "%2", "-x", "38") not in server.calls
    one.tick(server, "%1", 50)                       # the user drags sidebar one
    assert server.options[OPTION_SHARED_WIDTH] == "50"
    two.tick(server, "%2", 38)
    assert server.calls[-1] == ("resize-pane", "-t", "%2", "-x", "50")
    two.tick(server, "%2", 50)                       # its own resize: not published again
    assert server.calls[-1] == ("resize-pane", "-t", "%2", "-x", "50")


def test_a_new_sidebar_takes_the_shared_width_and_a_clamped_resize_is_not_published():
    server = Server("50")
    narrow = WidthSync()
    narrow.tick(server, "%3", 36)
    assert server.calls == [("resize-pane", "-t", "%3", "-x", "50")]
    narrow.tick(server, "%3", 40)                    # tmux clamped it in a narrow window
    narrow.tick(server, "%3", 40)
    assert server.options[OPTION_SHARED_WIDTH] == "50" and len(server.calls) == 1
    narrow.tick(server, "%3", 30)                    # now the user drags it
    assert server.options[OPTION_SHARED_WIDTH] == "30"


def test_the_allowlist_lets_only_the_shared_width_and_an_own_pane_resize_through():
    import pytest

    from sebside.errors import ForbiddenCommand
    from sebside.tmux import check_command

    check_command("set-option", ("-g", OPTION_SHARED_WIDTH, "44"))
    check_command("resize-pane", ("-t", "%3", "-x", "44"))
    for subcommand, arguments in [("set-option", ("-g", OPTION_SHARED_WIDTH, "4;4")), ("set-option", ("-g", "status", "off")),
                                  ("resize-pane", ("-t", "%3", "-y", "44")), ("resize-pane", ("-t", "main", "-x", "44"))]:
        with pytest.raises(ForbiddenCommand):
            check_command(subcommand, arguments)


def test_reexec_retries_while_the_tool_environment_is_rebuilt():
    import pytest

    from sebside.tui import REEXEC_TRIES, reexec

    calls, naps = [], []

    def flaky(path, argv):
        calls.append(path)
        if len(calls) < 3:
            raise FileNotFoundError(path)

    reexec(flaky, naps.append)
    assert len(calls) == 3 and len(naps) == 2

    def gone(path, argv):
        raise FileNotFoundError(path)

    with pytest.raises(OSError):
        reexec(gone, lambda seconds: None)
    assert REEXEC_TRIES >= 10
