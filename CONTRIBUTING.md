# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

1. `uv sync`
2. Make your change with a test. Pure logic (parsing, merging, rendering) gets a unit test in `tests/unit/`;
   a scenario across sebside, the plugin scripts and tmux goes to `tests/integration/` and runs against
   `TmuxWorld` in `tests/fakes.py`, a fake tmux that behaves like real tmux on the paths sebside uses (the bash
   scripts run for real through `run_script`). If your change needs tmux behaviour the fake does not model, it
   raises `FakeTmuxGap`: extend the fake to match real tmux.
3. One test, setup and teardown included, uses at most 0.04 s of CPU (`tests/conftest.py` fails a slower one, one
   that leaves a process running, and any `time.sleep`): a slow test is overbuilt, so test the logic as a pure
   function on small data, put tmux behind the fake and inject the clock instead of sleeping. Old slow tests are
   listed in `tests/slow_tests_debt.txt`; that list only shrinks (it is empty now). Margin check:
   `TEST_CPU_LIMIT_S=0.025 uv run pytest`.
4. Run `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` and `uv run mypy`.
5. When you change how sebside talks to tmux, check by hand against a real tmux once:
   `uv run pytest --noconftest tests/manual/real_tmux.py` (private tmux servers, never the default one; not part of
   the suite, it waits for real hooks and refreshes).
6. Add a line under `[Unreleased]` in `CHANGELOG.md`.

Ground rules (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

- No runtime dependencies. Standard library only.
- `src/sebside/tmux.py` is the only module that runs a subprocess. A new tmux subcommand must be added to the
  allowlist there, with a reason, and the allowlist test updated.
- sebside never types into a pane, never runs a shell through tmux and never kills a session.
- Nothing tool-specific in the core: integrations talk to sebside through `sebside card set`.
