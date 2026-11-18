"""Test doubles: an in-memory tmux server that records every argv, and pane/record builders."""

from __future__ import annotations

from dataclasses import dataclass, field

from sebside.model import OPTION_ID, OPTION_ROLE, CardRecord, Pane, State, TmuxRef
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
            values = [str(self.pid), self.socket, pane.options.get(OPTION_ID, ""), pane.pane_id]
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
