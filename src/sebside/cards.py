"""Pure card logic: merge tmux panes with card files, reconcile closed cards, sort. No tmux, no files, no clock.

Rules:

- A card file is live when its tmux reference matches this server (socket and server pid) and its pane is in
  `list-panes` (and that pane does not carry a different `@sebside_id`). A reused `%N` after a server restart cannot
  claim an old card because the server pid differs.
- Open cards that are not live are closed by reconciliation on every poll. Nothing is keyed by session name.
- A card written outside tmux (no tmux reference) stays open and is shown without a pane.
- A session without a live card gets one default card from tmux basics: title = session name, subtitle = cwd,
  desc = current command, state = quiet after `quiet_minutes` without output, unknown otherwise.
- A card is stale (shown as unknown) when max(card updated_at, pane activity) is older than `stale_hours`.
- A done card the user has looked at (`seen_rev` >= rev) is shown as quiet.
- Closed cards: newest first, at most `history`, never one whose id is live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import STATE_ORDER, Card, CardRecord, Pane, Server, State

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from .config import Config

GRACE_S = 2.0  # a card set this recently is never closed (card set writes the pane option, then the file)


@dataclass(frozen=True, slots=True)
class Board:
    live: list[Card] = field(default_factory=list)
    history: list[Card] = field(default_factory=list)

    @property
    def cards(self) -> list[Card]:
        return self.live + self.history

    def counts(self) -> dict[State, int]:
        return {state: sum(card.state is state for card in self.live) for state in State}


def tilde(path: str, home: str) -> str:
    home = home.rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home) :]
    return path


def server_of(panes: Iterable[Pane]) -> Server | None:
    for pane in panes:
        return Server(pane.socket, pane.server_pid)
    return None


def live_pane(record: CardRecord, panes: Mapping[str, Pane], server: Server | None) -> Pane | None:
    ref = record.tmux
    if ref is None or server is None or ref.socket != server.socket or ref.server_pid != server.pid:
        return None
    pane = panes.get(ref.pane)
    if pane is None or (pane.card_id and pane.card_id != record.id):
        return None
    return pane


def reconcile(
    records: Mapping[str, CardRecord],
    panes: Iterable[Pane],
    server: Server | None,
    socket_exists: Callable[[str], bool],
    now: float,
) -> list[str]:
    """Ids of open cards that are no longer alive and should get `closed_at`."""
    by_id = {pane.pane_id: pane for pane in panes}
    gone: list[str] = []
    for record in records.values():
        ref = record.tmux
        if record.closed or record.readonly or ref is None or now - record.updated_at < GRACE_S:
            continue
        if server is not None and ref.socket == server.socket:
            if live_pane(record, by_id, server) is None:
                gone.append(record.id)
        elif not socket_exists(ref.socket):
            gone.append(record.id)
    return gone


def newly_seen(records: Mapping[str, CardRecord], panes: Iterable[Pane], server: Server | None) -> dict[str, int]:
    """Done cards whose pane is on screen right now count as seen."""
    by_id = {pane.pane_id: pane for pane in panes}
    out: dict[str, int] = {}
    for record in records.values():
        if record.closed or record.state is not State.DONE or record.seen_rev >= record.rev:
            continue
        pane = live_pane(record, by_id, server)
        if pane is not None and pane.visible:
            out[record.id] = record.rev
    return out


def _display_state(record: CardRecord, last: float, config: Config, now: float) -> tuple[State, bool, bool, str]:
    """state, stale, seen, explanation"""
    if now - last > config.stale_hours * 3600:
        return State.UNKNOWN, True, False, f"card file, stale: no update or output for over {config.stale_hours:g} h"
    if record.state is State.DONE and record.seen_rev >= record.rev > 0:
        return State.QUIET, False, True, "card file: done, and you have seen it"
    return record.state, False, False, "card file (sebside card set)"


def card_from_record(record: CardRecord, pane: Pane | None, config: Config, now: float, home: str) -> Card:
    sources: dict[str, str] = {}

    def pick(name: str, value: str, fallback: str, fallback_source: str) -> str:
        if value:
            sources[name] = "card file"
            return value
        sources[name] = fallback_source
        return fallback

    if pane is not None:
        title = pick("title", record.title, pane.session, "tmux: session name")
        subtitle = pick("subtitle", record.subtitle, tilde(pane.cwd, home), "tmux: pane directory")
        desc = pick("desc", record.desc, pane.command, "tmux: current command")
        last = max(record.updated_at, float(pane.activity))
    else:
        title = pick("title", record.title, record.id, "card id")
        subtitle = pick("subtitle", record.subtitle, tilde(record.cwd, home), "card file: cwd")
        desc = pick("desc", record.desc, "", "none")
        last = record.updated_at
    state, stale, seen, why = _display_state(record, last, config, now)
    sources["state"] = why if pane is not None else why + "; written outside tmux"
    sources["age"] = "card file: last card set"
    return Card(
        key=f"card:{record.id}",
        card_id=record.id,
        state=state,
        title=title,
        subtitle=subtitle,
        desc=desc,
        extra=record.extra,
        resume=record.resume,
        session=pane.session if pane else "",
        window=pane.window if pane else "",
        pane_id=pane.pane_id if pane else "",
        since=record.updated_at,
        rev=record.rev,
        in_tmux=pane is not None,
        stale=stale,
        seen=seen,
        sources=sources,
    )


def default_card(pane: Pane, config: Config, now: float, home: str) -> Card:
    quiet = bool(pane.activity) and now - pane.activity > config.quiet_minutes * 60
    reason = (
        f"tmux: no output for over {config.quiet_minutes:g} min"
        if quiet
        else "tmux basics only: no card set for this session"
    )
    return Card(
        key=f"pane:{pane.pane_id}",
        card_id="",
        state=State.QUIET if quiet else State.UNKNOWN,
        title=pane.session,
        subtitle=tilde(pane.cwd, home),
        desc=pane.command,
        session=pane.session,
        window=pane.window,
        pane_id=pane.pane_id,
        since=float(pane.activity) if pane.activity else None,
        sources={
            "title": "tmux: session name",
            "subtitle": "tmux: pane directory",
            "desc": "tmux: current command",
            "state": reason,
            "age": "tmux: window activity",
        },
    )


def history_card(record: CardRecord, home: str) -> Card:
    return Card(
        key=f"closed:{record.id}",
        card_id=record.id,
        state=record.state,
        title=record.title or record.id,
        subtitle=record.subtitle or tilde(record.cwd, home),
        desc=record.desc,
        extra=record.extra,
        resume=record.resume,
        since=record.closed_at,
        rev=record.rev,
        live=False,
        sources={
            "state": "card file: last state before the card closed",
            "age": "card file: time the card closed",
            "resume": "card file" if record.resume else "none: this card cannot be resumed",
        },
    )


def representative(panes: list[Pane]) -> Pane | None:
    """The pane a default card points at: the active pane of the active window, never a sidebar."""
    candidates = [pane for pane in panes if not pane.is_sidebar]
    for pane in candidates:
        if pane.window_active and pane.pane_active:
            return pane
    for pane in candidates:
        if pane.window_active:
            return pane
    return candidates[0] if candidates else None


def sort_key(card: Card, sort: str) -> tuple[object, ...]:
    if sort == "name":
        return (card.title.lower(), card.session, card.pane_id)
    if sort == "recent":
        return (-(card.since or 0.0), card.title.lower())
    return (STATE_ORDER.index(card.state), card.title.lower(), card.session, card.pane_id)


def build(panes: list[Pane], records: Mapping[str, CardRecord], config: Config, now: float, home: str = "") -> Board:
    server = server_of(panes)
    by_id = {pane.pane_id: pane for pane in panes}
    live: list[Card] = []
    carded_sessions: set[str] = set()
    live_ids: set[str] = set()
    for record in records.values():
        if record.closed:
            continue
        pane = live_pane(record, by_id, server)
        if pane is None and record.tmux is not None:
            continue  # not alive here: reconciliation closes it, or it belongs to another running server
        if pane is not None and (pane.is_sidebar or not config.filter.allows(pane.session)):
            continue
        live_ids.add(record.id)
        if pane is not None:
            carded_sessions.add(pane.session)
        live.append(card_from_record(record, pane, config, now, home))
    by_session: dict[str, list[Pane]] = {}
    for pane in panes:
        if config.filter.allows(pane.session) and pane.session not in carded_sessions:
            by_session.setdefault(pane.session, []).append(pane)
    for session_panes in by_session.values():
        chosen = representative(session_panes)
        if chosen is not None:
            live.append(default_card(chosen, config, now, home))
    live.sort(key=lambda card: sort_key(card, config.sort))
    closed = sorted(
        (r for r in records.values() if r.closed and r.id not in live_ids),
        key=lambda r: r.closed_at or 0.0,
        reverse=True,
    )
    return Board(live, [history_card(record, home) for record in closed[: config.history]])
