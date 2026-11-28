"""Command line: `sebside` (the sidebar), `sebside card ...`, `sebside meter ...`, `sebside group ...`,
`sebside reconcile`, `sebside config ...`, `sebside uninstall`. Every command is `run_<name>(ctx, args) -> int`;
errors map to exit codes (see errors.py)."""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import __version__
from .cards import build, pane_groups, reconcile, server_of
from .config import SORTS, Config, config_path, load_from_env
from .errors import EXIT_OK, SebsideError, StateError, TmuxError, UsageError
from .log import logger, setup
from .model import (
    OPTION_GROUP,
    OPTION_ID,
    OPTION_ROLE,
    OPTION_STATE,
    SIDEBAR_ROLE,
    CardRecord,
    Group,
    Meter,
    State,
    TmuxRef,
    parse_state,
    valid_group,
)
from .render import clean
from .store import Store
from .tmux import FIELD_SEP, OWN_HOOKS, PANE_ID, Tmux, format_field, server_from_env
from .tui import App, run

MAX_DESC = 300
MAX_TEXT = 200
DEFAULT_KEY = "S"


@dataclass(frozen=True)
class Context:
    env: Mapping[str, str]
    store: Store
    now: float


Command = Callable[[Context, argparse.Namespace], int]


# parser ------------------------------------------------------------------------------------------
@functools.cache  # built once per process: parse_args never changes the parser, and building it is most of a call
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sebside", description="A tmux sidebar of cards, one per session or task.")
    parser.add_argument("--version", action="version", version=f"sebside {__version__}")
    parser.add_argument("--socket", help="tmux socket path (default: $SEBSIDE_TMUX_SOCKET, else the one in $TMUX)")
    parser.add_argument("-v", "--verbose", action="store_true", help="write a debug log to the state directory")
    # The same two options are accepted after a subcommand too (`sebside reconcile --socket S`); SUPPRESS keeps a
    # subcommand from overwriting a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--socket", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")

    sidebar = commands.add_parser("sidebar", parents=[common], help="open the sidebar (the default command)")
    sidebar.add_argument("--sort", choices=SORTS)
    sidebar.add_argument("--history", type=int, help="how many closed cards to show")

    card = commands.add_parser("card", help="set, clear or list cards").add_subparsers(dest="card", required=True)
    states = ", ".join(state.value for state in State)
    set_ = card.add_parser("set", parents=[common], help="create or update a card (in tmux: on $TMUX_PANE)")
    set_.add_argument("--id", required=True, help="stable card id chosen by you, e.g. a task or agent session id")
    set_.add_argument("--state", required=True, help=f"one of: {states}")
    set_.add_argument("--title", help="first line (default: session name)")
    set_.add_argument("--subtitle", help="second line (default: pane directory)")
    set_.add_argument("--desc", help="third line (default: current command)")
    set_.add_argument("--extra", help="short text on the right of the second line")
    set_.add_argument("--resume", help="command that brings the card back, split into words, run without a shell")
    set_.add_argument("--cwd", help="directory for resume (default: current directory)")
    set_.add_argument("--pane", help="target pane id (default: $TMUX_PANE)")
    set_.add_argument(
        "--group", help='group id (default: the stored group, else the pane\'s @sebside_group; "" clears it)'
    )

    update = card.add_parser(
        "update", parents=[common], help="change text of an existing card; state, pane and age stay as they are"
    )
    update.add_argument("--id", required=True, help="id of an existing card")
    update.add_argument("--title", help="first line")
    update.add_argument("--subtitle", help="second line")
    update.add_argument("--desc", help="third line")
    update.add_argument("--extra", help="short text on the right of the second line")

    clear = card.add_parser("clear", parents=[common], help="close a card (it stays in history unless --forget)")
    clear.add_argument("--id", help="card id (default: the card on $TMUX_PANE)")
    clear.add_argument("--pane", help="target pane id (default: $TMUX_PANE)")
    clear.add_argument("--forget", action="store_true", help="delete the card file too")

    list_ = card.add_parser("list", parents=[common], help="print live and recently closed cards as JSON")
    list_.add_argument("--group", help="only the cards of this group, as a sidebar in that group shows them")

    meter = commands.add_parser("meter", help="set or clear the usage meter under the counter").add_subparsers(
        dest="meter", required=True
    )
    meter_set = meter.add_parser("set", parents=[common], help="show `label ████░░ N%%` and a note under the counter")
    meter_set.add_argument("--label", required=True, help="short name, e.g. week")
    meter_set.add_argument("--percent", required=True, type=int, help="0..100")
    meter_set.add_argument("--note", default="", help="second line, e.g. when the limit resets")
    meter_set.add_argument("--session", help="only sidebars in this tmux session show it (instead of the shared one)")
    meter_clear = meter.add_parser("clear", parents=[common], help="remove the meter")
    meter_clear.add_argument("--session", help="remove only this session's meter")

    group = commands.add_parser("group", help="set or clear the footer of a group's sidebars").add_subparsers(
        dest="group", required=True
    )
    group_set = group.add_parser("set", parents=[common], help="show a title and a note above the meter")
    group_set.add_argument("--id", required=True, help="group id, the value of @sebside_group")
    group_set.add_argument("--title", required=True, help="wrapped over up to group_lines lines")
    group_set.add_argument("--note", default="", help="one dimmed line under the title")
    group_clear = group.add_parser("clear", parents=[common], help="remove the group's footer")
    group_clear.add_argument("--id", required=True, help="group id")

    commands.add_parser(
        "reconcile", parents=[common], help="close cards whose pane is gone (the session-closed hook calls this)"
    )

    config = commands.add_parser("config", help="show the config").add_subparsers(dest="config", required=True)
    config.add_parser("path", parents=[common], help="print the config file path")
    config.add_parser("show", parents=[common], help="print the effective config as JSON")
    get = config.add_parser("get", parents=[common], help="print one value, e.g. width")
    get.add_argument("key")

    open_ = commands.add_parser("open", parents=[common], help="open the sidebar pane in a window")
    open_.add_argument(
        "--if-missing", action="store_true", help="only when the window has no sidebar (quiet, idempotent)"
    )
    open_.add_argument("--target", help="window id (default: the window of $TMUX_PANE)")

    commands.add_parser(
        "uninstall", parents=[common], help="remove sebside's tmux hook, key binding, sidebar panes and pane options"
    )
    return parser


# helpers -----------------------------------------------------------------------------------------
def _socket(args: argparse.Namespace, env: Mapping[str, str]) -> Path | None:
    explicit = args.socket or env.get("SEBSIDE_TMUX_SOCKET")
    if explicit:
        return Path(explicit).expanduser()
    server = server_from_env(env)
    return Path(server.socket) if server else None


def _tmux(args: argparse.Namespace, env: Mapping[str, str], *, uninstall: bool = False, opener: bool = False) -> Tmux:
    if not Tmux.available():
        raise TmuxError("tmux not found: install tmux 3.0 or newer")
    if opener:
        return Tmux.create(_socket(args, env), opener=True)
    return Tmux.create(_socket(args, env), uninstall=uninstall)


def _one_line(value: str, limit: int) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:limit]


def _config(args: argparse.Namespace, env: Mapping[str, str]) -> Config:
    flags: dict[str, Any] = {}
    for key in ("sort", "history"):
        value = getattr(args, key, None)
        if value is not None:
            flags[key] = value
    return load_from_env(env, flags)


# card set ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Target:
    tmux: Tmux
    ref: TmuxRef
    previous_id: str
    group: str = ""  # the pane's @sebside_group as tmux resolves it ("" when unset or not a valid id)


def _target(args: argparse.Namespace, env: Mapping[str, str]) -> Target | None:
    """The pane to attach the card to, or None outside tmux (then tmux is never called)."""
    pane = args.pane or (env.get("TMUX_PANE", "") if env.get("TMUX") else "")
    if not pane:
        return None
    socket = _socket(args, env)
    if socket is None:
        raise UsageError("--pane needs a tmux server: pass --socket or run inside tmux")
    tmux = _tmux(args, env)
    names = ("pid", "socket_path", OPTION_ID, "pane_id", OPTION_GROUP)
    done = tmux.display(pane, FIELD_SEP.join(format_field(name) for name in names))
    if not done.ok:
        raise TmuxError(f"cannot read pane {pane}: {done.error}")
    values = done.stdout.rstrip("\n").split(FIELD_SEP)
    if len(values) != len(names) or not values[0].isdigit() or not PANE_ID.match(values[3]):
        raise TmuxError(f"unexpected tmux answer for pane {pane}: is it tmux 3.0 or newer?")
    group = values[4] if valid_group(values[4]) else ""
    return Target(tmux, TmuxRef(values[1], int(values[0]), values[3]), values[2], group)


def _group_arg(value: str | None, *, empty: bool = False) -> str | None:
    """A --group value: None when not given; "" (no group) only where `empty` allows it."""
    if value is None or (empty and value == ""):
        return value
    if not valid_group(value):
        raise UsageError(f"--group {value!r} is not a group id: 1..100 letters, digits, '.', '_' or '-'")
    return value


def _card_id(value: str) -> str:
    card_id = value.strip()
    if not card_id or _one_line(card_id, MAX_TEXT) != card_id:
        raise UsageError(f"--id must be a non-empty single line of at most {MAX_TEXT} characters")
    return card_id


def run_card_set(ctx: Context, args: argparse.Namespace) -> int:
    card_id = _card_id(args.id)
    try:
        state = parse_state(args.state)
    except ValueError as error:
        raise UsageError(str(error)) from None
    try:
        resume = tuple(shlex.split(args.resume)) if args.resume is not None else None
    except ValueError as error:
        raise UsageError(f"--resume: {error}") from None
    if resume is not None and not all(_utf8(word) and word.isprintable() for word in resume):
        raise UsageError("--resume must be printable UTF-8 text")
    group = _group_arg(args.group, empty=True)
    text = _text_fields(args)
    target = _target(args, ctx.env)
    if target is not None:
        if target.previous_id and target.previous_id != card_id:
            _close_if_on(ctx, target)
        _set_option(target, OPTION_ID, card_id)
        _set_option(target, OPTION_STATE, state.value)
    cwd = _escape_surrogates(args.cwd or ctx.env.get("PWD") or os.fspath(Path.cwd()))

    def change(current: CardRecord | None) -> CardRecord:
        record = current or CardRecord(card_id, created_at=ctx.now)
        if record.readonly:
            raise StateError(f"card {card_id!r} was written by a newer sebside: upgrade sebside")
        updates: dict[str, Any] = dict(text)
        if resume is not None:
            updates["resume"] = resume
        if group is not None:
            updates["group"] = group
        elif not record.group and target is not None:
            updates["group"] = target.group  # without --group a stored group stays; with none stored, the pane's
        updates["pane_group"] = target.group if target else ""
        return replace(
            record,
            **updates,
            state=state,
            cwd=cwd,
            tmux=target.ref if target else None,
            rev=record.rev + 1,
            updated_at=ctx.now,
            closed_at=None,
        )

    record = ctx.store.update(card_id, change)
    if record is None:
        raise StateError(f"card {card_id!r} was not written: check {ctx.store.cards_dir}")
    where = f"pane {record.tmux.pane}" if record.tmux else "outside tmux"
    print(f"card {clean(record.id)}: {record.state.value} (rev {record.rev}, {where})")
    return EXIT_OK


def run_card_update(ctx: Context, args: argparse.Namespace) -> int:
    """Only the given text fields change: never the state, the pane, the revision, the age or any tmux option, so a
    done card you have seen stays seen and a card stays on its pane. tmux is not called."""
    card_id = _card_id(args.id)
    text = _text_fields(args)
    if not text:
        raise UsageError("nothing to update: pass --title, --subtitle, --desc or --extra")

    def change(current: CardRecord | None) -> CardRecord:
        if current is None:
            raise UsageError(f"no card {card_id!r}: create it with sebside card set --id ... --state ...")
        if current.readonly:
            raise StateError(f"card {card_id!r} was written by a newer sebside: upgrade sebside")
        fields: dict[str, Any] = dict(text)
        return replace(current, **fields)

    record = ctx.store.update(card_id, change)
    if record is None:
        raise StateError(f"card {card_id!r} was not written: check {ctx.store.cards_dir}")
    print(f"card {clean(record.id)}: updated {', '.join(text)} (rev {record.rev})")
    return EXIT_OK


def _text_fields(args: argparse.Namespace) -> dict[str, str]:
    """The text flags that were given, each made one clean line of at most its length."""
    fields = (("title", args.title), ("subtitle", args.subtitle), ("desc", args.desc), ("extra", args.extra))
    return {
        name: _one_line(value, MAX_DESC if name == "desc" else MAX_TEXT) for name, value in fields if value is not None
    }


def _utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _escape_surrogates(text: str) -> str:
    """A path from a non-UTF-8 environment, made writable as JSON (undecodable bytes become U+FFFD)."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _close_if_on(ctx: Context, target: Target) -> bool:
    """Close the card the pane option names, but only if its file says it lives on this very pane. A stale or
    copied option must never close a card that is live elsewhere."""
    previous = ctx.store.load(target.previous_id)
    if previous is None or previous.tmux != target.ref:
        return False
    return bool(ctx.store.close([previous.id], ctx.now, {previous.id: previous.rev}, {previous.id: target.group}))


def _set_option(target: Target, name: str, value: str) -> None:
    done = target.tmux.set_pane_option(target.ref.pane, name, value)
    if not done.ok:
        raise TmuxError(f"tmux refused to set {name} on {target.ref.pane}: {done.error}")


# other commands ----------------------------------------------------------------------------------
def run_card_clear(ctx: Context, args: argparse.Namespace) -> int:
    card_id = args.id
    target = _target(args, ctx.env) if not card_id or args.pane else None
    if target is not None:
        for name in (OPTION_ID, OPTION_STATE):
            target.tmux.unset_pane_option(target.ref.pane, name)
        if not card_id:
            if not target.previous_id:
                raise UsageError("no card on this pane: pass --id")
            # Without --id only the card that really lives on this pane is closed.
            record = ctx.store.load(target.previous_id)
            if record is None or record.tmux != target.ref:
                print(f"removed the pane option; {clean(target.previous_id)} lives elsewhere and stays open")
                return EXIT_OK
            card_id = target.previous_id
    if not card_id:
        raise UsageError("no card to clear: pass --id, or run inside a pane that has a card")
    current = ctx.store.load(card_id) if target is not None else None
    groups = {card_id: target.group} if target is not None and current and current.tmux == target.ref else None
    done = ctx.store.delete(card_id) if args.forget else bool(ctx.store.close([card_id], ctx.now, None, groups))
    verb = "forgot" if args.forget else "closed"
    print(f"{verb} {clean(card_id)}" if done else f"{clean(card_id)}: nothing to do")
    return EXIT_OK


def run_card_list(ctx: Context, args: argparse.Namespace) -> int:
    config = _config(args, ctx.env)
    panes = _tmux(args, ctx.env).list_panes() if _socket(args, ctx.env) else []
    group = _group_arg(args.group) or ""
    board = build(panes or [], ctx.store.load_all(), config, ctx.now, ctx.env.get("HOME", ""), group)
    payload = {"live": [asdict(card) for card in board.live], "history": [asdict(card) for card in board.history]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def run_reconcile(ctx: Context, args: argparse.Namespace) -> int:
    panes = _tmux(args, ctx.env).list_panes()
    if panes is None:
        raise TmuxError("tmux did not answer: try again")
    records = ctx.store.load_all()
    server = server_of(panes)
    gone = reconcile(records, panes, server, lambda path: Path(path).exists(), ctx.now)
    closed = ctx.store.close(gone, ctx.now, {card_id: records[card_id].rev for card_id in gone})
    ctx.store.remember_pane_groups(pane_groups(records, panes, server))
    ctx.store.prune(ctx.now)
    if closed:
        print("closed: " + ", ".join(clean(card_id) for card_id in closed))
    return EXIT_OK


def run_config(ctx: Context, args: argparse.Namespace) -> int:
    if args.config == "path":
        print(config_path(ctx.env))
        return EXIT_OK
    config = _config(args, ctx.env)
    data: dict[str, Any] = asdict(config)
    data["icons"], data["colors"] = dict(config.icons), dict(config.colors)
    if args.config == "show":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    if args.key not in data:
        raise UsageError(f"unknown key {args.key!r}: known keys are {', '.join(data)}")
    value = data[args.key]
    print(json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list | tuple) else value)
    return EXIT_OK


def run_uninstall(ctx: Context, args: argparse.Namespace) -> int:
    tmux = _tmux(args, ctx.env, uninstall=True)
    panes = tmux.list_panes()
    if panes is None:
        raise TmuxError("tmux did not answer: try again")
    for hook in sorted(OWN_HOOKS):
        tmux.run("set-hook", "-gu", hook)
    key = tmux.run("show-options", "-gqv", "@sebside-key").stdout.strip() or DEFAULT_KEY
    binding = tmux.run("list-keys", "-T", "prefix", key)
    if binding.ok and "sebside" in binding.stdout:
        tmux.run("unbind-key", "-T", "prefix", key)
    killed = 0
    for pane in panes:
        if pane.role == SIDEBAR_ROLE:
            killed += tmux.run("kill-pane", "-t", pane.pane_id).ok
            continue
        for name in (OPTION_ID, OPTION_STATE, OPTION_ROLE):
            tmux.unset_pane_option(pane.pane_id, name)
    print(f"removed hooks, key binding and {killed} sidebar pane(s); card files stay in {ctx.store.cards_dir}")
    print("also remove the plugin line from your tmux.conf, or the next start installs it again")
    return EXIT_OK


TARGET = re.compile(r"^[@%$][0-9]+$")
WIDTH = re.compile(r"^([0-9]+)(%?)$")


def _sidebar_command(env: Mapping[str, str]) -> str:
    """The command that runs the sidebar in its pane: this very program, quoted for sh."""
    own = Path(sys.argv[0])
    candidates = [env.get("SEBSIDE_BIN", ""), os.fspath(own) if own.name == "sebside" and own.is_absolute() else ""]
    program = next((c for c in candidates if c and os.access(c, os.X_OK)), "") or shutil.which(
        "sebside", path=env.get("PATH")
    )
    argv = [program, "sidebar"] if program else [sys.executable, "-m", "sebside", "sidebar"]
    return shlex.join(argv)


def _open_width(tmux: Tmux, configured: int, window: str) -> str:
    """Width for the new pane: `@sebside-width` (columns or percent) or the config, always under half the window."""
    match = WIDTH.match(tmux.option("@sebside-width"))
    value, percent = (int(match.group(1)), match.group(2) == "%") if match else (configured, False)
    if percent:
        return f"{max(1, min(value, 49))}%"
    done = tmux.display(window, "#{window_width}")
    total = int(done.stdout.strip()) if done.ok and done.stdout.strip().isdigit() else 80
    return str(max(10, min(value, total // 2 - 1)))


def run_open(ctx: Context, args: argparse.Namespace) -> int:
    target = args.target or (ctx.env.get("TMUX_PANE", "") if ctx.env.get("TMUX") else "")
    if not target or not TARGET.match(target):
        raise UsageError("open needs --target (a window id like @3) or must run inside a tmux pane")
    tmux = _tmux(args, ctx.env, opener=True)
    existing = tmux.sidebar_panes(target)
    if existing is None:
        raise TmuxError(f"cannot read window {target}")
    if existing:
        if args.if_missing:
            return EXIT_OK
        raise UsageError(f"window {target} already has a sidebar")
    width = _open_width(tmux, _config(args, ctx.env).width, target)
    done = tmux.open_sidebar(target, width, _sidebar_command(ctx.env))
    if not done.ok:
        raise TmuxError(f"cannot open the sidebar: {done.error}")
    return EXIT_OK


def run_sidebar(ctx: Context, args: argparse.Namespace) -> int:
    socket = _socket(args, ctx.env)
    if socket is None:
        raise TmuxError("not inside tmux: open the sidebar from a tmux pane (prefix + S) or pass --socket")
    app = App(_config(args, ctx.env), _tmux(args, ctx.env), ctx.store, home=ctx.env.get("HOME", ""))
    return run(app, ctx.env.get("TMUX_PANE") if ctx.env.get("TMUX") else None)


def run_meter_set(ctx: Context, args: argparse.Namespace) -> int:
    if not 0 <= args.percent <= 100:
        raise UsageError(f"--percent must be 0..100, got {args.percent}")
    label = _one_line(args.label, 16)
    if not label:
        raise UsageError("--label must not be empty")
    ctx.store.write_meter(Meter(label, args.percent, _one_line(args.note, 60), ctx.now), args.session)
    return EXIT_OK


def run_meter_clear(ctx: Context, args: argparse.Namespace) -> int:
    ctx.store.clear_meter(args.session)
    return EXIT_OK


def _group_id(value: str) -> str:
    if not valid_group(value):
        raise UsageError(f"--id {value!r} is not a group id: 1..100 letters, digits, '.', '_' or '-'")
    return value


def run_group_set(ctx: Context, args: argparse.Namespace) -> int:
    group_id = _group_id(args.id)
    title = _one_line(args.title, MAX_TEXT)
    if not title:
        raise UsageError("--title must not be empty")
    ctx.store.write_group(Group(group_id, title, _one_line(args.note, MAX_TEXT), ctx.now))
    return EXIT_OK


def run_group_clear(ctx: Context, args: argparse.Namespace) -> int:
    ctx.store.clear_group(_group_id(args.id))
    return EXIT_OK


COMMANDS: dict[str, Command] = {
    "meter set": run_meter_set,
    "meter clear": run_meter_clear,
    "group set": run_group_set,
    "group clear": run_group_clear,
    "sidebar": run_sidebar,
    "card set": run_card_set,
    "card update": run_card_update,
    "card clear": run_card_clear,
    "card list": run_card_list,
    "reconcile": run_reconcile,
    "config": run_config,
    "uninstall": run_uninstall,
    "open": run_open,
}


# main --------------------------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    environment = dict(os.environ) if env is None else dict(env)
    args = _parser().parse_args(argv)
    store = Store.from_env(environment)
    debug = bool(args.verbose) or environment.get("SEBSIDE_DEBUG") == "1"
    ctx = Context(environment, store, time.time())
    name = args.command or "sidebar"
    if name == "card":
        name = f"card {args.card}"
    if name == "meter":
        name = f"meter {args.meter}"
    if name == "group":
        name = f"group {args.group}"
    try:
        setup(debug, store.root)
        return COMMANDS[name](ctx, args)
    except SebsideError as error:
        logger().exception("failed")
        print(f"sebside: {error}", file=sys.stderr)
        return error.exit_code
    except OSError as error:
        if debug:
            raise
        print(f"sebside: {error.strerror or error}: run with -v for a debug log", file=sys.stderr)
        return StateError.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
