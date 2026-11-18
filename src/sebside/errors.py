"""Error tree. Each error carries the process exit code the CLI returns for it."""

from __future__ import annotations

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_TMUX = 3
EXIT_STATE = 4


class SebsideError(Exception):
    """Base error: message is `<what failed>: <hint>`."""

    exit_code = EXIT_RUNTIME


class UsageError(SebsideError):
    exit_code = EXIT_USAGE


class ConfigError(UsageError):
    pass


class TmuxError(SebsideError):
    """tmux is missing, not running, or refused a call."""

    exit_code = EXIT_TMUX


class ForbiddenCommand(SebsideError):
    """A tmux subcommand or argument outside the allowlist (a bug, never user input)."""


class StateError(SebsideError):
    """The state directory or a card file cannot be read or written safely."""

    exit_code = EXIT_STATE
