"""The sidebar: a keyboard-and-mouse TUI drawn with raw ANSI escapes.

`App` holds the logic (poll, reconcile, selection, keys, actions) and is tested without a terminal. `run` owns the
terminal: alternate screen, hidden cursor, cbreak mode, a SIGWINCH flag, a select() loop and whole-frame writes that
are skipped when nothing changed.

Towards tmux the sidebar is read-only, except: switch-client / select-window / select-pane to show a card,
new-session to resume a closed card (argv, no shell, only allowlisted programs, after you confirm), and its own pane
option `@sebside_role`. It writes card files only to close vanished cards, to remember seen done cards and to remember
the group of a card's pane (so the card stays in that group once closed).

In a session whose `@sebside_group` is set, the sidebar shows only the cards of that group and the group's footer.
"""

from __future__ import annotations

import os
import re
import select
import signal
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .cards import Board, build, newly_seen, pane_groups, reconcile, server_of
from .errors import SebsideError
from .log import logger
from .model import OPTION_ROLE, OPTION_SHARED_WIDTH, SIDEBAR_ROLE, Card, Group, Meter, Pane, State
from .render import Row, clean, frame_layout, to_ansi

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from .config import Config
    from .store import Store
    from .tmux import TmuxLike

SPINNER_HZ = 8
KEY_ALIASES: dict[str, str] = {
    "\x1b[A": "up",
    "\x1bOA": "up",
    "k": "up",
    "\x1b[B": "down",
    "\x1bOB": "down",
    "j": "down",
    "\r": "enter",
    "\n": "enter",
    "y": "yes",
    "n": "esc",
    "\x1b": "esc",
    "?": "explain",
    "q": "quit",
    "\x03": "quit",
    "x": "forget",
    "\x1b[3~": "forget",
    "g": "top",
    "G": "bottom",
}
ESCAPE_SEQUENCE = re.compile(r"\x1b(\[<[0-9;]*[Mm]|\[[0-9;]*[A-Za-z~]|O[A-Za-z])")
MOUSE_REPORT = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"  # report presses and wheel, in SGR form (no 223-column limit)
MOUSE_OFF = "\x1b[?1000l\x1b[?1006l"
CLICK = "click:"
MODIFIER_BITS = 4 | 8 | 16
WHEEL = {64: "up", 65: "down"}


def decode_mouse(token: str) -> str | None:
    """An SGR mouse report -> "up"/"down" (wheel) or "click:<row>:<column>" (left press, 0-based); else None."""
    match = MOUSE_REPORT.fullmatch(token)
    if match is None or match.group(4) != "M":
        return None
    button = int(match.group(1)) & ~MODIFIER_BITS  # shift/meta/ctrl do not change which button it was
    if button == 0:
        return f"{CLICK}{int(match.group(3)) - 1}:{int(match.group(2)) - 1}"
    return WHEEL.get(button)


def decode_keys(data: str) -> list[str]:
    """Raw terminal input -> key names (unknown keys are dropped)."""
    keys: list[str] = []
    index = 0
    while index < len(data):
        match = ESCAPE_SEQUENCE.match(data, index)
        token = match.group(0) if match else data[index]
        index += len(token)
        key = decode_mouse(token) if token.startswith("\x1b[<") else KEY_ALIASES.get(token)
        if key:
            keys.append(key)
    return keys


def session_name(title: str, taken: set[str]) -> str:
    """A tmux-safe, unused session name from a card title (tmux forbids '.' and ':')."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")[:40] or "resumed"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def _socket_exists(path: str) -> bool:
    return Path(path).exists()


@dataclass
class App:
    config: Config
    tmux: TmuxLike
    store: Store
    clock: Callable[[], float] = time.time
    home: str = ""
    socket_exists: Callable[[str], bool] = _socket_exists
    board: Board = field(default_factory=Board)
    panes: list[Pane] = field(default_factory=list)
    selected_key: str | None = None
    mode: str = "list"  # list | explain | confirm
    message: str = ""
    prefix: str = "C-b"
    own_pane: str = ""  # this sidebar's pane id (from $TMUX_PANE); empty when unknown
    reload: bool = False  # the installed code changed: restart this process once the terminal is restored
    companion_seen: bool = False  # a non-sidebar pane has shared this window with us at least once
    row_keys: list[str | None] = field(default_factory=list)  # card key under each screen row, as last painted
    meter: Meter | None = None  # the usage meter under the counter, as last read
    footer: Group | None = None  # the footer of this sidebar's group, as last read

    # data --------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Poll tmux and the card files. Errors are shown on the message line; the sidebar keeps running."""
        try:
            self._refresh()
        except (SebsideError, OSError) as error:
            logger().exception("refresh failed")
            self.message = f"refresh failed: {clean(str(error))}"

    def _refresh(self) -> None:
        self.meter = self.store.load_meter()
        panes = self.tmux.list_panes()
        if panes is None:
            self.message = "tmux did not answer; showing the last known cards"
            return
        now = self.clock()
        self.panes = panes
        server = server_of(panes)
        records = self.store.load_all()
        gone = reconcile(records, panes, server, self.socket_exists, now)
        seen = newly_seen(records, panes, server)
        groups = pane_groups(records, panes, server)
        if gone or seen or groups:
            closed = self.store.close(gone, now, {card_id: records[card_id].rev for card_id in gone})
            self.store.mark_seen(seen)
            self.store.remember_pane_groups(groups)
            logger().debug("reconcile closed=%s seen=%s groups=%s", closed, sorted(seen), groups)
            records = self.store.load_all()
        self.board = build(panes, records, self.config, now, self.home, self.group)
        self.footer = self.store.load_group(self.group) if self.group else None
        keys = [card.key for card in self.board.cards]
        if self.selected_key not in keys:
            self.selected_key = keys[0] if keys else None
            if self.mode == "confirm":
                self.mode = "list"

    def alone(self) -> bool:
        """True when the sidebar must close itself: every pane left in its window is a sidebar, after a main pane was
        there once, and `@sebside-close-alone` is not `off`. Never true while a main pane is in the window. The
        sidebar only ends its own process: nothing is killed, so tmux closes the pane, window and session itself."""
        mine = next((pane for pane in self.panes if pane.pane_id == self.own_pane), None)
        if mine is None:
            return False
        window = [pane for pane in self.panes if (pane.session, pane.window) == (mine.session, mine.window)]
        if any(pane.role != SIDEBAR_ROLE for pane in window):
            self.companion_seen = True
            return False
        if not self.companion_seen or self.tmux.option("@sebside-close-alone") == "off":
            return False
        logger().debug("sidebar %s is alone in %s:%s, closing", self.own_pane, mine.session, mine.window)
        last_window = all(
            (pane.session, pane.window) == (mine.session, mine.window)
            for pane in self.panes
            if pane.session == mine.session
        )
        if last_window and self.tmux.resumed(mine.session):
            self.tmux.back()  # the session ends with this pane: return the viewer to where it came from
        return True

    @property
    def selected(self) -> Card | None:
        return next((card for card in self.board.cards if card.key == self.selected_key), None)

    # keys --------------------------------------------------------------------------------------------
    def handle_key(self, key: str) -> bool:
        """Returns False to quit."""
        if key == "quit":
            return False
        if key.startswith(CLICK):
            row, _, column = key.removeprefix(CLICK).partition(":")
            self._click(int(row), int(column) if column else None)
            return True
        self.message = ""
        if self.mode == "confirm":
            if key in ("enter", "yes"):
                self.run_resume()
            self.mode = "list"
        elif key in ("esc", "explain") and self.mode == "explain":
            self.mode = "list"
        elif key == "explain":
            self.mode = "explain"
        elif key in ("up", "down", "top", "bottom"):
            self._move(key)
        elif key == "forget":
            self.forget_selected()
        elif key == "enter":
            self.activate()
        return True

    def _click(self, row: int, column: int | None = None) -> None:
        """A click cancels the resume confirmation. In the list it selects the card under the row and activates it: a
        live card is shown, a closed one is resumed at once (Enter on a closed card asks first)."""
        if self.mode == "confirm":
            self.mode, self.message = "list", ""
            return
        if self.mode != "list" or not 0 <= row < len(self.row_keys) or self.row_keys[row] is None:
            return
        self.message = ""
        self.selected_key = self.row_keys[row]
        self.activate(confirm=False)

    def forget_selected(self) -> None:
        """Drop the selected closed card from the history (its own card file only); the selection stays nearby."""
        card = self.selected
        if card is None or card.live:
            return
        keys = [other.key for other in self.board.cards]
        index = keys.index(card.key)
        done, reason = self.store.forget(card.card_id)
        if not done:
            self.message = f"cannot forget: {clean(reason)}"
            return
        self.refresh()
        keys = [other.key for other in self.board.cards]
        if keys:
            self.selected_key = keys[min(index, len(keys) - 1)]

    def _move(self, key: str) -> None:
        keys = [card.key for card in self.board.cards]
        if not keys:
            return
        index = keys.index(self.selected_key) if self.selected_key in keys else 0
        index = {"up": index - 1, "down": index + 1, "top": 0, "bottom": len(keys) - 1}[key]
        self.selected_key = keys[max(0, min(len(keys) - 1, index))]

    # actions -----------------------------------------------------------------------------------------
    def activate(self, *, confirm: bool = True) -> None:
        """Show a live card; resume a closed one, after the confirmation screen unless `confirm` is False."""
        card = self.selected
        if card is None:
            return
        if card.live:
            self.show(card)
            return
        twin = next((live for live in self.board.live if live.card_id == card.card_id), None)
        if twin is not None:
            self.show(twin)
        elif not card.resume:
            self.message = "this card has no resume command"
        elif confirm:
            self.mode = "confirm"
        else:
            self.run_resume()

    def show(self, card: Card) -> None:
        if not card.in_tmux or not card.session:
            self.message = "this card was set outside tmux: nothing to switch to"
            return
        done = self.tmux.show(
            card.session, card.window, card.pane_id, focus_main=self.tmux.option("@sebside-focus") == "main"
        )
        if not done.ok:
            self.message = f"cannot switch: {done.error}"
            return
        if card.card_id and card.state is State.DONE:
            self.store.mark_seen({card.card_id: card.rev})

    def resume_allowed(self) -> bool:
        card = self.selected
        return card is not None and self.config.may_resume(card.resume)

    def run_resume(self) -> None:
        """Resume the selected closed card: argv from its own file only, allowlisted program, no shell."""
        card = self.selected
        if card is None or card.live:
            return
        if any(live.card_id == card.card_id for live in self.board.live):
            self.message = "already running"
            return
        # Read once, from the descriptor whose owner and mode were checked, and run exactly what was confirmed.
        record, reason = self.store.load_trusted(card.card_id)
        if record is None:
            self.message = f"resume refused: {clean(reason)}"
            return
        if not record.resume:
            self.message = "this card has no resume command"
            return
        if record.resume != card.resume:
            self.message = "resume refused: the command changed since you confirmed it; press Enter again"
            return
        if not self.config.may_resume(record.resume):
            self.message = f"resume refused: add {clean(record.resume[0])!r} to resume_programs in the config"
            return
        name = session_name(record.title or record.id, {pane.session for pane in self.panes})
        usable = record.cwd and "#" not in record.cwd and Path(record.cwd).is_dir()
        cwd = record.cwd if usable else None
        done = self.tmux.new_session(name, cwd, record.resume)
        if not done.ok:
            self.message = f"resume failed: {done.error}"
            return
        self.tmux.show(name, "", "")
        self.message = f"resumed in session {name}"

    @property
    def group(self) -> str:
        """The group of the session this sidebar sits in ("" when it has none, or while the pane is unknown)."""
        mine = next((pane for pane in self.panes if pane.pane_id == self.own_pane), None)
        return mine.group if mine else ""

    @property
    def current_session(self) -> str | None:
        """The tmux session this sidebar's own pane lives in (None while the pane is unknown)."""
        mine = next((pane for pane in self.panes if pane.pane_id == self.own_pane), None)
        return mine.session if mine else None

    # drawing -----------------------------------------------------------------------------------------
    def rows(self, width: int, height: int, frame: int = 0) -> list[Row]:
        rows, self.row_keys = frame_layout(
            self.board,
            self.selected_key,
            width,
            height,
            self.config,
            self.clock(),
            frame=frame,
            mode=self.mode,
            message=self.message,
            prefix=self.prefix,
            resume_allowed=self.resume_allowed(),
            current_session=self.current_session,
            meter=self.meter,
            group=self.footer,
        )
        return rows


# terminal ----------------------------------------------------------------------------------------
ESCAPE_WAIT_S = 0.05
PARTIAL_ESCAPE = re.compile(r"\x1b(\[<?[0-9;]*|O)?$")


RELOAD_CHECK_S = 3.0
PACKAGE_DIR = Path(__file__).resolve().parent


def code_fingerprint(root: Path = PACKAGE_DIR) -> tuple[tuple[str, int, int], ...] | None:
    """Name, mtime and size of every module of the installed package; None while the files cannot be read (for
    instance in the middle of an upgrade)."""
    try:
        return tuple(sorted((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in root.glob("*.py")))
    except OSError:
        return None


@dataclass
class CodeWatch:
    """Notices that sebside was upgraded under a running sidebar. A change counts once it is seen twice in a row,
    so an upgrade that is still copying files is not mistaken for a finished one."""

    fingerprint: Callable[[], tuple[tuple[str, int, int], ...] | None] = code_fingerprint
    interval: float = RELOAD_CHECK_S
    known: tuple[tuple[str, int, int], ...] | None = None
    candidate: tuple[tuple[str, int, int], ...] | None = None
    checked_at: float = float("-inf")

    def __post_init__(self) -> None:
        self.known = self.fingerprint()

    def changed(self, now: float) -> bool:
        if now - self.checked_at < self.interval:
            return False
        self.checked_at = now
        current = self.fingerprint()
        if current is None or current == self.known:
            self.candidate = None
            return False
        if current != self.candidate:
            self.candidate = current
            return False
        return True


REEXEC_TRIES = 20
REEXEC_GAP_S = 0.5


def reexec(execv: Callable[[str, list[str]], None] = os.execv, sleep: Callable[[float], None] = time.sleep) -> None:
    """Replace this process by a fresh sebside with the same arguments and environment (same pane, new code).
    `uv tool install --reinstall` rebuilds the tool's environment, so its python can be missing for a moment: try
    again for up to ~10 s before giving up (a sidebar vanished that way on 2026-11-18)."""
    for attempt in range(REEXEC_TRIES):
        try:
            execv(sys.executable, [sys.executable, "-m", "sebside", *sys.argv[1:]])
            return
        except OSError:
            if attempt == REEXEC_TRIES - 1:
                raise
            sleep(REEXEC_GAP_S)


def split_pending(data: str) -> tuple[str, str]:
    """Split off a trailing, possibly incomplete escape sequence so a key cut between two reads is not read as Esc."""
    match = PARTIAL_ESCAPE.search(data)
    if match is None:
        return data, ""
    return data[: match.start()], data[match.start() :]


def terminal_size(fd: int, fallback_width: int) -> tuple[int, int]:
    """Size of the terminal on `fd` itself (not $COLUMNS/$LINES, which may be stale)."""
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        return fallback_width, 24
    return max(size.columns, 1), max(size.lines, 1)


def _exit_on_signal(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)  # unwinds through `finally`, so the terminal is restored


def paint(rows: list[Row]) -> str:
    return "\x1b[H" + "\r\n".join(to_ansi(row) + "\x1b[K" for row in rows)


@dataclass
@dataclass
class WidthSync:
    """One width for every sidebar: a sidebar the user resized publishes its width in OPTION_SHARED_WIDTH, the others
    resize their own pane to it on their next poll. A change that follows our own resize request (also one tmux
    clamped in a narrow window) is ours, not the user's, so it is never published."""

    last: int | None = None
    requested: int | None = None
    tried_for: int | None = None
    window: int | None = None

    def tick(self, tmux: TmuxLike, pane: str, columns: int, window: int | None = None) -> None:
        if not pane:
            return
        raw = tmux.option(OPTION_SHARED_WIDTH)
        shared = int(raw) if raw.isdigit() else None
        # tmux rescales panes when the window itself changes size (new client, Terminal resized): not a drag.
        window_changed = window is not None and self.window is not None and window != self.window
        if window is not None:
            self.window = window
        if columns == self.last and self.requested is not None:
            self.requested = None  # the resize changed nothing (already at tmux's limit): the next change is the user's
        if self.last is None or columns != self.last:
            first, ours = self.last is None, self.requested is not None
            self.last, self.requested = columns, None
            if ours:
                return
            if window_changed and not first:
                self.tried_for = None  # follow the shared width again instead of publishing tmux's rescale
                if shared is not None and shared != columns:
                    self.requested = self.tried_for = shared
                    tmux.run("resize-pane", "-t", pane, "-x", str(shared))
                return
            if not first or shared is None:
                if shared != columns:
                    tmux.run("set-option", "-g", OPTION_SHARED_WIDTH, str(columns))
                self.tried_for = columns
                return
        if shared is not None and shared != columns and self.tried_for != shared:
            self.requested = self.tried_for = shared
            tmux.run("resize-pane", "-t", pane, "-x", str(shared))


def _window_width(tmux: TmuxLike, pane: str) -> int | None:
    """Width of the window holding our pane; None when tmux does not answer."""
    if not pane:
        return None
    done = tmux.display(pane, "#{window_width}")
    text = done.stdout.strip() if done.ok else ""
    return int(text) if text.isdigit() else None


class _Resized:
    flag: bool = True

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        self.flag = True


def run(app: App, own_pane: str | None = None) -> int:
    code = _run(app, own_pane)
    if app.reload:
        logger().debug("sebside was upgraded, restarting the sidebar")
        try:
            reexec()
        except OSError:
            logger().exception("restart after upgrade failed")
            print("sebside: upgraded, but the sidebar could not restart itself: reopen it", file=sys.stderr)
            return 1
    return code


def _run(app: App, own_pane: str | None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("sebside: the sidebar needs a terminal: run it inside a tmux pane", file=sys.stderr)
        return 2
    if own_pane:
        app.own_pane = own_pane
        app.tmux.set_pane_option(own_pane, OPTION_ROLE, SIDEBAR_ROLE)
        app.tmux.set_pane_option(own_pane, "remain-on-exit", "off")  # never leave a dead sidebar holding the window
    app.prefix = app.tmux.prefix()
    app.store.prune(app.clock())
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    resized = _Resized()
    previous_handler = signal.signal(signal.SIGWINCH, resized)
    out = sys.stdout
    last_poll = 0.0
    last_stamp: tuple[int, int] | None = None
    last_paint = ""
    pending = ""
    previous_term = signal.signal(signal.SIGTERM, _exit_on_signal)
    watch = CodeWatch()
    widths = WidthSync()
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[2J" + MOUSE_ON)
        out.flush()
        while True:
            stamp = app.store.dir_stamp()
            if time.monotonic() - last_poll >= app.config.refresh_seconds or stamp != last_stamp:
                app.refresh()
                last_poll, last_stamp = time.monotonic(), app.store.dir_stamp()
                if app.alone():
                    return 0
                widths.tick(
                    app.tmux,
                    app.own_pane,
                    terminal_size(fd, app.config.width)[0],
                    _window_width(app.tmux, app.own_pane),
                )
            if watch.changed(time.monotonic()):
                app.reload = True
                return 0
            if resized.flag:
                resized.flag = False
                last_paint = ""
                out.write("\x1b[2J" + MOUSE_ON)
            columns, lines = terminal_size(fd, app.config.width)
            painted = paint(app.rows(columns, lines, int(time.time() * SPINNER_HZ)))
            if painted != last_paint:
                out.write(MOUSE_ON + painted)  # cheap, and keeps mouse mode on whatever happened to the terminal
                out.flush()
                last_paint = painted
            try:
                ready, _, _ = select.select([fd], [], [], ESCAPE_WAIT_S if pending else 1 / SPINNER_HZ)
            except InterruptedError:
                continue
            if not ready:
                if pending:  # a lone ESC (or a cut sequence) that got no continuation in time
                    complete, pending = pending, ""
                else:
                    continue
            else:
                complete, pending = split_pending(pending + os.read(fd, 64).decode("utf-8", errors="ignore"))
            for key in decode_keys(complete):
                if not app.handle_key(key):
                    return 0
                if key in ("enter", "yes", "forget") or key.startswith(CLICK):
                    last_poll = 0.0  # show the effect at once
    except KeyboardInterrupt:
        return 0
    finally:
        out.write(MOUSE_OFF + "\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        signal.signal(signal.SIGWINCH, previous_handler)
        signal.signal(signal.SIGTERM, previous_term)
