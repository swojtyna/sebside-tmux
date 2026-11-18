"""Card files: `$XDG_STATE_HOME/sebside/cards/<id>.json` (fallback `~/.local/state/sebside/cards/`).

- One JSON file per card; it is the single source of truth for card content.
- Writes go to a temporary file in the same directory, then `os.replace`, so readers never see half a file.
- Read-modify-write and pruning hold `fcntl.flock` on `cards/.lock`, so the sidebar and `card set` never lose
  each other's changes.
- The directory is 0700 and files 0600; files are opened with `O_NOFOLLOW` and skipped unless owned by us.
- `"schema": 1` per card. Unknown fields are ignored; a file with a newer schema is shown but never rewritten.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import StateError
from .model import SCHEMA, CardRecord, State, TmuxRef, parse_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
KEEP_CLOSED = 50
KEEP_CLOSED_DAYS = 30


def state_dir(env: Mapping[str, str]) -> Path:
    override = env.get("SEBSIDE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = env.get("XDG_STATE_HOME") or os.fspath(Path(env.get("HOME", "~")).expanduser() / ".local" / "state")
    return Path(base) / "sebside"


def file_name(card_id: str) -> str:
    """The id itself when it is filename-safe, otherwise a hash (the id is stored inside the file)."""
    if SAFE_ID.match(card_id) and not card_id.startswith("."):
        return f"{card_id}.json"
    return "id-" + hashlib.sha256(card_id.encode()).hexdigest()[:32] + ".json"


# (de)serialization: pure -------------------------------------------------------------------------
def to_dict(record: CardRecord) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": record.id,
        "state": record.state.value,
        "title": record.title,
        "subtitle": record.subtitle,
        "desc": record.desc,
        "extra": record.extra,
        "resume": list(record.resume),
        "cwd": record.cwd,
        "tmux": None
        if record.tmux is None
        else {"socket": record.tmux.socket, "server_pid": record.tmux.server_pid, "pane": record.tmux.pane},
        "rev": record.rev,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "closed_at": record.closed_at,
        "seen_rev": record.seen_rev,
    }


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    return value if isinstance(value, str) else ""


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _num(data: Mapping[str, Any], key: str) -> float:
    return _finite(data.get(key, 0)) or 0.0


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name} in card file")


def parse_json(raw: bytes) -> dict[str, Any] | None:
    """Strict JSON: NaN and Infinity are not numbers a card file may contain."""
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def from_dict(data: Mapping[str, Any]) -> CardRecord | None:
    """A card from its JSON; None when it is not a card. Migrations from older schemas go here (none yet)."""
    card_id = _str(data, "id")
    if not card_id or not card_id.isprintable() or len(card_id) > 200:
        return None
    schema = data.get("schema", SCHEMA)
    schema = schema if isinstance(schema, int) else SCHEMA
    try:
        state = parse_state(_str(data, "state") or "unknown")
    except ValueError:
        state = State.UNKNOWN
    raw_tmux = data.get("tmux")
    ref = None
    if isinstance(raw_tmux, dict) and isinstance(raw_tmux.get("pane"), str) and raw_tmux.get("pane"):
        ref = TmuxRef(_str(raw_tmux, "socket"), int(_num(raw_tmux, "server_pid")), _str(raw_tmux, "pane"))
    raw_resume = data.get("resume", [])
    resume = tuple(raw_resume) if isinstance(raw_resume, list) and all(isinstance(a, str) for a in raw_resume) else ()
    closed = data.get("closed_at")
    return CardRecord(
        id=card_id,
        state=state,
        title=_str(data, "title"),
        subtitle=_str(data, "subtitle"),
        desc=_str(data, "desc"),
        extra=_str(data, "extra"),
        resume=resume,
        cwd=_str(data, "cwd"),
        tmux=ref,
        rev=int(_num(data, "rev")),
        created_at=_num(data, "created_at"),
        updated_at=_num(data, "updated_at"),
        closed_at=_finite(closed),
        seen_rev=int(_num(data, "seen_rev")),
        schema=schema,
        readonly=schema > SCHEMA,
    )


# files -------------------------------------------------------------------------------------------
@dataclass
class Store:
    root: Path  # the sebside state directory
    _cache: dict[str, tuple[int, int, int, CardRecord | None]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Store:
        return cls(state_dir(env))

    @property
    def cards_dir(self) -> Path:
        return self.root / "cards"

    def path(self, card_id: str) -> Path:
        return self.cards_dir / file_name(card_id)

    def ensure_dir(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = self.root.lstat()
            if root.st_uid == os.getuid() and stat.S_IMODE(root.st_mode) & 0o077:
                self.root.chmod(0o700)
            self.cards_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise StateError(f"cannot create {self.cards_dir}: {error.strerror}") from None
        info = self.cards_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise StateError(f"{self.cards_dir} is not a directory owned by you: remove it and try again")
        if stat.S_IMODE(info.st_mode) & 0o077:
            self.cards_dir.chmod(0o700)

    def dir_stamp(self) -> int:
        """Changes whenever a card file is added, replaced or removed (cheap check before a reload)."""
        try:
            return self.cards_dir.stat().st_mtime_ns
        except OSError:
            return 0

    def _read(self, path: Path, private: bool = False) -> tuple[CardRecord | None, str]:
        """Parse one card file from the descriptor that was checked (no symlinks, owned by us; with `private`
        also no group/other permissions). Files whose name does not match their id are ignored."""
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None, f"{path} cannot be opened (missing or a symlink)"
        key = os.fspath(path)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                return None, f"{path} is not a regular file owned by you"
            if private and stat.S_IMODE(info.st_mode) & 0o077:
                return None, f"{path} is readable or writable by others: chmod go-rwx it"
            stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
            cached = self._cache.get(key)
            if cached and cached[:3] == stamp:
                return cached[3], ""
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(1_000_000)
        finally:
            os.close(descriptor)
        data = parse_json(raw)
        record = from_dict(data) if data is not None else None
        if record is not None and file_name(record.id) != path.name:
            record = None
        self._cache[key] = (*stamp, record)
        return record, "" if record else f"{path} is not a valid card file"

    def read_file(self, path: Path) -> CardRecord | None:
        return self._read(path)[0]

    def load_trusted(self, card_id: str) -> tuple[CardRecord | None, str]:
        """The card as read for running its resume argv: directory and file owned by us and private."""
        try:
            info = self.cards_dir.lstat()
        except OSError:
            return None, f"{self.cards_dir} does not exist"
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
            return None, f"{self.cards_dir} is not a directory owned by you"
        if stat.S_IMODE(info.st_mode) & 0o077:
            return None, f"{self.cards_dir} is readable or writable by others: chmod go-rwx it"
        return self._read(self.path(card_id), private=True)

    def load(self, card_id: str) -> CardRecord | None:
        return self.read_file(self.path(card_id))

    def load_all(self) -> dict[str, CardRecord]:
        """Every readable card. Files whose mtime and size did not change are not parsed again."""
        try:
            paths = [p for p in self.cards_dir.iterdir() if p.suffix == ".json" and not p.name.startswith(".")]
        except OSError:
            return {}
        records: dict[str, CardRecord] = {}
        for path in paths:
            record = self.read_file(path)
            if record is not None:
                records[record.id] = record
        live_keys = {os.fspath(p) for p in paths}
        for key in [k for k in self._cache if k not in live_keys]:
            del self._cache[key]
        return records

    def trusted(self, card_id: str) -> tuple[bool, str]:
        """May the resume argv of this card be run? File and directory owned by us, no group/other permissions."""
        record, reason = self.load_trusted(card_id)
        return record is not None, reason

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.ensure_dir()
        descriptor = os.open(self.cards_dir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def write(self, record: CardRecord) -> None:
        """Atomic write (temporary file + rename). Call under `locked()` when it follows a read."""
        if record.readonly:
            raise StateError(f"card {record.id!r} was written by a newer sebside: upgrade sebside to change it")
        self.ensure_dir()
        target = self.path(record.id)
        descriptor, temporary = tempfile.mkstemp(prefix=".card-", suffix=".tmp", dir=self.cards_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(to_dict(record), handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(target)
        except BaseException as error:
            with contextlib.suppress(OSError):
                Path(temporary).unlink()
            if isinstance(error, OSError):
                raise StateError(f"cannot write {target}: {error.strerror}") from None
            if isinstance(error, UnicodeError | ValueError):
                raise StateError(f"cannot write {target}: text is not valid UTF-8") from None
            raise

    def update(self, card_id: str, change: Callable[[CardRecord | None], CardRecord | None]) -> CardRecord | None:
        """Read, change and write one card under the lock. `change` returns None to write nothing."""
        with self.locked():
            current = self.load(card_id)
            new = change(current)
            if new is not None and new != current:
                self.write(new)
            return new if new is not None else current

    def delete(self, card_id: str) -> bool:
        with self.locked():
            try:
                self.path(card_id).unlink()
            except FileNotFoundError:
                return False
            return True

    def close(self, card_ids: list[str], now: float, expected: Mapping[str, int] | None = None) -> list[str]:
        """Mark open cards closed. Readonly cards are left alone. With `expected` (id -> rev seen when the decision
        was made), a card that `card set` changed in the meantime is not closed."""
        closed: list[str] = []
        if not card_ids:
            return closed
        with self.locked():
            for card_id in card_ids:
                record = self.load(card_id)
                if record is None or record.closed or record.readonly:
                    continue
                if expected is not None and card_id in expected and record.rev != expected[card_id]:
                    continue
                self.write(replace(record, closed_at=now))
                closed.append(card_id)
        return closed

    def mark_seen(self, seen: Mapping[str, int]) -> None:
        if not seen:
            return
        with self.locked():
            for card_id, rev in seen.items():
                record = self.load(card_id)
                if record is not None and not record.readonly and record.seen_rev < rev:
                    self.write(replace(record, seen_rev=rev))

    def prune(self, now: float, keep: int = KEEP_CLOSED, days: float = KEEP_CLOSED_DAYS) -> list[str]:
        """Delete closed cards beyond the newest `keep` or older than `days`."""
        removed: list[str] = []
        with self.locked():
            closed = sorted(
                (r for r in self.load_all().values() if r.closed and not r.readonly),
                key=lambda r: r.closed_at or 0.0,
                reverse=True,
            )
            for index, record in enumerate(closed):
                if index >= keep or now - (record.closed_at or now) > days * 86400:
                    with contextlib.suppress(FileNotFoundError):
                        self.path(record.id).unlink()
                    removed.append(record.id)
        return removed
