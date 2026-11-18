"""Draw cards as rows of styled cells, and turn them into ANSI text.

Colors are the terminal's own 16 ANSI colors plus default foreground/background, bold and dim, so the sidebar
follows the terminal theme. `#rrggbb` overrides from the config become 24-bit escapes.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import ANSI_NAMES, HEX, Config
from .model import Card, Meter, State

if TYPE_CHECKING:
    from .cards import Board

BAR = "▌"
RULE = "─"
ELLIPSIS = "…"
HERE = "●"  # marks the card of the session this sidebar sits in


@dataclass(frozen=True)
class Style:
    fg: str = "default"
    bold: bool = False
    dim: bool = False
    bg: str | None = None


Cell = tuple[str, Style]  # "" = right half of a wide character
Row = list[Cell]
Span = tuple[str, Style]


# text ---------------------------------------------------------------------------------------------
def clean(text: str) -> str:
    """One printable line: control characters (newlines, escapes, tabs) become spaces."""
    return "".join(ch if ch.isprintable() else " " for ch in text)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def clip(text: str, width: int) -> str:
    """Cut `text` to `width` columns, ending with … when something was cut."""
    text = clean(text)
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + ELLIPSIS


def cells(text: str, style: Style) -> Row:
    row: Row = []
    for ch in text:
        w = char_width(ch)
        if w == 0:
            continue
        row.append((ch, style))
        if w == 2:
            row.append(("", style))
    return row


def line(
    width: int, left: list[Span], right: Span | list[Span] | None = None, bg: str | None = None, margin: bool = True
) -> Row:
    """A row of exactly `width` cells: left spans, padding, right span, one trailing space. Column 0 is a margin
    that never takes the selection background."""
    blank = Style(bg=bg)
    row: Row = [(" ", Style())] if margin else []
    for text, style in left:
        row += cells(text, Style(style.fg, style.bold, style.dim, bg))
    right_spans = [right] if isinstance(right, tuple) else right or []
    right_cells: Row = []
    for text, style in right_spans:
        right_cells += cells(text, Style(style.fg, style.bold, style.dim, bg))
    pad = width - 1 - len(row) - len(right_cells)
    row += [(" ", blank)] * max(pad, 0) + right_cells
    row = fit(row, max(width - 1, 0))
    if width > 0:
        row.append((" ", blank))
    return fit(row, width)


def fit(row: Row, width: int) -> Row:
    """Cut a row to `width` cells without leaving half of a wide character (which would overflow one column)."""
    out = list(row[: max(width, 0)])
    for index, (ch, style) in enumerate(out):
        orphan_right = ch == "" and (index == 0 or char_width(out[index - 1][0]) != 2)
        orphan_left = ch != "" and char_width(ch) == 2 and (index + 1 >= len(out) or out[index + 1][0] != "")
        if orphan_right or orphan_left:
            out[index] = (" ", style)
    return out


# ages ---------------------------------------------------------------------------------------------
def age(seconds: float | None) -> str:
    """0:07, 5:54 (minutes:seconds) under an hour, then 3h, then 2d."""
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# cards --------------------------------------------------------------------------------------------
def icon(card: Card, config: Config, frame: int) -> str:
    key = "closed" if not card.live else card.state.value
    frames = config.icon_frames(key)
    return frames[frame % len(frames)] if len(frames) > 1 and card.live else frames[0]


def color_of(card: Card, config: Config) -> str:
    return config.colors["closed"] if not card.live else config.colors[card.state.value]


def _second_right(extra: str, here: bool, extra_style: Style, dot_style: Style) -> list[Span] | None:
    """Right side of line 2: the extra text, then the "you are here" dot (a space apart from the extra)."""
    spans: list[Span] = [(extra, extra_style)] if extra else []
    if here:
        spans += [((" " if extra else "") + HERE, dot_style)]
    return spans or None


def card_rows(
    card: Card,
    width: int,
    selected: bool,
    config: Config,
    now: float,
    frame: int = 0,
    *,
    current_session: str | None = None,
) -> list[Row]:
    """Three lines and a thin rule:  ▌ ● title   time /  ▌   subtitle   extra /  ▌   description…"""
    bg = config.colors["selected-bg"] if selected else None
    color = color_of(card, config)
    here = current_session is not None and card.session == current_session
    quiet = not card.live and not selected  # only closed cards are dimmed; state shows in the bar and icon
    text = config.colors["text"]
    bar: Span = (BAR, Style(color, bold=True))
    body = width - 6
    if config.show_age:
        when = age(now - card.since) if card.since else ""
        extra = clip(card.extra, max(body // 3, 0)) if card.extra else ""
    else:  # no age: the extra text (e.g. context left) takes its place on the first line
        when = clip(card.extra, max(body // 3, 0)) if card.extra else ""
        extra = ""
    title = clip(card.title, body - text_width(when) - 1)
    reserved = (text_width(extra) + 1 if extra else 0) + (2 if here else 0)
    subtitle = clip(card.subtitle, body - reserved)
    desc = clip(card.desc, body)
    first = line(
        width,
        [
            bar,
            (" ", Style()),
            (icon(card, config, frame), Style(color, bold=True)),
            (" ", Style()),
            (title, Style(text, bold=not quiet, dim=quiet)),
        ],
        [(when, Style(text, bold=not quiet, dim=quiet))],
        bg,
    )
    second = line(
        width,
        [bar, ("   ", Style()), (subtitle, Style(text, dim=True))],
        _second_right(extra, here, Style(text, bold=not quiet, dim=quiet), Style(color, bold=True)),
        bg,
    )
    # A finished card says so in the "done" color (the theme's green), not in dim text, even once it is closed or
    # seen; bold only while nobody has looked at it yet.
    if card.state is State.DONE:
        desc_style = Style(config.colors[State.DONE], bold=True)
    elif card.seen:
        desc_style = Style(config.colors[State.DONE])
    else:
        desc_style = Style(text, dim=True)
    third = line(width, [bar, ("   ", Style()), (desc, desc_style)], None, bg)
    rule = line(width, [(RULE * max(width - 2, 0), Style(config.colors["muted"], dim=True))])
    return [first, second, third, rule]


# frame --------------------------------------------------------------------------------------------
def prefix_label(prefix: str) -> str:
    """tmux's "C-b" as "Ctrl-b"."""
    return "Ctrl-" + prefix[2:] if prefix.startswith("C-") else prefix


def counter_row(board: Board, width: int, config: Config, frame: int) -> Row:
    counts = board.counts()
    spans: list[Span] = []
    labels = ((State.WAITING, "waiting"), (State.ERROR, "error"), (State.WORKING, "working"), (State.DONE, "done"))
    for state, label in labels:
        n = counts[state]
        if not n:
            continue
        frames = config.icon_frames(state.value)
        mark = frames[frame % len(frames)] if len(frames) > 1 else frames[0]
        strong = state in (State.WAITING, State.ERROR)
        spans += [
            (mark + " ", Style(config.colors[state.value], bold=True)),
            (f"{n} {label}  ", Style(config.colors["text"], bold=strong, dim=not strong)),
        ]
    if not spans:
        spans = [("nothing needs you", Style(config.colors["text"], dim=True))]
    return line(width, spans, margin=False)


METER_FULL = "█"
METER_EMPTY = "░"


def meter_rows(meter: Meter | None, width: int, config: Config, now: float) -> list[Row]:
    """`label ████████░░░░░░  47%` and the note under it, aligned with the bar. Nothing for a missing meter or one
    older than `stale_hours` (its number would no longer be true)."""
    if meter is None or now - meter.at > config.stale_hours * 3600:
        return []
    text = config.colors["text"]
    level = "error" if meter.percent >= 90 else "waiting" if meter.percent >= 75 else "working"
    label = clip(meter.label, 12) + " "
    value = f"  {meter.percent}%"
    room = max(width - 1 - text_width(label) - len(value), 4)
    filled = round(room * meter.percent / 100)
    rows = [
        line(
            width,
            [
                (label, Style(text)),
                (METER_FULL * filled, Style(config.colors[level])),
                (METER_EMPTY * (room - filled), Style(config.colors["muted"], dim=True)),
                (value, Style(text, bold=meter.percent >= 75)),
            ],
            margin=False,
        )
    ]
    if meter.note:
        indent = " " * text_width(label)
        note = indent + clip(meter.note, width - 1 - len(indent))
        rows.append(line(width, [(note, Style(text, dim=True))], margin=False))
    return rows


def hint_rows(width: int, config: Config, prefix: str, mode: str) -> list[Row]:
    muted = Style(config.colors["text"], dim=True)
    key = prefix_label(prefix)
    first = {
        "explain": "? or Esc back · q quit",
        "confirm": "⏎ run · Esc/click cancel · q quit",
    }.get(mode, "⏎ go · ↑↓ select · ? why · q quit")
    return [
        line(width, [(clip(first, width - 1), muted)], margin=False),
        line(width, [(clip(f"{key} → session · {key} ← back", width - 1), muted)], margin=False),
    ]


def explain_rows(card: Card | None, width: int, config: Config, now: float) -> list[Row]:
    text = config.colors["text"]
    if card is None:
        return [line(width, [("No card selected.", Style(text, dim=True))])]
    rows = [line(width, [(clip(f"Why: {card.title}", width - 2), Style(text, bold=True))]), line(width, [])]
    if not card.card_id:
        kind = "tmux basics only (no card set)"
    elif not card.live:
        kind = f"closed card {card.card_id}"
    else:
        kind = f"card {card.card_id}" + ("" if card.in_tmux else ", written outside tmux")
    facts = [
        ("state", card.state.value + (" (stale)" if card.stale else "")),
        ("from", card.sources.get("state", "")),
        ("age", f"{age(now - card.since)} ago" if card.since else "unknown"),
        ("age from", card.sources.get("age", "")),
        ("kind", kind),
        ("title", card.sources.get("title", "card file")),
        ("subtitle", card.sources.get("subtitle", "card file")),
        ("desc", card.sources.get("desc", "card file")),
        ("where", f"{card.session}:{card.window} {card.pane_id}" if card.pane_id else "-"),
        ("rev", str(card.rev) if card.rev else "-"),
    ]
    if not card.live:
        facts.append(("resume", " ".join(card.resume) if card.resume else card.sources.get("resume", "")))
    for label, value in facts:
        rows += wrap_fact(label, value, width, config)
    return rows


def confirm_rows(card: Card | None, width: int, config: Config, allowed: bool) -> list[Row]:
    """Shown before a resume runs: the exact argv, and whether the config allows it."""
    text = config.colors["text"]
    if card is None:
        return []
    rows = [line(width, [(clip(f"Resume {card.title}?", width - 2), Style(text, bold=True))]), line(width, [])]
    rows += wrap_fact("runs", " ".join(card.resume) or "-", width, config)
    rows += wrap_fact("as", "new detached tmux session, no shell", width, config)
    if not allowed:
        rows.append(line(width, []))
        rows += wrap_fact(
            "blocked", f"{card.resume[0] if card.resume else '-'} is not in resume_programs", width, config
        )
    return rows


def _hard_split(word: str, width: int) -> list[str]:
    out: list[str] = []
    current, used = "", 0
    for ch in word:
        w = char_width(ch)
        if used + w > width and current:
            out.append(current)
            current, used = "", 0
        current += ch
        used += w
    return [*out, current] if current else out


def chunks(text: str, width: int) -> list[str]:
    """Word-wrap text into lines of at most `width` columns (long words are split)."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if text_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        pieces = _hard_split(word, width) or [""]
        lines += pieces[:-1]
        current = pieces[-1]
    if current:
        lines.append(current)
    return lines or ["-"]


def wrap_fact(label: str, value: str, width: int, config: Config) -> list[Row]:
    text = config.colors["text"]
    pieces = chunks(clean(value) or "-", max(width - 13, 4))
    out = [line(width, [(f"{label:<10}", Style(text, dim=True)), (" " + pieces[0], Style(text))])]
    out += [line(width, [(" " * 10, Style()), (" " + piece, Style(text))]) for piece in pieces[1:]]
    return out


def body_layout(
    board: Board,
    selected_key: str | None,
    width: int,
    config: Config,
    now: float,
    frame: int,
    current_session: str | None = None,
) -> tuple[list[Row], tuple[int, int], list[str | None]]:
    """All card rows, the row span of the selected card (for scrolling) and the card key of every row (None for
    rows that belong to no card, such as the "recently closed" header)."""
    rows: list[Row] = []
    keys: list[str | None] = []
    span = (0, 0)
    muted = Style(config.colors["muted"], dim=True)
    if not board.cards:
        return [line(width, [("No tmux sessions to show.", muted)])], span, [None]
    for index, card in enumerate(board.cards):
        if index == len(board.live) and board.history:
            rows.append(line(width, [("recently closed", muted)]))
            keys.append(None)
        start = len(rows)
        rows += card_rows(
            card, width, card.key == selected_key, config, now, frame, current_session=current_session
        )
        keys += [card.key] * (len(rows) - len(keys))
        if card.key == selected_key:
            span = (start, len(rows))
    return rows, span, keys


def body_rows(
    board: Board, selected_key: str | None, width: int, config: Config, now: float, frame: int
) -> tuple[list[Row], tuple[int, int]]:
    """All card rows, and the row span of the selected card (for scrolling)."""
    rows, span, _ = body_layout(board, selected_key, width, config, now, frame)
    return rows, span


def frame_layout(
    board: Board,
    selected_key: str | None,
    width: int,
    height: int,
    config: Config,
    now: float,
    *,
    frame: int = 0,
    mode: str = "list",
    message: str = "",
    prefix: str = "C-b",
    resume_allowed: bool = False,
    current_session: str | None = None,
    meter: Meter | None = None,
) -> tuple[list[Row], list[str | None]]:
    """The whole screen and, per screen row, the card key under it (None for anything else). Cards are scrolled to
    keep the selection visible, then come the counter and key hints."""
    footer: list[Row] = []
    if message:
        footer.append(line(width, [(clip(message, width - 1), Style(config.colors["text"], bold=True))], margin=False))
    footer.append(line(width, []))
    footer.append(counter_row(board, width, config, frame))
    footer += meter_rows(meter, width, config, now)
    footer += hint_rows(width, config, prefix, mode)
    room = max(height - len(footer), 0)
    selected = next((card for card in board.cards if card.key == selected_key), None)
    body_keys: list[str | None] = []
    if mode == "explain":
        body = explain_rows(selected, width, config, now)[:room]
    elif mode == "confirm":
        body = confirm_rows(selected, width, config, resume_allowed)[:room]
    else:
        rows, (start, end), keys = body_layout(
            board, selected_key, width, config, now, frame, current_session
        )
        offset = max(0, end - room) if end > room else 0
        offset = min(offset, start)
        body = rows[offset : offset + room]
        body_keys = keys[offset : offset + room]
    body_keys += [None] * (room - len(body_keys))
    body += [line(width, [])] * (room - len(body))
    if height <= 0:
        return [], []
    tail = len(footer)
    return (
        (body + footer)[-height:],
        (body_keys + [None] * tail)[-height:],
    )


def frame_rows(
    board: Board,
    selected_key: str | None,
    width: int,
    height: int,
    config: Config,
    now: float,
    *,
    frame: int = 0,
    mode: str = "list",
    message: str = "",
    prefix: str = "C-b",
    resume_allowed: bool = False,
    current_session: str | None = None,
    meter: Meter | None = None,
) -> list[Row]:
    """The whole screen: cards (scrolled to keep the selection visible), then the counter, meter and key hints."""
    return frame_layout(
        board,
        selected_key,
        width,
        height,
        config,
        now,
        frame=frame,
        mode=mode,
        message=message,
        prefix=prefix,
        resume_allowed=resume_allowed,
        current_session=current_session,
        meter=meter,
    )[0]


# ANSI ---------------------------------------------------------------------------------------------
def _color_code(color: str, background: bool) -> str:
    if HEX.match(color):
        r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
        return f"{48 if background else 38};2;{r};{g};{b}"
    if color == "default":
        return "49" if background else "39"
    if color in ("grey", "gray"):
        return "100" if background else "90"
    bright = color.startswith("bright-")
    index = ANSI_NAMES.index(color.removeprefix("bright-"))
    base = (100 if bright else 40) if background else (90 if bright else 30)
    return str(base + index)


def sgr(style: Style) -> str:
    codes = ["0", _color_code(style.fg, False)]
    if style.bold:
        codes.append("1")
    if style.dim:
        codes.append("2")
    if style.bg:
        codes.append(_color_code(style.bg, True))
    return "\x1b[" + ";".join(codes) + "m"


def to_ansi(row: Row) -> str:
    out: list[str] = []
    current: Style | None = None
    for ch, style in row:
        if ch == "":
            continue
        if style != current:
            out.append(sgr(style))
            current = style
        out.append(ch)
    out.append("\x1b[0m")
    return "".join(out)


def to_plain(row: Row) -> str:
    return "".join(ch for ch, _ in row)
