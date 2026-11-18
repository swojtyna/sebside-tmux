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

