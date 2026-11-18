"""The only module that runs an external program: `tmux`.

Safety rules, enforced before any process starts:

- `ALLOWED` is the complete list of tmux subcommands the sidebar and `card` commands may run. sebside never types
  into a pane (no `send-keys`), never runs a shell through tmux (no `run-shell`, `if-shell`, `#(...)` formats) and
  never kills anything. `sebside uninstall` alone gets `UNINSTALL_EXTRA` (remove its hooks, key and sidebar panes) and
  `sebside open` alone gets `OPEN_EXTRA` (one `split-window` that starts the sidebar program in a new pane).
- `set-option` only touches pane-scoped `@sebside_*` options and never with `-F`. Exceptions: `remain-on-exit off`
  on a pane (a resumed program's pane and the sidebar's own pane close when their program exits) and the session
  marker `@sebside_resumed 1` on a session sebside has just resumed (its sidebar sends the client back on close),
  and the global `@sebside_shared_width <columns>` (one width for every sidebar).
- `resize-pane` only as `-t %N -x <columns>`: a sidebar follows the shared width with its own pane.
- `display-message` only with `-p` (print to stdout, nothing shown in a client).
- An argument ending in `;` is refused, because tmux would read it as a second command.
- `new-session` is used for exactly one thing: resuming a closed card as an argv list (no shell), detached.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .errors import ForbiddenCommand
from .model import OPTION_ID, OPTION_RESUMED, OPTION_ROLE, OPTION_SHARED_WIDTH, SIDEBAR_ROLE, Pane, Server

if TYPE_CHECKING:
    from pathlib import Path

ALLOWED: frozenset[str] = frozenset(
    {
        "list-panes",  # the one read the sidebar does every poll
        "display-message",  # -p only: read formats for one pane
        "show-options",  # read the prefix key for the hints
        "set-option",  # pane-scoped @sebside_* only
        "switch-client",  # Enter: show the card's session
        "select-window",  # Enter: show the card's window
        "select-pane",  # Enter: focus the card's pane
        "new-session",  # Enter on a closed card: run its resume argv, detached
        "resize-pane",  # -t %N -x N only: the sidebar follows the shared width
    }
)
UNINSTALL_EXTRA: frozenset[str] = frozenset({"set-hook", "list-keys", "unbind-key", "kill-pane"})
OPEN_EXTRA: frozenset[str] = frozenset({"split-window"})  # `sebside open` alone: add the sidebar pane
OWN_HOOKS: frozenset[str] = frozenset({"session-closed[97]", "session-created[96]", "after-new-window[98]"})
OWN_OPTION_PREFIX = "@sebside_"
TIMEOUT_S = 5.0
PANE_ID = re.compile(r"^%[0-9]+$")


@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def error(self) -> str:
        return " ".join(self.stderr.split())[:160] or f"tmux exited with {self.returncode}"


# argv, timeout -> result. A timeout is reported as returncode 124, a missing binary as 127.
Runner = Callable[[list[str], float], Completed]


def subprocess_runner(argv: list[str], timeout: float) -> Completed:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False
        )
    except subprocess.TimeoutExpired:
        return Completed(124, "", "tmux timed out")
    except OSError as error:
        return Completed(127, "", str(error))
    return Completed(done.returncode, done.stdout, done.stderr)


# allowlist ---------------------------------------------------------------------------------------
def split_flags(
    arguments: tuple[str, ...], with_value: frozenset[str] = frozenset({"-t"})
) -> tuple[list[str], list[str]]:
    """Flags come before the first positional argument (or `--`); a value like "-5" later stays positional."""
    flags: list[str] = []
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not positional and argument == "--":
            positional.extend(arguments[index + 1 :])
            break
        if not positional and argument.startswith("-") and len(argument) > 1:
            flags.append(argument)
            if argument in with_value:
                index += 1
        else:
            positional.append(argument)
        index += 1
    return flags, positional


def check_command(subcommand: str, arguments: tuple[str, ...], allowed: frozenset[str] = ALLOWED) -> None:
    """Raise `ForbiddenCommand` unless the call is on the allowlist and its arguments are harmless."""
    if subcommand not in allowed:
        raise ForbiddenCommand(f"tmux {subcommand!r} is not allowed: allowed are {', '.join(sorted(allowed))}")
    for argument in arguments:
        if argument.rstrip().endswith(";"):
            raise ForbiddenCommand(f"argument {argument!r} would chain another tmux command")
    if subcommand != "set-option":
        for argument in arguments:
            if "#(" in argument:
                raise ForbiddenCommand(f"argument {argument!r} would run a shell command through a format")
    if subcommand == "display-message" and "-p" not in arguments:
        raise ForbiddenCommand("display-message only with -p")
    if subcommand == "set-option":
        _check_set_option(arguments)
    if subcommand == "resize-pane":
        _check_resize_pane(arguments)
    if subcommand == "new-session":
        if "--" not in arguments:
            raise ForbiddenCommand("new-session only with an argv after --")
        options = arguments[: arguments.index("--")]
        value = options[options.index("-c") + 1] if "-c" in options[:-1] else ""
        if "#" in value:
            raise ForbiddenCommand("new-session -c value must not contain '#' (tmux would expand it as a format)")
    if subcommand == "set-hook":
        flags, positional = split_flags(arguments)
        if flags not in (["-gu"], ["-g", "-u"], ["-u", "-g"]) or len(positional) != 1 or positional[0] not in OWN_HOOKS:
            raise ForbiddenCommand(f"set-hook only as -gu to remove sebside's own hooks {sorted(OWN_HOOKS)}")
    if subcommand == "split-window":
        _check_split_window(arguments)
    if subcommand == "kill-pane" and (len(arguments) != 2 or arguments[0] != "-t" or not PANE_ID.match(arguments[1])):
        raise ForbiddenCommand("kill-pane only as -t %N (sebside's own sidebar panes)")


def _check_split_window(arguments: tuple[str, ...]) -> None:
    flags, positional = split_flags(arguments, frozenset({"-t", "-l", "-F"}))
    if any(flag not in ("-h", "-b", "-f", "-d", "-P", "-t", "-l", "-F") for flag in flags):
        raise ForbiddenCommand(f"split-window flags {flags} not allowed")
    if "-t" not in flags or len(positional) != 1:
        raise ForbiddenCommand("split-window only with -t and exactly one command (the sidebar program)")


def _check_resize_pane(arguments: tuple[str, ...]) -> None:
    flags, positional = split_flags(arguments, frozenset({"-t", "-x"}))
    target = arguments[arguments.index("-t") + 1] if "-t" in arguments[:-1] else ""
    columns = arguments[arguments.index("-x") + 1] if "-x" in arguments[:-1] else ""
    if sorted(flags) != ["-t", "-x"] or positional or not PANE_ID.match(target) or not columns.isdigit():
        raise ForbiddenCommand("resize-pane only as -t %N -x <columns>")


def _check_set_option(arguments: tuple[str, ...]) -> None:
    flags, positional = split_flags(arguments)
    if positional == [OPTION_RESUMED, "1"] and flags == ["-t"]:
        return  # session marker of a session sebside has just resumed
    if flags == ["-g"] and len(positional) == 2 and positional[0] == OPTION_SHARED_WIDTH and positional[1].isdigit():
        return  # the one global sebside writes: the width every sidebar follows
    if "-p" not in flags:
        raise ForbiddenCommand("set-option only with -p (pane scope)")
    if any(flag not in ("-p", "-u", "-q", "-t") for flag in flags):
        raise ForbiddenCommand(f"set-option flags {flags} not allowed: only -p, -u, -q, -t")
    if positional == ["remain-on-exit", "off"] and "-u" not in flags and "-p" in flags:
        return
    if not positional or not positional[0].startswith(OWN_OPTION_PREFIX):
        raise ForbiddenCommand(f"set-option may only set {OWN_OPTION_PREFIX}* options (or remain-on-exit off)")


# list-panes format, built from a field list ------------------------------------------------------
FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
PANE_FIELDS: tuple[str, ...] = (
    "session_name",
    "window_index",
    "pane_id",
    "pane_current_path",
    "pane_current_command",
    "window_activity",
    "session_attached",
    "window_active",
    "pane_active",
    "socket_path",
    "pid",  # the server's pid
    OPTION_ID,
    OPTION_ROLE,
)
# Free-text fields can contain anything a user or program puts there, including our separators, so tmux replaces
# them with "?" before printing (format substitution, `#{s/regex/replacement/:field}`). Numeric and id fields are
# validated after parsing instead; a record that fails validation is skipped.
FREE_TEXT_FIELDS: frozenset[str] = frozenset(
    {"session_name", "pane_current_path", "pane_current_command", "socket_path", OPTION_ID, OPTION_ROLE}
)


def format_field(name: str) -> str:
    if name in FREE_TEXT_FIELDS:
        return "#{s/[" + FIELD_SEP + RECORD_SEP + "]/?/:" + name + "}"
    return "#{" + name + "}"


PANE_FORMAT: str = FIELD_SEP.join(format_field(name) for name in PANE_FIELDS) + RECORD_SEP
DIGITS = re.compile(r"^[0-9]+$")
NUMERIC_FIELDS: tuple[str, ...] = ("window_index", "window_activity", "session_attached", "pid")


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def parse_panes(output: str) -> list[Pane]:
    """tmux prints option values raw, so records end with RS and a record with the wrong field count is skipped."""
    panes: list[Pane] = []
    for raw in output.split(RECORD_SEP):
        record = raw.lstrip("\n")
        if not record:
            continue
        values = record.split(FIELD_SEP)
        if len(values) != len(PANE_FIELDS):
            continue
        v = dict(zip(PANE_FIELDS, values, strict=True))
        if not PANE_ID.match(v["pane_id"]) or not all(DIGITS.match(v[name]) for name in NUMERIC_FIELDS):
            continue
        panes.append(
            Pane(
                session=v["session_name"],
                window=v["window_index"],
                pane_id=v["pane_id"],
                cwd=v["pane_current_path"],
                command=v["pane_current_command"],
                activity=_int(v["window_activity"]),
                session_attached=_int(v["session_attached"]) > 0,
                window_active=v["window_active"] == "1",
                pane_active=v["pane_active"] == "1",
                socket=v["socket_path"],
                server_pid=_int(v["pid"]),
                card_id=v[OPTION_ID],
                role=v[OPTION_ROLE],
            )
        )
    return panes


def server_from_env(env: Mapping[str, str]) -> Server | None:
    """`$TMUX` is `<socket>,<server pid>,<session index>`."""
    parts = env.get("TMUX", "").split(",")
    if len(parts) < 2 or not parts[0]:
        return None
    return Server(parts[0], _int(parts[1]))


# client ------------------------------------------------------------------------------------------
class TmuxLike(Protocol):
    @property
    def socket(self) -> Path | None: ...

    def run(self, subcommand: str, *arguments: str) -> Completed: ...
    def list_panes(self) -> list[Pane] | None: ...
    def display(self, target: str, fmt: str) -> Completed: ...
    def prefix(self) -> str: ...
    def set_pane_option(self, pane: str, name: str, value: str) -> Completed: ...
    def unset_pane_option(self, pane: str, name: str) -> Completed: ...
    def show(self, session: str, window: str, pane: str, *, focus_main: bool = False) -> Completed: ...
    def option(self, name: str) -> str: ...
    def sidebar_panes(self, window: str) -> list[str] | None: ...
    def open_sidebar(self, window: str, width: str, command: str) -> Completed: ...
    def new_session(self, name: str, cwd: str | None, argv: tuple[str, ...]) -> Completed: ...
    def resumed(self, session: str) -> bool: ...
    def back(self) -> Completed: ...


@dataclass(frozen=True)
class Tmux:
    """A tmux client. `socket=None` means plain `tmux`, which inside tmux talks to the server of `$TMUX`."""

    socket: Path | None = None
    runner: Runner = field(default=subprocess_runner, repr=False)
    binary: str = "tmux"
    timeout: float = TIMEOUT_S
    allowed: frozenset[str] = ALLOWED

    @classmethod
    def create(cls, socket: Path | None = None, *, uninstall: bool = False, opener: bool = False) -> Tmux:
        allowed = ALLOWED | (UNINSTALL_EXTRA if uninstall else frozenset()) | (OPEN_EXTRA if opener else frozenset())
        return cls(socket, subprocess_runner, shutil.which("tmux") or "tmux", allowed=allowed)

    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    def argv(self, subcommand: str, *arguments: str) -> list[str]:
        base = [self.binary]
        if self.socket is not None:
            base += ["-S", os.fspath(self.socket)]
        return [*base, subcommand, *arguments]

    def run(self, subcommand: str, *arguments: str) -> Completed:
        check_command(subcommand, arguments, self.allowed)
        return self.runner(self.argv(subcommand, *arguments), self.timeout)

    def list_panes(self) -> list[Pane] | None:
        """All panes of the server; [] when no server runs; None when tmux did not answer properly."""
        done = self.run("list-panes", "-a", "-F", PANE_FORMAT)
        if done.ok:
            return parse_panes(done.stdout)
        if any(text in done.stderr for text in ("no server", "error connecting", "No such file")):
            return []
        return None

    def display(self, target: str, fmt: str) -> Completed:
        return self.run("display-message", "-p", "-t", target, fmt)

    def prefix(self) -> str:
        done = self.run("show-options", "-gqv", "prefix")
        return done.stdout.strip() if done.ok and done.stdout.strip() else "C-b"

    def set_pane_option(self, pane: str, name: str, value: str) -> Completed:
        return self.run("set-option", "-p", "-t", pane, name, value)

    def unset_pane_option(self, pane: str, name: str) -> Completed:
        return self.run("set-option", "-p", "-u", "-t", pane, name)

    def show(self, session: str, window: str, pane: str, *, focus_main: bool = False) -> Completed:
        """Switch the attached client to `session`, then its window and pane. Nothing is typed anywhere.
        With `focus_main` the cursor lands in a pane that is not a sidebar (the card's pane when it is one)."""
        done = self.run("switch-client", "-t", f"={session}")
        if not done.ok:
            return done
        target = f"={session}:{window}"
        if window:
            self.run("select-window", "-t", target)
        if focus_main:
            pane = self._main_pane(target, pane)
        if pane:
            self.run("select-pane", "-t", pane)
        return done

    def _main_pane(self, window: str, preferred: str) -> str:
        listed = self.run("list-panes", "-t", window, "-F", "#{pane_id} #{@sebside_role}")
        if not listed.ok:
            return preferred
        rows = [line.split() for line in listed.stdout.splitlines() if line.split()]
        main = [row[0] for row in rows if row[1:] != ["sidebar"]]
        return preferred if preferred in main else (main[0] if main else preferred)

    def option(self, name: str) -> str:
        """A global tmux option (empty when unset)."""
        done = self.run("show-options", "-gqv", name)
        return done.stdout.strip() if done.ok else ""

    def sidebar_panes(self, window: str) -> list[str] | None:
        """Sidebar pane ids of a window; None when tmux did not answer (window gone)."""
        done = self.run("list-panes", "-t", window, "-F", "#{pane_id} #{@sebside_role}")
        if not done.ok:
            return None
        rows = [line.split() for line in done.stdout.splitlines()]
        return [row[0] for row in rows if row[1:] == ["sidebar"]]

    def open_sidebar(self, window: str, width: str, command: str) -> Completed:
        """Add a sidebar pane on the left, full height, without taking focus; mark it with @sebside_role."""
        done = self.run(
            "split-window", "-h", "-b", "-f", "-d", "-l", width, "-t", window, "-P", "-F", "#{pane_id}", command
        )
        pane = done.stdout.strip()
        if done.ok and PANE_ID.match(pane):
            self.set_pane_option(pane, OPTION_ROLE, SIDEBAR_ROLE)
        return done

    def new_session(self, name: str, cwd: str | None, argv: tuple[str, ...]) -> Completed:
        arguments = ["-d", "-s", name]
        if cwd:
            arguments += ["-c", cwd]
        if len(argv) == 1:
            # tmux runs a single argument through `sh -c`; `env -- prog` keeps it a plain exec without a shell.
            argv = ("env", "--", argv[0])
        done = self.run("new-session", *arguments, "--", *argv)
        if done.ok:
            # A server with `remain-on-exit on` would keep the dead pane (and its window and session) after the
            # resumed program exits; a resumed card should go away with its program, like a fresh session does.
            self.run("set-option", "-p", "-t", f"={name}:", "remain-on-exit", "off")
            self.run("set-option", "-t", f"={name}:", OPTION_RESUMED, "1")
        return done

    def resumed(self, session: str) -> bool:
        """True when sebside resumed `session` from a closed card (its session marker is set)."""
        done = self.run("show-options", "-qv", "-t", f"={session}:", OPTION_RESUMED)
        return done.ok and done.stdout.strip() == "1"

    def back(self) -> Completed:
        """Send the client of the calling pane's session to its last session (where it came from)."""
        return self.run("switch-client", "-l")
