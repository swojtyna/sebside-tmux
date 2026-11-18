# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

1. `uv sync`
2. Make your change with a test. Pure logic (parsing, merging, rendering) gets a unit test in `tests/unit/`;
   anything that needs real tmux goes to `tests/integration/` with `@pytest.mark.tmux` and a private tmux server
   (never the default one).
3. Run `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` and `uv run mypy`.
4. Add a line under `[Unreleased]` in `CHANGELOG.md`.

Ground rules (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

- No runtime dependencies. Standard library only.
- `src/sebside/tmux.py` is the only module that runs a subprocess. A new tmux subcommand must be added to the
  allowlist there, with a reason, and the allowlist test updated.
- sebside never types into a pane, never runs a shell through tmux and never kills a session.
- Nothing tool-specific in the core: integrations talk to sebside through `sebside card set`.
