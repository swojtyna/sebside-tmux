from __future__ import annotations

from pathlib import Path

import pytest

from fakes import SOCKET, FakeTmuxServer
from sebside.store import Store
from sebside.tmux import Tmux


@pytest.fixture
def server() -> FakeTmuxServer:
    return FakeTmuxServer()


@pytest.fixture
def tmux(server: FakeTmuxServer) -> Tmux:
    return Tmux(Path(SOCKET), server)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state" / "sebside")
