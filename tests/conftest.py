from __future__ import annotations

import os
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


# --- one test, at most 0.04 s of CPU (setup + call + teardown) ------------------------------------------------------
TEST_CPU_LIMIT_S = min(0.04, float(os.environ.get("TEST_CPU_LIMIT_S", "0.04")))  # env may only lower it (margin runs)
SLOW_DEBT = Path(__file__).with_name("slow_tests_debt.txt")
_DEBT = (
    {line.strip() for line in SLOW_DEBT.read_text().splitlines() if line.strip() and not line.startswith("#")}
    if SLOW_DEBT.exists()
    else set()
)


def _cpu_s() -> float:
    """CPU seconds of this process and of its finished children (subprocess, bash, tmux client)."""
    import resource

    own, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime


def _child_pids() -> set[int]:
    """Live and finished-but-unreaped children of this process (macOS libproc, a few µs); empty elsewhere."""
    import ctypes

    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return set()
    buffer = (ctypes.c_int * 1024)()
    count = lib.proc_listchildpids(os.getpid(), buffer, ctypes.sizeof(buffer))
    return {buffer[i] for i in range(max(count, 0)) if buffer[i]}


def _new_live_children(before: set[int]) -> set[int]:
    """Children started during the test: finished ones are reaped (their CPU is charged now), live ones returned."""
    live = set()
    for pid in _child_pids() - before:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if done == 0:
            live.add(pid)
    return live


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    item._children_before = _child_pids()
    item._cpu_start_s = _cpu_s()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Setup + call + teardown of one test over 0.04 s of CPU fails it. CPU, not wall clock, so a busy machine does not
    make a test flaky. A slow test is a signal it is overbuilt: real processes (bash, tmux, subprocess), sleeping or a
    weak mock. Fix: a pure function on small data, a fake behind the tmux wrapper, an injected clock. Tests listed in
    `tests/slow_tests_debt.txt` are old debt being fixed; the list only shrinks."""
    outcome = yield
    report = outcome.get_result()
    if call.when != "teardown" or item.nodeid in _DEBT or report.failed:
        return
    if live := _new_live_children(getattr(item, "_children_before", set())):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} left child process(es) {sorted(live)} running: an unreaped child escapes the CPU "
            "measure. Wait for (or kill and wait for) every process the test starts, or better, start none: put "
            "the tool behind a fake."
        )
        return
    spent = _cpu_s() - getattr(item, "_cpu_start_s", _cpu_s())
    if spent <= TEST_CPU_LIMIT_S:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} used {spent:.3f} s of CPU; one test may use at most {TEST_CPU_LIMIT_S} s. A slow test is "
        "overbuilt: test the pure logic on small data, put external tools (subprocess, bash, tmux) behind a fake "
        "(tests/fakes.py), inject the clock instead of sleeping. Measure: TEST_CPU_LIMIT_S=0 uv run pytest. Remove "
        "the line from tests/slow_tests_debt.txt once the test is fast."
    )


@pytest.fixture(autouse=True)
def no_real_sleep(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test never waits: `time.sleep` fails it (also no way around the CPU limit). Code that polls takes
    `sleep`/`clock` as parameters; tests pass a fake. Old slow tests on the debt list are exempt until rewritten."""
    if request.node.nodeid in _DEBT:
        return
    import time

    def refuse(seconds: float) -> None:
        raise AssertionError(
            f"time.sleep({seconds}) in a test: inject `sleep`/`clock` into the code under test and pass a fake "
            "(a test never waits)"
        )

    monkeypatch.setattr(time, "sleep", refuse)
