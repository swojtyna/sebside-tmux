from fakes import NOW, SERVER_PID, SOCKET, pane, record
from sebside.cards import build, newly_seen, reconcile, representative, server_of, tilde
from sebside.config import Config, Filter
from sebside.model import Server, State

SERVER = Server(SOCKET, SERVER_PID)


def _always(path: str) -> bool:
    return True


def _never(path: str) -> bool:
    return False


def test_defaults_from_tmux_basics():
    board = build([pane("%1", "api")], {}, Config(), NOW, home="/home/me")
    card = board.live[0]
    assert (card.title, card.subtitle, card.desc) == ("api", "~/code/api", "zsh")
    assert card.state is State.UNKNOWN and card.card_id == ""
    assert card.sources["title"] == "tmux: session name"


def test_default_is_quiet_after_quiet_minutes():
    board = build([pane("%1", "api", activity=NOW - 11 * 60)], {}, Config(quiet_minutes=10), NOW)
    assert board.live[0].state is State.QUIET


def test_card_file_is_the_source_of_truth():
    records = {"x": record("x", State.WAITING, title="Deploy", desc="approve?", extra="3/4", rev=2)}
    card = build([pane("%1", "api", card_id="x")], records, Config(), NOW).live[0]
    assert (card.title, card.desc, card.extra, card.state, card.rev) == ("Deploy", "approve?", "3/4", State.WAITING, 2)
    assert card.subtitle == "/home/me/code/api" and card.sources["subtitle"] == "tmux: pane directory"
    assert card.needs_you and card.pane_id == "%1"


def test_card_needs_matching_server_pid_and_pane():
    panes = [pane("%1", "api")]
    restarted = {"x": record("x", server_pid=999)}
    assert build(panes, restarted, Config(), NOW).live[0].card_id == ""
    other_pane = {"x": record("x", pane_id="%2")}
    assert build(panes, other_pane, Config(), NOW).live[0].card_id == ""


def test_pane_carrying_another_id_does_not_show_old_card():
    board = build([pane("%1", "api", card_id="new")], {"old": record("old")}, Config(), NOW)
    assert [card.card_id for card in board.live] == [""]


def test_card_written_outside_tmux_is_shown_without_pane():
    board = build([], {"cron": record("cron", State.ERROR, pane_id=None, title="nightly")}, Config(), NOW)
    card = board.live[0]
    assert card.title == "nightly" and not card.in_tmux and card.state is State.ERROR


def test_sidebar_panes_never_become_cards():
    board = build([pane("%3", "work", role="sidebar"), pane("%4", "work")], {}, Config(), NOW)
    assert [card.pane_id for card in board.live] == ["%4"]
    assert representative([pane("%1", "s", role="sidebar")]) is None


def test_one_default_card_per_session_without_cards():
    panes = [pane("%1", "work", card_id="a"), pane("%2", "work"), pane("%3", "other")]
    board = build(panes, {"a": record("a")}, Config(), NOW)
    assert sorted((c.session, c.card_id) for c in board.live) == [("other", ""), ("work", "a")]


def test_representative_prefers_active_pane():
    panes = [pane("%1", "s", role="sidebar"), pane("%2", "s", active=False), pane("%3", "s")]
    chosen = representative(panes)
    assert chosen is not None and chosen.pane_id == "%3"


def test_stale_uses_newest_of_card_update_and_pane_activity():
    old = {"x": record("x", updated_at=NOW - 13 * 3600)}
    idle = build([pane("%1", "a", card_id="x", activity=NOW - 13 * 3600)], old, Config(stale_hours=12), NOW)
    assert idle.live[0].state is State.UNKNOWN and idle.live[0].stale
    busy = build([pane("%1", "a", card_id="x", activity=NOW - 5)], old, Config(stale_hours=12), NOW)
    assert busy.live[0].state is State.WORKING and not busy.live[0].stale


def test_done_becomes_quiet_once_seen():
    panes = [pane("%1", "api", card_id="x")]
    unseen = build(panes, {"x": record("x", State.DONE, rev=3, seen_rev=2)}, Config(), NOW).live[0]
    assert unseen.state is State.DONE
    seen = build(panes, {"x": record("x", State.DONE, rev=3, seen_rev=3)}, Config(), NOW).live[0]
    assert seen.state is State.QUIET and seen.seen


def test_attention_order_needs_you_first():
    states = [State.QUIET, State.WORKING, State.ERROR, State.WAITING, State.DONE]
    panes = [pane(f"%{i}", f"s{i}", card_id=f"c{i}") for i in range(len(states))] + [pane("%9", "plain")]
    records = {f"c{i}": record(f"c{i}", state, pane_id=f"%{i}") for i, state in enumerate(states)}
    order = [card.state for card in build(panes, records, Config(), NOW).live]
    assert order == [State.WAITING, State.ERROR, State.WORKING, State.DONE, State.QUIET, State.UNKNOWN]


def test_name_and_recent_sort():
    panes = [pane("%1", "b", activity=NOW - 100), pane("%2", "a", activity=NOW - 10)]
    assert [c.title for c in build(panes, {}, Config(sort="name"), NOW).live] == ["a", "b"]
    assert [c.title for c in build(panes, {}, Config(sort="recent"), NOW).live] == ["a", "b"]


def test_session_filter():
    panes = [pane("%1", "main"), pane("%2", "scratch-1"), pane("%3", "other")]
    config = Config(filter=Filter(include=("main", "scratch-*"), exclude=("scratch-*",)))
    assert [c.title for c in build(panes, {}, config, NOW).live] == ["main"]


def test_history_newest_first_limited_and_without_live_ids():
    records = {f"h{i}": record(f"h{i}", title=f"old {i}", closed_at=NOW - 100 + i, resume=("vim",)) for i in range(8)}
    records["live"] = record("live")
    board = build([pane("%1", "s", card_id="live")], records, Config(history=3), NOW)
    assert [c.card_id for c in board.history] == ["h7", "h6", "h5"]
    assert all(not c.live and c.resume == ("vim",) for c in board.history)


def test_counts_only_live_cards():
    records = {"h": record("h", State.WAITING, closed_at=NOW), "w": record("w", State.WAITING)}
    board = build([pane("%1", "s", card_id="w")], records, Config(), NOW)
    assert board.counts()[State.WAITING] == 1


def test_reconcile_closes_dead_cards_by_identity():
    records = {
        "alive": record("alive", pane_id="%1"),
        "pane-gone": record("pane-gone", pane_id="%9"),
        "restarted": record("restarted", pane_id="%1", server_pid=1),
        "replaced": record("replaced", pane_id="%2"),
        "fresh": record("fresh", pane_id="%8", updated_at=NOW - 1),
        "outside": record("outside", pane_id=None),
        "closed": record("closed", pane_id="%7", closed_at=NOW - 5),
        "other-live": record("other-live", socket="/tmp/other-live"),
        "other-dead": record("other-dead", socket="/tmp/other-dead"),
    }
    panes = [pane("%1", "s", card_id="alive"), pane("%2", "s", card_id="newer")]

    def exists(path: str) -> bool:
        return path == "/tmp/other-live"

    gone = reconcile(records, panes, server_of(panes), exists, NOW)
    assert sorted(gone) == ["other-dead", "pane-gone", "replaced", "restarted"]


def test_reconcile_without_server_closes_cards_of_vanished_sockets():
    records = {"a": record("a")}
    assert reconcile(records, [], None, _never, NOW) == ["a"]
    assert reconcile(records, [], None, _always, NOW) == []


def test_newly_seen_only_visible_done_cards():
    records = {"d": record("d", State.DONE, rev=2), "w": record("w", State.WORKING, pane_id="%2", rev=2)}
    visible = [pane("%1", "s", card_id="d", attached=True), pane("%2", "s", card_id="w", attached=True)]
    assert newly_seen(records, visible, SERVER) == {"d": 2}
    hidden = [pane("%1", "s", card_id="d", attached=False)]
    assert newly_seen(records, hidden, SERVER) == {}


def test_tilde():
    assert tilde("/home/me/x", "/home/me") == "~/x"
    assert tilde("/home/me", "/home/me/") == "~"
    assert tilde("/home/meow", "/home/me") == "/home/meow"
