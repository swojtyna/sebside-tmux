import pytest

from fakes import NOW
from sebside.cards import Board
from sebside.config import Config
from sebside.model import Card, State
from sebside.render import (
    Style,
    age,
    card_rows,
    clip,
    counter_row,
    frame_rows,
    prefix_label,
    sgr,
    text_width,
    to_ansi,
    to_plain,
)


def card(state: State = State.WAITING, **kwargs: object) -> Card:
    values: dict[str, object] = {
        "key": f"card:{state.value}",
        "card_id": state.value,
        "state": state,
        "title": "deploy",
        "subtitle": "~/code/api",
        "desc": "Waiting for approval: run migrations",
        "since": NOW - 354,
    }
    values.update(kwargs)
    return Card(**values)  # type: ignore[arg-type]


def test_card_plain_layout():
    rows = card_rows(card(extra="3/4"), 36, False, Config(), NOW)
    assert [to_plain(row) for row in rows] == [
        " ▌ ● deploy                    5:54 ",
        " ▌   ~/code/api                 3/4 ",
        " ▌   Waiting for approval: run mig… ",
        " ────────────────────────────────── ",
    ]


def test_card_ansi_snapshot():
    first = card_rows(card(), 36, False, Config(), NOW)[0]
    assert to_ansi(first) == (
        "\x1b[0;39m \x1b[0;93;1m▌\x1b[0;39m \x1b[0;93;1m●\x1b[0;39m \x1b[0;39;1mdeploy"
        "\x1b[0;39m                    \x1b[0;39;1m5:54\x1b[0;39m \x1b[0m"
    )


def test_selected_card_gets_background_except_margin():
    first = card_rows(card(), 36, True, Config(), NOW)[0]
    assert first[0][1].bg is None
    assert all(style.bg == "grey" for _, style in first[1:])
    assert "100" in to_ansi(first)


def test_closed_card_is_grey():
    closed = card_rows(card(State.WAITING, live=False), 36, False, Config(), NOW)[0]
    assert closed[1][1].fg == "grey" and to_plain(closed)[3] == "↺"


def test_show_age_false_hides_the_time():
    rows = card_rows(card(), 36, False, Config(show_age=False), NOW)
    assert to_plain(rows[0]) == " ▌ ● deploy                         "


def test_without_age_extra_moves_to_the_first_line():
    rows = card_rows(card(extra="ctx 70%"), 36, False, Config(show_age=False), NOW)
    assert to_plain(rows[0]) == " ▌ ● deploy                 ctx 70% "
    assert to_plain(rows[1]) == " ▌   ~/code/api                     "


def test_selected_quiet_card_is_not_dimmed():
    title_style = card_rows(card(State.QUIET), 36, True, Config(), NOW)[0][5][1]
    assert not title_style.dim and title_style.bold


def test_spinner_animates_by_frame():
    icons = {to_plain(card_rows(card(State.WORKING), 36, False, Config(), NOW, frame)[0])[3] for frame in range(10)}
    assert len(icons) == 10


@pytest.mark.parametrize("width", [20, 24, 36, 50, 80])
def test_rows_have_exact_width_at_any_size(width):
    wide = card(title="デプロイ 部署 💬 long title", desc="説明" * 30, extra="ok")
    for row in card_rows(wide, width, True, Config(), NOW):
        assert len(row) == width
        assert text_width(to_plain(row)) == width


def test_wide_icon_from_config_keeps_alignment():
    config = Config(icons={**Config().icons, "waiting": "💬"})
    rows = card_rows(card(), 36, False, config, NOW)
    assert all(text_width(to_plain(row)) == 36 for row in rows)


def test_clip_and_age():
    assert clip("hello world", 6) == "hello…"
    assert clip("a\nb\x1b[31m", 20) == "a b [31m"
    assert clip("日本語テキスト", 7) == "日本語…"
    assert [age(s) for s in (7, 354, 3599, 3600, 90000, -5)] == ["0:07", "5:54", "59:59", "1h", "1d", "0:00"]


def test_hex_and_bright_colors():
    assert sgr(Style("#00ff88", bold=True)) == "\x1b[0;38;2;0;255;136;1m"
    assert sgr(Style("bright-cyan", bg="#000000")) == "\x1b[0;96;48;2;0;0;0m"


def test_counter_shows_only_nonzero_states():
    board = Board([card(State.WAITING), card(State.ERROR, key="e"), card(State.WORKING, key="w")])
    assert to_plain(counter_row(board, 40, Config(), 0)).startswith("● 1 waiting  ✖ 1 error  ⠋ 1 working")
    assert to_plain(counter_row(Board(), 40, Config(), 0)).startswith("nothing needs you")


def test_frame_counter_and_hints_at_bottom():
    board = Board([card()])
    rows = [to_plain(r) for r in frame_rows(board, "card:waiting", 36, 12, Config(), NOW, prefix="C-a")]
    assert len(rows) == 12
    assert rows[-1].startswith("Ctrl-a → session · Ctrl-a ← back")
    assert rows[-2].startswith("⏎ go · ↑↓ select · ? why · q quit")
    assert rows[-3].startswith("● 1 waiting")
    assert rows[0].startswith(" ▌ ● deploy")


def test_frame_scrolls_to_selection():
    cards = [card(State.WORKING, key=f"k{i}", title=f"job {i}") for i in range(10)]
    rows = [to_plain(r) for r in frame_rows(Board(cards), "k9", 36, 12, Config(), NOW)]
    assert any("job 9" in row for row in rows)
    assert not any("job 0" in row for row in rows)


def test_history_section_label():
    board = Board([card()], [card(State.DONE, key="closed:x", live=False, title="old job")])
    rows = [to_plain(r) for r in frame_rows(board, None, 36, 20, Config(), NOW)]
    assert any("recently closed" in row for row in rows)


def test_explain_and_confirm_views():
    selected = card(sources={"state": "card file (sebside card set)", "age": "card file: last card set"})
    explain = [to_plain(r) for r in frame_rows(Board([selected]), selected.key, 60, 20, Config(), NOW, mode="explain")]
    assert explain[0].startswith(" Why: deploy")
    assert any("card file (sebside card set)" in row for row in explain)
    assert explain[-2].startswith("? or Esc back")
    closed = card(live=False, key="closed:a", resume=("agent", "--resume", "abc"))
    confirm = [to_plain(r) for r in frame_rows(Board([], [closed]), closed.key, 40, 20, Config(), NOW, mode="confirm")]
    assert any("agent --resume abc" in row for row in confirm)
    assert any("resume_programs" in row for row in confirm)


def test_prefix_label():
    assert prefix_label("C-b") == "Ctrl-b"
    assert prefix_label("M-a") == "M-a"


def test_current_session_card_gets_a_dot_on_line_two_at_the_right_edge():
    here, other = card(session="api"), card(State.DONE, session="web")
    rows = card_rows(here, 36, False, Config(show_age=False), NOW, current_session="api")
    assert to_plain(rows[1]).endswith("api" + " " * 19 + "● ") and len(rows[1]) == 36
    assert to_plain(rows[1]).rstrip().endswith("●")
    assert all("●" not in to_plain(row) for row in card_rows(other, 36, False, Config(), NOW, current_session="api")[1:])
    assert all("●" not in to_plain(row) for row in card_rows(here, 36, False, Config(), NOW)[1:])


def test_dot_follows_the_extra_text_without_breaking_width():
    rows = card_rows(card(extra="3/4", session="api"), 36, False, Config(), NOW, current_session="api")
    assert to_plain(rows[1]).endswith(" 3/4 ● ") and len(rows[1]) == 36
    long = card_rows(card(subtitle="x" * 80, extra="3/4", session="api"), 36, False, Config(), NOW, current_session="api")
    assert len(long[1]) == 36 and to_plain(long[1]).endswith("3/4 ● ")


def test_live_cards_are_never_dimmed_but_closed_ones_are():
    for state in (State.QUIET, State.DONE, State.UNKNOWN):
        style = card_rows(card(state), 36, False, Config(), NOW)[0][5][1]
        assert not style.dim and style.bold
    closed = card(State.QUIET, live=False)
    assert card_rows(closed, 36, False, Config(), NOW)[0][5][1].dim
