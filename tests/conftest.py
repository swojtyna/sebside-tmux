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


# --- one test, at most 0.04 s (CONTRIBUTING.md) ---------------------------------------------------------------------
TEST_TIME_LIMIT_S = 0.04
SLOW_DEBT = Path(__file__).with_name("slow_tests_debt.txt")
_DEBT = (
    {line.strip() for line in SLOW_DEBT.read_text().splitlines() if line.strip() and not line.startswith("#")}
    if SLOW_DEBT.exists()
    else set()
)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Setup + call + teardown of one test over 0.04 s fails it. A slow test is a signal it is overbuilt: real data,
    real processes (tmux, git, subprocess, sleep) or a weak mock. Fix: pure function on small data, a fake behind the
    tool's wrapper, an injected clock. Tests listed in `tests/slow_tests_debt.txt` are old debt being fixed; the list
    only shrinks."""
    outcome = yield
    report = outcome.get_result()
    spent = getattr(item, "_spent_s", 0.0) + call.duration
    item._spent_s = spent
    if call.when != "teardown" or spent <= TEST_TIME_LIMIT_S or item.nodeid in _DEBT or report.failed:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} took {spent:.3f} s; one test may take at most {TEST_TIME_LIMIT_S} s "
        "(CONTRIBUTING.md). A slow test is overbuilt: test the pure logic on small data, put "
        "external tools (subprocess, tmux, git) behind a fake, inject the clock instead of sleeping. "
        "Measure: uv run pytest --durations=0 --durations-min=0.04"
    )
