"""Test doubles: an in-memory tmux server that records every argv (`FakeTmuxServer`), a fake tmux that behaves like
real tmux on the paths sebside and its scripts use (`TmuxWorld`, `run_script`), and pane/record builders."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

from sebside.model import OPTION_GROUP, OPTION_ID, OPTION_ROLE, CardRecord, Pane, State, TmuxRef
from sebside.tmux import FIELD_SEP, PANE_FIELDS, RECORD_SEP, Completed

NOW = 1_800_000_000.0
SOCKET = "/tmp/fake.sock"
SERVER_PID = 4242


@dataclass
class FakePane:
    pane_id: str
    session: str
    window: str = "0"
    cwd: str = "/home/me/project"
    command: str = "zsh"
    activity: int = int(NOW) - 30
    attached: bool = False
    window_active: bool = True
    pane_active: bool = True
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeTmuxServer:
    panes: list[FakePane] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)
    socket: str = SOCKET
    pid: int = SERVER_PID
    tmux_options: dict[str, str] = field(default_factory=dict)

    def pane(self, pane_id: str) -> FakePane:
        return next(p for p in self.panes if p.pane_id == pane_id)

    @staticmethod
    def _in(pane: FakePane, target: str) -> bool:
        if target.startswith("@"):
            return target == f"@{pane.window}"
        session, _, window = target.lstrip("=").partition(":")
        return pane.session == session and (not window or window == pane.window)

    def record(self, pane: FakePane) -> str:
        values = {
            "session_name": pane.session,
            "window_index": pane.window,
            "pane_id": pane.pane_id,
            "pane_current_path": pane.cwd,
            "pane_current_command": pane.command,
            "window_activity": str(pane.activity),
            "session_attached": "1" if pane.attached else "0",
            "window_active": "1" if pane.window_active else "0",
            "pane_active": "1" if pane.pane_active else "0",
            "socket_path": self.socket,
            "pid": str(self.pid),
            OPTION_ID: pane.options.get(OPTION_ID, ""),
            OPTION_ROLE: pane.options.get(OPTION_ROLE, ""),
            OPTION_GROUP: pane.options.get(OPTION_GROUP, ""),  # what tmux resolves (pane, window or session)
        }
        return FIELD_SEP.join(values[name] for name in PANE_FIELDS) + RECORD_SEP

    def __call__(self, argv: list[str], timeout: float) -> Completed:
        self.calls.append(argv)
        rest = argv[3:] if argv[1] == "-S" else argv[1:]
        command, rest = rest[0], rest[1:]
        if command in self.fail:
            return Completed(1, "", f"{command} failed")
        if command == "list-panes" and "-t" in rest:
            target = rest[rest.index("-t") + 1]  # "@3" (window id) or "=session:window"
            lines = [
                f"{p.pane_id} {p.options.get(OPTION_ROLE, '')}".rstrip() for p in self.panes if self._in(p, target)
            ]
            return Completed(0, "\n".join(lines) + "\n", "") if lines else Completed(1, "", "can't find window")
        if command == "list-panes":
            return Completed(0, "\n".join(self.record(p) for p in self.panes) + "\n", "")
        if command == "display-message" and rest[rest.index("-t") + 1].startswith("@"):
            return Completed(0, "120\n", "")  # window_width
        if command == "display-message":
            pane = self.pane(rest[rest.index("-t") + 1])
            values = [
                str(self.pid),
                self.socket,
                pane.options.get(OPTION_ID, ""),
                pane.pane_id,
                pane.options.get(OPTION_GROUP, ""),
            ]
            return Completed(0, FIELD_SEP.join(values) + "\n", "")
        if command == "set-option":
            index = rest.index("-t")
            if not rest[index + 1].startswith("%"):  # "=session:": options of a resumed session
                return Completed(0, "", "")
            pane = self.pane(rest[index + 1])
            positional = rest[index + 2 :]
            if "-u" in rest:
                pane.options.pop(positional[0], None)
            else:
                pane.options[positional[0]] = positional[1]
            return Completed(0, "", "")
        if command == "show-options":
            name = rest[-1]
            return Completed(0, self.tmux_options.get(name, "C-a" if name == "prefix" else "") + "\n", "")
        if command == "split-window":
            new = FakePane(f"%{len(self.panes) + 50}", self.panes[0].session, window=self.panes[0].window)
            self.panes.append(new)
            return Completed(0, new.pane_id + "\n", "")
        return Completed(0, "", "")

    def commands(self) -> list[str]:
        return [(argv[3] if argv[1] == "-S" else argv[1]) for argv in self.calls]


def pane(
    pane_id: str,
    session: str,
    *,
    card_id: str = "",
    role: str = "",
    activity: float = NOW - 30,
    active: bool = True,
    attached: bool = False,
    socket: str = SOCKET,
    server_pid: int = SERVER_PID,
    group: str = "",
) -> Pane:
    return Pane(
        session=session,
        window="0",
        pane_id=pane_id,
        cwd="/home/me/code/api",
        command="zsh",
        activity=int(activity),
        session_attached=attached,
        window_active=active,
        pane_active=active,
        socket=socket,
        server_pid=server_pid,
        card_id=card_id,
        role=role,
        group=group,
    )


def record(
    card_id: str,
    state: State = State.WORKING,
    *,
    pane_id: str | None = "%1",
    socket: str = SOCKET,
    server_pid: int = SERVER_PID,
    updated_at: float = NOW - 60,
    **kwargs: object,
) -> CardRecord:
    ref = TmuxRef(socket, server_pid, pane_id) if pane_id else None
    return CardRecord(card_id, state, tmux=ref, updated_at=updated_at, **kwargs)  # type: ignore[arg-type]


# A tmux server that behaves like real tmux on the paths sebside and its scripts use ------------------------------
#
# `TmuxWorld` keeps sessions, windows, panes, options, the global environment, hooks and key bindings in memory and
# answers the same argv a real tmux client would get. It fires `session-created`, `after-new-window` and
# `session-closed` hooks, parses hook strings like tmux does (double quotes, backslash escapes, `$VAR`), expands
# formats (`##`, `#{name}`, `#{q:name}`, `#{s/re/to/:name}`) and runs `run-shell` commands through a small sh word
# splitter that refuses anything a real shell would interpret (operators, unquoted `$`, globs). Programs run by
# `run-shell` are in-process callables registered in `programs`, so `sebside` itself runs as `cli.main`. Anything the
# fake does not know raises `FakeTmuxGap` instead of guessing, so a test never passes on a made-up answer.
#
# `run_script` runs a real bash script (sebside.tmux, toggle.sh) in one bash process whose `tmux` (and optional
# program shims like `sebside`) are bash functions that ask this world over two pipes: no executable is created, so
# a test stays in the millisecond range.


class FakeTmuxGap(AssertionError):
    """The fake was asked something it does not model: extend it rather than guess."""


Program = Callable[[list[str], dict[str, str]], Completed]


@dataclass
class WPane:
    pane_id: str
    argv: tuple[str, ...]
    cwd: str
    left: int
    width: int
    options: dict[str, str] = field(default_factory=dict)

    @property
    def command(self) -> str:
        return Path(self.argv[2].split()[0] if self.argv[:2] == ("sh", "-c") else self.argv[0]).name


@dataclass
class WWindow:
    window_id: str
    index: int
    width: int
    panes: list[WPane]
    active: str
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class WSession:
    name: str
    windows: list[WWindow]
    current: int = 0  # position in `windows`
    options: dict[str, str] = field(default_factory=dict)

    def window(self) -> WWindow:
        return self.windows[self.current]


QUOTE_FOR_SHELL = set("|&;<>()$`\\\"'*?[# =%")  # what tmux's #{q:...} escapes with a backslash
VALUE_FLAGS: dict[str, str] = {
    "new-session": "scxytnFe",
    "new-window": "tncFe",
    "split-window": "tlFce",
    "list-panes": "tFf",
    "display-message": "tFcd",
    "show-options": "t",
    "show-option": "t",
    "set-option": "t",
    "set-hook": "t",
    "show-hooks": "t",
    "bind-key": "TN",
    "list-keys": "TP",
    "unbind-key": "T",
    "kill-pane": "t",
    "kill-session": "t",
    "select-pane": "tT",
    "select-window": "t",
    "switch-client": "tc",
    "list-sessions": "Ff",
    "list-clients": "Ft",
    "set-environment": "t",
    "show-environment": "t",
    "run-shell": "tdc",
    "resize-pane": "txy",
}


def parse_args(command: str, args: list[str]) -> tuple[set[str], dict[str, str], list[str]]:
    """tmux getopt: single-letter flags, possibly combined (-gqv), value flags take the next word, `--` ends flags."""
    if command not in VALUE_FLAGS:
        raise FakeTmuxGap(f"fake tmux does not know {command!r}")
    takes = VALUE_FLAGS[command]
    letters: set[str] = set()
    values: dict[str, str] = {}
    index = 0
    while index < len(args) and args[index].startswith("-") and len(args[index]) > 1:
        if args[index] == "--":
            index += 1
            break
        word = args[index][1:]
        for position, letter in enumerate(word):
            if letter in takes:
                rest = word[position + 1 :]
                if rest:
                    values[letter] = rest
                else:
                    index += 1
                    values[letter] = args[index]
                break
            letters.add(letter)
        index += 1
    return letters, values, args[index:]


def tmux_words(text: str, environment: Mapping[str, str]) -> list[str]:
    """Split one tmux command string like tmux's parser: quotes, backslash escapes, `$NAME`/`${NAME}` expansion."""
    words: list[str] = []
    word: str | None = None
    index = 0

    def variable(at: int) -> tuple[str, int]:
        if text[at + 1 : at + 2] == "{":
            end = text.index("}", at)
            return environment.get(text[at + 2 : end], ""), end + 1
        match = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(text, at + 1)
        if not match:
            return "$", at + 1
        return environment.get(match.group(0), ""), match.end()

    while index < len(text):
        char = text[index]
        if char in " \t":
            if word is not None:
                words.append(word)
                word = None
            index += 1
        elif char == "'":
            end = text.index("'", index + 1)
            word, index = (word or "") + text[index + 1 : end], end + 1
        elif char == '"':
            word, index = word or "", index + 1
            while text[index] != '"':
                if text[index] == "\\":
                    if text[index + 1] not in '\\"$':
                        raise FakeTmuxGap(f"fake tmux parser: escape \\{text[index + 1]} in {text!r}")
                    word, index = word + text[index + 1], index + 2
                elif text[index] == "$":
                    value, index = variable(index)
                    word += value
                else:
                    word, index = word + text[index], index + 1
            index += 1
        elif char == "\\":
            word, index = (word or "") + text[index + 1], index + 2
        elif char == "$":
            value, index = variable(index)
            word = (word or "") + value
        elif char in ";#" and word is None:
            raise FakeTmuxGap(f"fake tmux parser: {char!r} (command separator or comment) in {text!r}")
        else:
            word, index = (word or "") + char, index + 1
    if word is not None:
        words.append(word)
    return words


def sh_words(command: str) -> list[str]:
    """Split a `run-shell` command like sh would split a simple command. Anything sh would interpret beyond quoting
    (operators, unquoted `$` or backquote, globs, `~`) raises, so a quoting bug cannot pass silently."""
    words: list[str] = []
    word: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char in " \t\n":
            if word is not None:
                words.append(word)
                word = None
            index += 1
        elif char == "'":
            end = command.index("'", index + 1)
            word, index = (word or "") + command[index + 1 : end], end + 1
        elif char == "\\":
            word, index = (word or "") + command[index + 1], index + 2
        elif char == '"':
            word, index = word or "", index + 1
            while command[index] != '"':
                if command[index] in "$`":
                    raise FakeTmuxGap(f"fake sh: expansion inside double quotes in {command!r}")
                if command[index] == "\\" and command[index + 1] in '$`"\\':
                    index += 1
                word, index = word + command[index], index + 1
            index += 1
        elif char == "#" and word is None:
            break  # a comment: the rest of the line is ignored, as sh does
        elif char in "$`;&|<>()*?[" or (char == "~" and word is None):
            raise FakeTmuxGap(f"fake sh: {char!r} would be interpreted by the shell in {command!r}")
        else:
            word, index = (word or "") + char, index + 1
    if word is not None:
        words.append(word)
    return words


def quote_for_shell(value: str) -> str:
    return "".join("\\" + char if char in QUOTE_FOR_SHELL else char for char in value)


FORMAT = re.compile(r"##|#\{([^}]*)\}|#(.?)")


@dataclass
class TmuxWorld:
    socket: str
    environment: dict[str, str] = field(default_factory=dict)
    pid: int = SERVER_PID
    version: str = "tmux 3.5a"
    sessions: dict[str, WSession] = field(default_factory=dict)
    options: dict[str, str] = field(default_factory=lambda: {"prefix": "C-b"})
    hooks: dict[str, dict[int, str]] = field(default_factory=dict)
    keys: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    clients: list[str] = field(default_factory=list)  # session name of each attached client
    last_sessions: list[str] = field(default_factory=list)  # each client's previous session (switch-client -l)
    messages: list[str] = field(default_factory=list)  # display-message without -p
    programs: dict[str, Program] = field(default_factory=dict)  # run-shell targets, by name or absolute path
    calls: list[list[str]] = field(default_factory=list)
    next_pane: int = 0
    next_window: int = 0

    # client side ---------------------------------------------------------------------------------------------
    def __call__(self, argv: list[str], timeout: float) -> Completed:
        """The `Runner` sebside's `Tmux` uses: `[binary, -S, socket, command, ...]` or `[binary, command, ...]`."""
        if argv[1:2] == ["-S"]:
            return self.client(argv[2], argv[3:])
        return self.client(None, argv[1:])

    def client(self, socket: str | None, args: list[str]) -> Completed:
        if args == ["-V"]:
            return Completed(0, self.version + "\n", "")
        if socket != self.socket:
            return Completed(1, "", f"no server running on {socket or '/tmp/tmux-501/default'}\n")
        self.calls.append(args)
        return self.execute(args, {})

    def tmux(self, *args: str) -> str:
        """The test harness typing a tmux command (like `subprocess.run(["tmux", "-S", ...], check=True)`)."""
        done = self.client(self.socket, list(args))
        if not done.ok:
            raise AssertionError(f"tmux {' '.join(args)} failed: {done.stderr}")
        return done.stdout.strip()

    # lookups -------------------------------------------------------------------------------------------------
    def panes(self) -> list[tuple[WSession, WWindow, WPane]]:
        return [
            (s, w, p) for s in sorted(self.sessions.values(), key=lambda s: s.name) for w in s.windows for p in w.panes
        ]

    def resolve(self, target: str | None) -> tuple[WSession, WWindow, WPane] | None:
        if target is None:
            raise FakeTmuxGap("fake tmux: a command without -t (no current client to resolve it)")
        for session, window, pane in self.panes():
            if pane.pane_id == target:
                return session, window, pane
        for session in self.sessions.values():
            for window in session.windows:
                if window.window_id == target:
                    return session, window, self._active(window)
        name, _, index = target.removeprefix("=").partition(":")
        session = self.sessions.get(name)
        if session is None:
            return None
        if not index:
            window = session.window()
            return session, window, self._active(window)
        window = next((w for w in session.windows if str(w.index) == index), None)
        return (session, window, self._active(window)) if window else None

    @staticmethod
    def _active(window: WWindow) -> WPane:
        return next(p for p in window.panes if p.pane_id == window.active)

    def variables(self, place: tuple[WSession, WWindow, WPane] | None) -> dict[str, str]:
        values = {"socket_path": self.socket, "pid": str(self.pid)}
        if place:
            session, window, pane = place
            values |= {
                "session_name": session.name,
                "session_attached": str(self.clients.count(session.name)),
                "window_index": str(window.index),
                "window_id": window.window_id,
                "window_width": str(window.width),
                "window_activity": str(int(NOW)),
                "window_active": "1" if session.window() is window else "0",
                "pane_id": pane.pane_id,
                "pane_active": "1" if window.active == pane.pane_id else "0",
                "pane_current_path": pane.cwd,
                "pane_current_command": pane.command,
                "pane_left": str(pane.left),
                "pane_width": str(pane.width),
            }
        return values

    def expand(self, fmt: str, place: tuple[WSession, WWindow, WPane] | None) -> str:
        values = self.variables(place)

        def lookup(name: str) -> str:
            if name.startswith("@"):  # user options: pane, then window, then session, then global (as tmux does)
                scopes = [place[2].options, place[1].options, place[0].options] if place else []
                return next((scope[name] for scope in [*scopes, self.options] if name in scope), "")
            if name not in values:
                raise FakeTmuxGap(f"fake tmux: format variable {name!r} not modelled here")
            return values[name]

        def one(match: re.Match[str]) -> str:
            if match.group(0) == "##":
                return "#"
            if match.group(1) is None:
                raise FakeTmuxGap(f"fake tmux: a lone '#' in format {fmt!r} (tmux would read it as a format)")
            body = match.group(1)
            if body.startswith("q:"):
                return quote_for_shell(lookup(body[2:]))
            if body.startswith("s/"):
                _, pattern, replacement, name = body.split("/", 3)
                return re.sub(pattern, replacement, lookup(name.removeprefix(":")))
            return lookup(body)

        return FORMAT.sub(one, fmt)

    # changes the harness makes (what a program in a pane or a person would do) -------------------------------
    def attach(self, session: str) -> None:
        """A client attaches (`tmux attach -t =session`)."""
        self.clients.append(session)
        self.last_sessions.append("")

    def program_exits(self, pane_id: str) -> None:
        """The program in a pane ends (remain-on-exit off): tmux closes the pane, then an empty window and session."""
        session, window, pane = self._place(pane_id)
        window.panes.remove(pane)
        if window.panes:
            window.active = window.active if window.active != pane_id else window.panes[0].pane_id
            return
        session.windows.remove(window)
        session.current = 0
        if not session.windows:
            self._close_session(session)

    def _place(self, pane_id: str) -> tuple[WSession, WWindow, WPane]:
        place = self.resolve(pane_id)
        if place is None or place[2].pane_id != pane_id:
            raise AssertionError(f"no pane {pane_id}")
        return place

    def _close_session(self, session: WSession) -> None:
        del self.sessions[session.name]
        kept = [
            (name, last) for name, last in zip(self.clients, self.last_sessions, strict=True) if name != session.name
        ]
        self.clients, self.last_sessions = [name for name, _ in kept], [last for _, last in kept]  # detach-on-destroy
        self.fire("session-closed", None)

    def _new_pane(self, command: list[str], cwd: str, width: int, left: int = 0) -> WPane:
        argv = tuple(command) if len(command) > 1 else ("sh", "-c", command[0]) if command else ("sh",)
        pane = WPane(f"%{self.next_pane}", argv, cwd, left, width)
        self.next_pane += 1
        return pane

    def _new_window(self, index: int, width: int, command: list[str], cwd: str) -> WWindow:
        pane = self._new_pane(command, cwd, width)
        window = WWindow(f"@{self.next_window}", index, width, [pane], pane.pane_id)
        self.next_window += 1
        return window

    # hooks ---------------------------------------------------------------------------------------------------
    def fire(self, hook: str, place: tuple[WSession, WWindow, WPane] | None) -> None:
        for _, text in sorted(self.hooks.get(hook, {}).items()):
            self.execute(tmux_words(text, self.environment), {"place": place})

    def run_shell(self, command: str, place: tuple[WSession, WWindow, WPane] | None) -> Completed:
        argv = sh_words(self.expand(command, place))
        program = self.programs.get(argv[0])
        if program is None:
            raise FakeTmuxGap(f"fake tmux: run-shell would start {argv[0]!r}, which no test registered")
        return program(argv, dict(self.environment))

    # commands ------------------------------------------------------------------------------------------------
    def execute(self, args: list[str], context: dict[str, object]) -> Completed:
        command = {"show-option": "show-options", "set": "set-option"}.get(args[0], args[0])
        flags, values, rest = parse_args(args[0], args[1:])
        target = values.get("t")
        ok = Completed(0, "", "")
        if command == "new-session":
            name = values["s"]
            if name in self.sessions:
                return Completed(1, "", f"duplicate session: {name}\n")
            window = self._new_window(
                0, int(values.get("x", "80")), rest, values.get("c", self.environment.get("HOME", "/"))
            )
            self.sessions[name] = session = WSession(name, [window])
            self.fire("session-created", (session, window, self._active(window)))
            return ok
        if command == "new-window":
            place = self.resolve(target)
            if place is None:
                return Completed(1, "", f"can't find session: {target}\n")
            session = place[0]
            window = self._new_window(max(w.index for w in session.windows) + 1, place[1].width, rest, place[2].cwd)
            session.windows.append(window)
            if "d" not in flags:
                session.current = len(session.windows) - 1
            self.fire("after-new-window", (session, window, self._active(window)))
            return ok
        if command == "kill-session":
            place = self.resolve(target)
            if place is None:
                return Completed(1, "", f"can't find session: {target}\n")
            self._close_session(place[0])
            return ok
        if command == "split-window":
            place = self.resolve(target)
            if place is None:
                return Completed(1, "", f"can't find pane: {target}\n")
            session, window, at = place
            size = values.get("l", "")
            if "h" in flags and "f" in flags and "b" in flags:
                width = window.width * int(size[:-1]) // 100 if size.endswith("%") else int(size)
                for pane in window.panes:
                    pane.left, pane.width = pane.left + width + 1, max(1, pane.width - width - 1)
                new = self._new_pane(rest, at.cwd, width)
                window.panes.insert(0, new)
            elif not flags & {"h", "f", "b"} and not size:
                new = self._new_pane(rest, at.cwd, at.width, at.left)
                window.panes.insert(window.panes.index(at) + 1, new)
            else:
                raise FakeTmuxGap(f"fake tmux: split-window {args[1:]} geometry not modelled")
            if "d" not in flags:
                window.active = new.pane_id
            out = self.expand(values["F"], (session, window, new)) + "\n" if "P" in flags else ""
            return Completed(0, out, "")
        if command == "kill-pane":
            self.program_exits(self._place(target or "")[2].pane_id)
            return ok
        if command == "list-panes":
            if "a" in flags:
                places = self.panes()
            else:
                place = self.resolve(target)
                if place is None:
                    return Completed(1, "", f"can't find window: {target}\n")
                places = [(place[0], place[1], pane) for pane in place[1].panes]
            return Completed(0, "".join(self.expand(values["F"], p) + "\n" for p in places), "")
        if command == "display-message":
            place = self.resolve(target) if target else context.get("place")  # type: ignore[assignment]
            if target and place is None:
                return Completed(1, "", f"can't find pane: {target}\n")
            text = self.expand(rest[0] if rest else "", place)  # type: ignore[arg-type]
            if "p" in flags:
                return Completed(0, text + "\n", "")
            self.messages.append(text)
            return ok
        if command == "show-options":
            name = rest[0]
            place = self.resolve(target) if target else None
            if target and place is None:
                return Completed(1, "", f"can't find {target}\n")
            if "g" in flags:
                value = self.options.get(name)
            elif place is None:
                raise FakeTmuxGap(f"fake tmux: show-options {args[1:]} without -g or -t")
            else:
                value = (place[2] if "p" in flags else place[1] if "w" in flags else place[0]).options.get(name)
            if value is None:
                return ok if "q" in flags else Completed(1, "", f"invalid option: {name}\n")
            return Completed(0, (value if "v" in flags else f"{name} {value}") + "\n", "")
        if command == "set-option":
            store = self.options
            if "g" not in flags:  # -p pane, -w window, else the session of the target (tmux's default scope)
                place = self.resolve(target)
                if place is None:
                    return Completed(1, "", f"can't find {target}\n")
                store = (place[2] if "p" in flags else place[1] if "w" in flags else place[0]).options
            if "u" in flags:
                store.pop(rest[0], None)
            else:
                store[rest[0]] = rest[1]
            return ok
        if command == "set-environment":
            self.environment[rest[0]] = rest[1]
            return ok
        if command == "show-environment":
            if rest[0] not in self.environment:
                return Completed(1, "", f"unknown variable: {rest[0]}\n")
            return Completed(0, f"{rest[0]}={self.environment[rest[0]]}\n", "")
        if command == "set-hook":
            name, _, index = rest[0].partition("[")
            slot = int(index.rstrip("]") or 0)
            if "u" in flags:
                self.hooks.get(name, {}).pop(slot, None)
            else:
                tmux_words(rest[1], self.environment)  # tmux parses the command when the hook is set
                self.hooks.setdefault(name, {})[slot] = rest[1]
            return ok
        if command == "show-hooks":
            lines = [
                f"{name}[{slot}] {text}"
                for name, slots in sorted(self.hooks.items())
                for slot, text in sorted(slots.items())
            ]
            return Completed(0, "".join(line + "\n" for line in lines), "")
        if command == "bind-key":
            self.keys[(values.get("T", "prefix"), rest[0])] = rest[1:]
            return ok
        if command == "unbind-key":
            self.keys.pop((values.get("T", "prefix"), rest[0]), None)
            return ok
        if command == "list-keys":
            table = values.get("T", "prefix")
            bound = self.keys.get((table, rest[0]))
            if bound is None:
                return Completed(1, "", f"unknown key: {rest[0]}\n")
            words = " ".join(f'"{w}"' if " " in w else w for w in bound)
            return Completed(0, f"bind-key -T {table} {rest[0]} {words}\n", "")
        if command == "run-shell":
            return self.run_shell(rest[0], context.get("place"))  # type: ignore[arg-type]
        if command == "switch-client":
            if not self.clients:
                return Completed(1, "", "no current client\n")
            if "l" in flags:
                if self.last_sessions[0] not in self.sessions:
                    return Completed(1, "", "can't find last session\n")
                self.clients[0], self.last_sessions[0] = self.last_sessions[0], self.clients[0]
                return ok
            place = self.resolve(target)
            if place is None:
                return Completed(1, "", f"can't find session: {target}\n")
            if place[0].name != self.clients[0]:
                self.last_sessions[0], self.clients[0] = self.clients[0], place[0].name
            return ok
        if command == "resize-pane":
            self._place(target or "")[2].width = int(values["x"])
            return ok
        if command == "select-window":
            place = self.resolve(target)
            if place is None:
                return Completed(1, "", f"can't find window: {target}\n")
            place[0].current = place[0].windows.index(place[1])
            return ok
        if command == "select-pane":
            session, window, pane = self._place(target or "")
            window.active = pane.pane_id
            return ok
        if command == "list-sessions":
            return Completed(
                0, "".join(self.expand(values["F"], self.resolve(f"={n}:")) + "\n" for n in sorted(self.sessions)), ""
            )
        if command == "list-clients":
            return Completed(0, "".join(f"/dev/fake{i}: {name}\n" for i, name in enumerate(self.clients)), "")
        raise FakeTmuxGap(f"fake tmux: {command} not modelled")


def sebside_program(main: Callable[[list[str], dict[str, str]], int]) -> Program:
    """`sebside` as a program run by tmux or a script: `cli.main` in this process, stdout and stderr captured."""

    def run(argv: list[str], env: dict[str, str]) -> Completed:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv[1:], env)
        return Completed(code, out.getvalue(), err.getvalue())

    return run


BASH_BRIDGE = r"""
__fake() {
    printf '%s\037' "$@" >&REQUEST_FD
    printf '\0' >&REQUEST_FD
    local reply rc out
    IFS= read -r -d '' reply <&REPLY_FD
    rc="${reply%%$'\037'*}"; reply="${reply#*$'\037'}"
    out="${reply%%$'\037'*}"; reply="${reply#*$'\037'}"
    printf '%s' "$out"
    [ -z "$reply" ] || printf '%s' "$reply" >&2
    return "$rc"
}
tmux() { __fake tmux "$@"; }
"""


BASH = shutil.which("bash") or "/bin/bash"


def run_script(
    world: TmuxWorld, script: Path, *args: str, env: dict[str, str], shims: tuple[str, ...] = ()
) -> Completed:
    """Run a bash script with `tmux` (and each program in `shims`) answered by `world`. `tmux` talks to the server
    named by `$TMUX`, as it does inside tmux."""
    request_read, request_write = os.pipe()
    reply_read, reply_write = os.pipe()
    bridge = BASH_BRIDGE.replace("REQUEST_FD", str(request_write)).replace("REPLY_FD", str(reply_read))
    prelude = bridge + "".join(f'{name}() {{ __fake {name} "$@"; }}\n' for name in shims) + 'source "$0" "$@"\n'
    os.set_inheritable(request_write, True)
    os.set_inheritable(reply_read, True)
    process = subprocess.Popen(  # close_fds=False lets Python use posix_spawn (cheaper than fork); only these two
        [BASH, "-c", prelude, str(script), *args],  # fds are inheritable, every other one Python opens is not
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=False,
    )
    os.close(request_write)
    os.close(reply_read)
    socket = env.get("TMUX", "").split(",")[0] or None
    pending = b""
    try:
        while chunk := os.read(request_read, 65536):
            pending += chunk
            while b"\0" in pending:
                message, pending = pending.split(b"\0", 1)
                kind, *argv = message.decode().split("\x1f")[:-1]
                done = world.client(socket, argv) if kind == "tmux" else world.programs[kind]([kind, *argv], dict(env))
                if "\x1f" in done.stdout + done.stderr or "\0" in done.stdout + done.stderr:
                    raise FakeTmuxGap("fake bridge: an answer with \\x1f or NUL cannot be passed to bash")
                os.write(reply_write, f"{done.returncode}\x1f{done.stdout}\x1f{done.stderr}\0".encode())
    finally:
        os.close(request_read)
        os.close(reply_write)
        out, err = process.communicate()  # blocking wait: a timeout would make Popen.wait poll with time.sleep
    return Completed(process.returncode, out.decode(), err.decode())
