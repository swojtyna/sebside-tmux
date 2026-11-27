"""The usage meter: `sebside meter set/clear`, its file, and the rows under the counter."""

from __future__ import annotations

import os
from pathlib import Path

from sebside import cli
from sebside.config import Config
from sebside.model import Meter
from sebside.render import meter_rows, to_plain
from sebside.store import Store

NOW = 1_800_000_000.0


def _env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}


def test_set_then_clear(tmp_path: Path):
    env = _env(tmp_path)
    assert cli.main(["meter", "set", "--label", "week", "--percent", "47", "--note", "resets Thu 11:00"], env) == 0
    meter = Store.from_env(env).load_meter()
    assert meter is not None and (meter.label, meter.percent, meter.note) == ("week", 47, "resets Thu 11:00")
    assert os.stat(Store.from_env(env).meter_path).st_mode & 0o077 == 0
    assert cli.main(["meter", "clear"], env) == 0
    assert Store.from_env(env).load_meter() is None


def test_percent_out_of_range_is_refused(tmp_path: Path):
    assert cli.main(["meter", "set", "--label", "week", "--percent", "120"], _env(tmp_path)) != 0
    assert Store.from_env(_env(tmp_path)).load_meter() is None


def test_invalid_file_is_ignored(tmp_path: Path):
    store = Store.from_env(_env(tmp_path))
    store.ensure_dir()
    store.meter_path.write_text('{"label": "week", "percent": "lots"}')
    assert store.load_meter() is None


def test_rows_fill_the_width_with_a_proportional_bar():
    rows = [to_plain(row) for row in meter_rows(Meter("week", 50, "resets Thu", NOW), 30, Config(), NOW)]
    assert len(rows) == 2
    assert rows[0].startswith("week ") and rows[0].rstrip().endswith("50%")
    assert rows[0].count("█") == rows[0].count("░") or abs(rows[0].count("█") - rows[0].count("░")) == 1
    assert rows[1].startswith("     resets Thu")


def test_no_rows_without_a_meter_or_when_it_is_old():
    config = Config()
    assert meter_rows(None, 30, config, NOW) == []
    old = Meter("week", 50, "", NOW - config.stale_hours * 3600 - 1)
    assert meter_rows(old, 30, config, NOW) == []


# session meters -----------------------------------------------------------------------------------
def test_session_meter_replaces_the_shared_one_only_in_its_session(tmp_path: Path):
    env = _env(tmp_path)
    assert cli.main(["meter", "set", "--label", "week", "--percent", "29"], env) == 0
    assert cli.main(["meter", "set", "--session", "gemini", "--label", "5h", "--percent", "3", "--note", "x"], env) == 0
    store = Store.from_env(env)
    assert store.load_meter("gemini").label == "5h"
    assert store.load_meter("badania-46").label == "week"
    assert store.load_meter().label == "week"
    assert os.stat(store.session_meter_path("gemini")).st_mode & 0o077 == 0
    assert cli.main(["meter", "clear", "--session", "gemini"], env) == 0
    assert store.load_meter("gemini").label == "week"


def test_session_meter_rejects_a_name_it_cannot_store(tmp_path: Path):
    assert cli.main(["meter", "set", "--session", "../x", "--label", "5h", "--percent", "3"], _env(tmp_path)) != 0


def test_sidebar_shows_the_meter_of_its_own_session(server, tmux, store):
    from fakes import FakePane
    from sebside.tui import App

    server.panes += [FakePane("%1", "gemini"), FakePane("%2", "badania")]
    store.write_meter(Meter("week", 29, "", NOW))
    store.write_meter(Meter("5h", 3, "", NOW), "gemini")
    app = App(Config(), tmux, store, clock=lambda: NOW, socket_exists=lambda p: True, own_pane="%1")
    app.refresh()
    assert app.meter is not None and app.meter.label == "5h"
    app.own_pane = "%2"
    app.refresh()
    assert app.meter is not None and app.meter.label == "week"
