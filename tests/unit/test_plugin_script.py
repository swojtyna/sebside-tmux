"""sebside.tmux against a fake `tmux` on PATH (no tmux server involved)."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_plugin(tmp_path: Path, version: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    fake = bin_dir / "tmux"
    fake.write_text(
        f'#!/bin/sh\nif [ "$1" = "-V" ]; then echo "{version}"; exit 0; fi\nprintf "%s\\n" "$*" >> "{log}"\n'
    )
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(["bash", str(REPO / "sebside.tmux")], env=env, check=True, timeout=20)
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize("version", ["tmux 3.4", "tmux 3.0a", "tmux next-3.6", "tmux master", "tmux openbsd-7.5"])
def test_new_or_unnumbered_versions_pass_the_check(tmp_path: Path, version: str):
    calls = run_plugin(tmp_path, version)
    assert "needs tmux 3.0" not in calls
    assert "'sebside' not found" in calls  # it got past the version check to the PATH check


@pytest.mark.parametrize("version", ["tmux 2.9a", "tmux 1.8"])
def test_old_versions_are_refused(tmp_path: Path, version: str):
    assert "needs tmux 3.0" in run_plugin(tmp_path, version)
