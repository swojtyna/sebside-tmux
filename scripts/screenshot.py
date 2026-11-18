"""Render docs/screenshot.png from the real renderer with made-up demo cards (dev only, needs Pillow).

uv run --with pillow python scripts/screenshot.py docs/screenshot.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from sebside.cards import Board
from sebside.config import Config
from sebside.model import Card, Meter, State
from sebside.render import Row, frame_rows

NOW = 1_800_000_000.0
WIDTH, HEIGHT, RIGHT = 36, 34, 52
# A generic dark theme: what the 16 ANSI colors might look like in a terminal profile.
PALETTE = {
    "default": (220, 223, 228),
    "grey": (110, 115, 125),
    "red": (232, 92, 92),
    "green": (110, 200, 120),
    "yellow": (225, 190, 90),
    "bright-yellow": (245, 215, 100),
    "cyan": (90, 190, 210),
    "blue": (100, 150, 230),
    "magenta": (200, 120, 210),
    "white": (230, 230, 230),
    "black": (20, 20, 20),
}
BG, SELECTED = (24, 26, 31), (52, 56, 66)
FONTS = ["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
SYMBOL_FONTS = ["/System/Library/Fonts/Apple Symbols.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def demo_board() -> Board:
    def card(key: str, state: State, title: str, subtitle: str, desc: str, ago: float, **extra: object) -> Card:
        return Card(f"card:{key}", key, state, title, subtitle, desc, since=NOW - ago, **extra)  # type: ignore[arg-type]

    live = [
        card("deploy", State.WAITING, "deploy-api", "~/code/api", "Approve the production migration?", 354),
        card("ci", State.ERROR, "nightly tests", "~/code/api", "3 failed in test_payments.py", 3),
        card("agent", State.WORKING, "agent: refactor auth", "~/code/web", "Editing src/session.ts", 94, extra="12/40"),
        card("docs", State.DONE, "docs build", "~/code/docs", "Built 214 pages in 41 s", 529),
        card("dev", State.QUIET, "dev-server", "~/code/web", "npm run dev: no output for an hour", 3700),
        Card("pane:%9", "", State.UNKNOWN, "scratch", "~", "zsh", since=NOW - 10800),
    ]
    closed = [
        Card(
            "closed:old",
            "old",
            State.DONE,
            "agent: migrate db",
            "~/code/api",
            "Finished, 6 files changed",
            since=NOW - 7200,
            live=False,
            resume=("agent", "--resume", "old"),
        ),
    ]
    return Board(live, closed)


def color(name: str, dim: bool) -> tuple[int, int, int]:
    if name.startswith("#"):
        rgb = (int(name[1:3], 16), int(name[3:5], 16), int(name[5:7], 16))
    else:
        rgb = PALETTE.get(name, PALETTE.get(name.removeprefix("bright-"), PALETTE["default"]))
    return (int(rgb[0] * 0.55), int(rgb[1] * 0.55), int(rgb[2] * 0.55)) if dim else rgb


def font(paths: list[str], size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    for path in paths:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                return ImageFont.truetype(path, size)
    raise SystemExit("no monospace font found: edit FONTS in scripts/screenshot.py")


def draw(rows: list[Row], path: Path) -> None:
    size = 24
    regular, bold = font(FONTS, size, 0), font(FONTS, size, 1)
    symbols = font(SYMBOL_FONTS, size)
    cw, ch = int(regular.getlength("M")), int(size * 1.5)
    image = Image.new("RGB", ((WIDTH + RIGHT) * cw, len(rows) * ch), BG)
    pen = ImageDraw.Draw(image)
    for y, row in enumerate(rows):
        x = 0
        for text, style in row:
            if text == "":
                continue
            x0, y0 = x * cw, y * ch
            pen.rectangle([x0, y0, x0 + cw, y0 + ch], fill=SELECTED if style.bg else BG)
            fg = color(style.fg, style.dim)
            if text == "▌":
                pen.rectangle([x0, y0, x0 + cw // 3, y0 + ch], fill=fg)
            elif text == "─":
                pen.line([x0, y0 + ch // 2, x0 + cw, y0 + ch // 2], fill=fg, width=1)
            elif text != " ":
                face = symbols if 0x2800 <= ord(text) <= 0x28FF else (bold if style.bold else regular)
                pen.text((x0, y0 + (ch - size) // 2 - 2), text, font=face, fill=fg)
            x += 1
    split = WIDTH * cw + cw // 2
    pen.line([split, 0, split, image.height], fill=PALETTE["grey"], width=2)
    main = [
        "$ deploy-api",
        "",
        "Running migration plan:",
        "  + add column orders.region",
        "  + backfill 1.2M rows",
        "",
        "Approve the production migration? [y/N]",
    ]
    for index, text in enumerate(main):
        pen.text((split + 2 * cw, index * ch + 8), text, font=regular, fill=color("default", index != 6))
    image.save(path)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshot.png")
    board = demo_board()
    meter = Meter("week", 62, "resets Fri 20.11 11:00", NOW)
    rows = frame_rows(board, board.live[0].key, WIDTH, HEIGHT, Config(), NOW, frame=3, meter=meter)
    draw(rows, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
