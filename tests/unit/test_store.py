import json
import os
import stat
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from fakes import NOW, record
from sebside.errors import StateError
from sebside.model import SCHEMA, CardRecord, State, TmuxRef
from sebside.store import file_name, from_dict, state_dir, to_dict


def test_state_dir_respects_xdg_and_override(tmp_path):
    assert state_dir({"XDG_STATE_HOME": str(tmp_path)}) == tmp_path / "sebside"
    assert state_dir({"HOME": "/home/me"}) == Path("/home/me/.local/state/sebside")
    assert state_dir({"SEBSIDE_STATE_DIR": "/x/y", "XDG_STATE_HOME": "/z"}) == Path("/x/y")


def test_file_names_are_safe():
    assert file_name("job-1.build") == "job-1.build.json"
    for nasty in ("../../etc/passwd", "a/b", ".hidden", "x" * 300, "space id"):
        name = file_name(nasty)
        assert name.startswith("id-") and "/" not in name


def test_round_trip_and_unknown_fields():
    original = CardRecord(
        "a",
        State.DONE,
        title="T",
        resume=("vim", "x y"),
        tmux=TmuxRef("/s", 7, "%3"),
        rev=4,
        closed_at=NOW,
        seen_rev=4,
    )
    data = to_dict(original)
    assert data["schema"] == SCHEMA and data["resume"] == ["vim", "x y"]
    assert from_dict({**data, "future_field": 1}) == original


def test_newer_schema_is_readonly_and_never_rewritten(store):
    store.ensure_dir()
    store.path("new").write_text(json.dumps({"schema": SCHEMA + 1, "id": "new", "state": "working"}))
    loaded = store.load("new")
    assert loaded is not None and loaded.readonly
    with pytest.raises(StateError):
        store.write(loaded)
    assert store.close(["new"], NOW) == []


def test_write_is_atomic_and_private(store):
    store.write(record("a"))
    assert store.load("a") == record("a")
    assert [p.name for p in store.cards_dir.iterdir() if p.name.endswith(".tmp")] == []
    assert stat.S_IMODE(store.path("a").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.cards_dir.stat().st_mode) == 0o700


def test_symlinked_or_foreign_card_files_are_ignored(store, tmp_path):
    store.ensure_dir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(to_dict(record("evil", resume=("rm", "-rf", "/")))))
    store.path("evil").symlink_to(outside)
    assert store.load("evil") is None
    assert "evil" not in store.load_all()


def test_broken_file_is_skipped(store):
    store.write(record("good"))
    store.path("bad").write_text("{not json")
    assert set(store.load_all()) == {"good"}


def test_load_all_reuses_parsed_files_until_they_change(store):
    store.write(record("a"))
    first = store.load_all()["a"]
    assert store.load_all()["a"] is first
    store.write(replace(first, desc="changed", rev=2))
    assert store.load_all()["a"].desc == "changed"


def test_update_under_lock(store):
    store.update("a", lambda current: replace(current or record("a"), rev=(current.rev if current else 0) + 1))
    store.update("a", lambda current: replace(current or record("a"), rev=(current.rev if current else 0) + 1))
    loaded = store.load("a")
    assert loaded is not None and loaded.rev == 2


def test_concurrent_updates_do_not_lose_revisions(store):
    def bump() -> None:
        store.update("a", lambda c: replace(c or record("a"), rev=(c.rev if c else 0) + 1))

    threads = [threading.Thread(target=bump) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    loaded = store.load("a")
    assert loaded is not None and loaded.rev == 20


def test_close_and_mark_seen(store):
    store.write(record("a", State.DONE, rev=3))
    assert store.close(["a", "missing"], NOW) == ["a"]
    assert store.close(["a"], NOW + 1) == []
    store.mark_seen({"a": 3})
    loaded = store.load("a")
    assert loaded is not None and loaded.closed_at == NOW and loaded.seen_rev == 3


def test_trusted_only_when_owned_and_private(store):
    assert not store.trusted("a")[0]
    store.write(record("a"))
    assert store.trusted("a") == (True, "")
    os.chmod(store.path("a"), 0o644)
    ok, reason = store.trusted("a")
    assert not ok and "chmod" in reason


def test_prune_keeps_open_and_newest_closed(store):
    for index in range(6):
        store.write(record(f"c{index}", closed_at=NOW - 100 + index))
    store.write(record("ancient", closed_at=NOW - 40 * 86400))
    store.write(record("open"))
    removed = store.prune(NOW, keep=3, days=30)
    assert set(removed) == {"c0", "c1", "c2", "ancient"}
    assert set(store.load_all()) == {"open", "c3", "c4", "c5"}


def test_delete(store):
    store.write(record("a"))
    assert store.delete("a") and not store.delete("a")
