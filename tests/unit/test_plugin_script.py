"""The plugin's tmux version check: sebside.tmux runs in real bash, `tmux` is answered by the fake (no tmux server,
no executable written, so each row takes a few milliseconds)."""

import shutil
from pathlib import Path

import pytest

from fakes import TmuxWorld, run_script

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_plugin(tmp_path: Path, version: str) -> str:
    """Messages the plugin shows (display-message) when loaded with `tmux -V` printing `version`."""
    world = TmuxWorld(str(tmp_path / "s"), version=version)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMUX": f"{world.socket},{world.pid},0"}
    done = run_script(world, REPO / "sebside.tmux", env=env)
    assert done.ok, done.stderr
    return "\n".join(world.messages)


@pytest.mark.parametrize("version", ["tmux 3.4", "tmux 3.0a", "tmux next-3.6", "tmux master", "tmux openbsd-7.5"])
def test_new_or_unnumbered_versions_pass_the_check(tmp_path: Path, version: str):
    calls = run_plugin(tmp_path, version)
    assert "needs tmux 3.0" not in calls
    assert "'sebside' not found" in calls  # it got past the version check to the PATH check


@pytest.mark.parametrize("version", ["tmux 2.9a", "tmux 1.8"])
def test_old_versions_are_refused(tmp_path: Path, version: str):
    assert "needs tmux 3.0" in run_plugin(tmp_path, version)
