"""Debug log: only to a file in the state directory, only with `-v` or `SEBSIDE_DEBUG=1`."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

NAME = "sebside"


def logger() -> logging.Logger:
    return logging.getLogger(NAME)


def setup(debug: bool, state_dir: Path) -> Path | None:
    """Turn on file logging; returns the log path, or None when debugging is off."""
    log = logger()
    log.propagate = False
    if not debug:
        if not log.handlers:
            log.addHandler(logging.NullHandler())
        return None
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / "debug.log"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600))
    path.chmod(0o600)  # the log may contain card text: owner only
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    return path
