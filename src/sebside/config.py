"""`~/.config/sebside/config.toml` (or `$XDG_CONFIG_HOME/sebside/config.toml`). Every key is optional."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ConfigError
from .model import State

if TYPE_CHECKING:
    from collections.abc import Mapping

SORTS: tuple[str, ...] = ("attention", "recent", "name")

DEFAULT_ICONS: dict[str, str] = {
    State.WAITING: "●",
    State.ERROR: "✖",
    State.WORKING: "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",  # several characters = animation frames
    State.DONE: "✔",
    State.QUIET: "○",
    State.UNKNOWN: "?",
    "closed": "↺",
}
DEFAULT_COLORS: dict[str, str] = {
    State.WAITING: "bright-yellow",
    State.ERROR: "red",
    State.WORKING: "cyan",
    State.DONE: "green",
    State.QUIET: "grey",
    State.UNKNOWN: "grey",
    "closed": "grey",
    "text": "default",
    "muted": "grey",
    "selected-bg": "grey",
}
ANSI_NAMES: tuple[str, ...] = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def valid_color(value: str) -> bool:
    if value in ("default", "grey", "gray") or HEX.match(value):
        return True
    name = value.removeprefix("bright-")
    return name in ANSI_NAMES


@dataclass(frozen=True)
class Filter:
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()

    def allows(self, session: str) -> bool:
        if not any(fnmatchcase(session, pattern) for pattern in self.include):
            return False
        return not any(fnmatchcase(session, pattern) for pattern in self.exclude)


@dataclass(frozen=True)
class Config:
    width: int = 36
    history: int = 5
    stale_hours: float = 12.0
    quiet_minutes: float = 10.0
    refresh_seconds: float = 1.5
    sort: str = "attention"
    icons: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ICONS))
    colors: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    filter: Filter = field(default_factory=Filter)
    show_age: bool = True  # the time on the right of each card; false hides it
    resume_programs: tuple[str, ...] = ()  # programs a closed card may be resumed with; empty = resume off
    group_lines: int = 3  # lines for the group title in the footer of a grouped sidebar; longer titles end with …

    def may_resume(self, argv: tuple[str, ...]) -> bool:
        """Only argv whose program (as written, or its basename) is on the allowlist."""
        if not argv:
            return False
        program = argv[0]
        return program in self.resume_programs or ("/" not in program and Path(program).name in self.resume_programs)

    def icon_frames(self, key: str) -> str:
        return self.icons.get(key) or DEFAULT_ICONS.get(key, "?")


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".config")
    return Path(base) / "sebside"


def config_path(env: Mapping[str, str]) -> Path:
    override = env.get("SEBSIDE_CONFIG")
    return Path(override).expanduser() if override else config_dir(env) / "config.toml"


def _number(data: Mapping[str, Any], key: str, default: float, minimum: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{key} must be at least {minimum}, got {value!r}")
    return value


def _string_map(data: object, key: str, allowed: Mapping[str, str]) -> dict[str, str]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"[{key}] must be a table")
    out: dict[str, str] = {}
    for name, value in data.items():
        if name not in allowed:
            raise ConfigError(f"[{key}] unknown key {name!r} (known: {', '.join(allowed)})")
        if not isinstance(value, str) or not value:
            raise ConfigError(f"[{key}] {name} must be a non-empty string")
        out[name] = value
    return out


def _patterns(data: Mapping[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[filter] {key} must be a list of glob strings")
    return tuple(value)


def parse(data: Mapping[str, Any]) -> Config:
    known = {*SCALARS, "icons", "colors", "filter", "resume_programs", "show_age"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(sorted(unknown))}")
    sort = data.get("sort", "attention")
    if sort not in SORTS:
        raise ConfigError(f"sort must be one of {', '.join(SORTS)}, got {sort!r}")
    icons = dict(DEFAULT_ICONS) | _string_map(data.get("icons"), "icons", DEFAULT_ICONS)
    color_overrides = _string_map(data.get("colors"), "colors", DEFAULT_COLORS)
    for name, value in color_overrides.items():
        if not valid_color(value):
            raise ConfigError(
                f"[colors] {name} = {value!r}: use an ANSI name (red, bright-cyan, grey, default) or #rrggbb"
            )
    show_age = data.get("show_age", True)
    if not isinstance(show_age, bool):
        raise ConfigError(f"show_age must be true or false, got {show_age!r}")
    raw_filter = data.get("filter", {})
    if not isinstance(raw_filter, dict):
        raise ConfigError("[filter] must be a table")
    return Config(
        width=int(_number(data, "width", 36, 20)),
        history=int(_number(data, "history", 5, 0)),
        stale_hours=float(_number(data, "stale_hours", 12, 0.01)),
        quiet_minutes=float(_number(data, "quiet_minutes", 10, 0)),
        refresh_seconds=float(_number(data, "refresh_seconds", 1.5, 0.5)),
        sort=sort,
        icons=icons,
        colors=dict(DEFAULT_COLORS) | color_overrides,
        filter=Filter(_patterns(raw_filter, "include", ("*",)), _patterns(raw_filter, "exclude", ())),
        resume_programs=_programs(data.get("resume_programs", [])),
        show_age=show_age,
        group_lines=int(_number(data, "group_lines", 3, 1)),
    )


def _programs(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError('resume_programs must be a list of program names, e.g. ["vim", "htop"]')
    return tuple(value)


SCALARS: dict[str, type] = {
    "width": int,
    "history": int,
    "stale_hours": float,
    "quiet_minutes": float,
    "refresh_seconds": float,
    "sort": str,
    "group_lines": int,
}


def env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """`SEBSIDE_WIDTH=40` and friends: environment beats the TOML file."""
    out: dict[str, Any] = {}
    for key, kind in SCALARS.items():
        raw = env.get(f"SEBSIDE_{key.upper()}")
        if raw is None or raw == "":
            continue
        try:
            out[key] = kind(raw)
        except ValueError:
            raise ConfigError(f"SEBSIDE_{key.upper()}={raw!r} is not a valid {kind.__name__}") from None
    return out


def load(path: Path, overrides: Mapping[str, Any] | None = None) -> Config:
    """TOML file < `overrides` (environment, then command-line flags)."""
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as error:
            raise ConfigError(f"{path}: {error}: fix or remove the file") from None
    try:
        return parse({**data, **(overrides or {})})
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from None


def load_from_env(env: Mapping[str, str], flags: Mapping[str, Any] | None = None) -> Config:
    return load(config_path(env), {**env_overrides(env), **(flags or {})})
