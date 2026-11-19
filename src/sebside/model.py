"""Plain data, no IO: card states, card files, tmux panes and the cards the sidebar draws."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    WAITING = "waiting"  # needs you: a question, a permission prompt, a review
    ERROR = "error"  # something failed and needs you
    WORKING = "working"  # busy, nothing to do for you
    DONE = "done"  # finished; becomes quiet once you have looked at it
    QUIET = "quiet"  # idle
    UNKNOWN = "unknown"  # no card and recent activity, or a card that went stale


ATTENTION: frozenset[State] = frozenset({State.WAITING, State.ERROR})
STATE_ORDER: tuple[State, ...] = (State.WAITING, State.ERROR, State.WORKING, State.DONE, State.QUIET, State.UNKNOWN)
DIMMED: frozenset[State] = frozenset({State.QUIET, State.UNKNOWN})

SCHEMA = 1
# Runtime pane options (underscore). Config options use a dash: @sebside-key, @sebside-width.
OPTION_ID = "@sebside_id"
OPTION_STATE = "@sebside_state"
OPTION_ROLE = "@sebside_role"
OPTION_RESUMED = "@sebside_resumed"  # session option: sebside resumed this session from a closed card
SIDEBAR_ROLE = "sidebar"
OPTION_SHARED_WIDTH = "@sebside_shared_width"  # global: the width every sidebar follows; a drag of one sets it
# Set by you or an integration (usually on a session), never by sebside: a sidebar in a session with a group shows
# only the cards of that group. tmux resolves it pane -> window -> session -> global.
OPTION_GROUP = "@sebside_group"
GROUP_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")  # also the name of its file: groups/<id>.json


def valid_group(value: str) -> bool:
    """A group id: 1..100 characters of letters, digits, `.`, `_` and `-`, not starting with a dot."""
    return bool(GROUP_ID.match(value))


@dataclass(frozen=True, slots=True)
class Meter:
    """One usage meter under the counter row, e.g. a weekly limit: `label ████░░░ 47%` and a note below."""

    label: str
    percent: int  # 0..100
    note: str = ""
    at: float = 0.0  # when it was set (epoch seconds); an old meter is not drawn


@dataclass(frozen=True, slots=True)
class Group:
    """The footer of the sidebars of one group: a title (wrapped) and a note under it."""

    id: str
    title: str
    note: str = ""
    at: float = 0.0  # when it was set (epoch seconds)


def parse_state(value: str) -> State:
    try:
        return State(value.strip().lower())
    except ValueError:
        valid = ", ".join(state.value for state in State)
        raise ValueError(f"unknown state {value!r} (valid: {valid})") from None


@dataclass(frozen=True, slots=True)
class TmuxRef:
    """Where a card lives. A card is live only on the same server (socket and server pid) and pane."""

    socket: str
    server_pid: int
    pane: str


@dataclass(frozen=True, slots=True)
class CardRecord:
    """One card file: `$XDG_STATE_HOME/sebside/cards/<id>.json`, the single source of truth for card content."""

    id: str
    state: State = State.UNKNOWN
    title: str = ""
    subtitle: str = ""
    desc: str = ""
    extra: str = ""
    resume: tuple[str, ...] = ()  # argv, run without a shell
    cwd: str = ""
    tmux: TmuxRef | None = None  # None: written outside tmux
    rev: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float | None = None
    seen_rev: int = 0  # the done rev the user has looked at
    group: str = ""  # the group it belongs to; "" follows its pane's group (and none outside tmux)
    pane_group: str = ""  # its pane's group when last seen, kept so a closed card without a group stays in that one
    schema: int = SCHEMA
    readonly: bool = False  # written by a newer sebside: shown, never rewritten

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    @property
    def last_group(self) -> str:
        """Its own group, else the group its pane had when last seen (the group a closed card is shown in)."""
        return self.group or self.pane_group


@dataclass(frozen=True, slots=True)
class Server:
    socket: str
    pid: int


@dataclass(frozen=True, slots=True)
class Pane:
    """One row of `tmux list-panes -a`."""

    session: str
    window: str  # window index
    pane_id: str
    cwd: str
    command: str
    activity: int  # epoch seconds of the window's last output
    session_attached: bool
    window_active: bool
    pane_active: bool
    socket: str
    server_pid: int
    card_id: str = ""  # @sebside_id
    role: str = ""  # @sebside_role, "sidebar" for sebside's own panes
    group: str = ""  # @sebside_group as tmux resolves it for this pane ("" when unset or not a valid id)

    @property
    def is_sidebar(self) -> bool:
        return self.role == SIDEBAR_ROLE

    @property
    def visible(self) -> bool:
        """On screen in some attached client."""
        return self.session_attached and self.window_active


@dataclass(frozen=True, slots=True)
class Card:
    """What the sidebar draws for one card, live or closed."""

    key: str  # stable selection key
    card_id: str  # "" for a card built from tmux basics only
    state: State
    title: str
    subtitle: str
    desc: str
    extra: str = ""
    resume: tuple[str, ...] = ()
    session: str = ""
    window: str = ""
    pane_id: str = ""
    since: float | None = None  # epoch seconds shown as the card's age
    rev: int = 0
    live: bool = True
    in_tmux: bool = True  # False for an open card written outside tmux
    stale: bool = False
    seen: bool = False
    group: str = ""
    sources: dict[str, str] = field(default_factory=dict)  # field -> where the value came from

    @property
    def needs_you(self) -> bool:
        return self.live and self.state in ATTENTION
